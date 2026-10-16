"""Team-scoped enterprise sign-in and directory lifecycle orchestration."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..domain.governance import teams
from ..domain.identity import api_keys as managed_keys
from ..domain.identity.access import _is_machine_email, _norm_email
from ..infra.db import session_maker
from ..models import (EnterpriseDirectoryUser, EnterpriseSSO, EnterpriseSSOEvent,
                      EnterpriseSSOIdentity, Membership, Org, User)
from . import signup


class EnterpriseSSOError(Exception):
    pass


PAID_STATUSES = frozenset({"active", "trialing"})


@dataclass(frozen=True)
class SigninResult:
    user_id: int
    token_version: int
    org_id: int
    org_slug: str


async def configured_team(org_id: int) -> tuple[Org, EnterpriseSSO]:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        config = await db.get(EnterpriseSSO, org_id)
        if org is None or config is None or config.subscription_status not in PAID_STATUSES:
            raise EnterpriseSSOError("SSO is not active for this team")
        return org, config


async def bind_workos_org(org_id: int, workos_org_id: str) -> None:
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        if config is None or config.subscription_status not in PAID_STATUSES:
            raise EnterpriseSSOError("SSO is not active for this team")
        if config.workos_org_id and config.workos_org_id != workos_org_id:
            raise EnterpriseSSOError("team already linked to another SSO organization")
        config.workos_org_id = workos_org_id
        db.add(config)
        await db.commit()


async def finish_signin(org_id: int, profile: dict) -> SigninResult:
    """A WorkOS code exchange is the only caller; untrusted browser data never reaches here."""
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        org = await db.get(Org, org_id)
        if (config is None or org is None or org.suspended
                or config.subscription_status not in PAID_STATUSES or not config.workos_org_id):
            raise EnterpriseSSOError("SSO is not active for this team")
        if profile.get("organization_id") != config.workos_org_id:
            raise EnterpriseSSOError("SSO response belongs to another team")
        connection = profile.get("connection_id")
        profile_id = profile.get("id")
        idp_id = profile.get("idp_id")
        email = profile.get("email")
        if not all(isinstance(value, str) and value for value in (connection, profile_id, idp_id, email)):
            raise EnterpriseSSOError("SSO response is missing identity fields")
        if config.connection_id and config.connection_id != connection:
            raise EnterpriseSSOError("SSO response came from another connection")
        email = _norm_email(email)
        if _is_machine_email(email):
            raise EnterpriseSSOError("machine identity cannot sign in")
        directory_user = None
        if config.plan == "sso_directory" and config.directory_id:
            if not config.directory_active:
                raise EnterpriseSSOError("directory access is inactive")
            directory_user = (await db.execute(select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.org_id == org_id,
                EnterpriseDirectoryUser.directory_id == config.directory_id,
                EnterpriseDirectoryUser.idp_id == idp_id,
            ))).scalar_one_or_none()
            if directory_user is None or not directory_user.active or directory_user.email != email:
                raise EnterpriseSSOError("this identity is not active in the company directory")
        identity = (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id,
            EnterpriseSSOIdentity.profile_id == profile_id,
        ))).scalar_one_or_none()
        if identity is not None and not identity.active:
            raise EnterpriseSSOError("this team has deactivated the identity")
        if identity is not None:
            user = await db.get(User, identity.user_id)
            if user is None or user.email != email or identity.connection_id != connection:
                # Email changes need an explicit, reviewed account-link flow; silently switching
                # the global User row would also change access to every other team.
                raise EnterpriseSSOError("SSO identity changed; contact the team owner")
        else:
            try:
                user = await signup.find_or_create_user(db, email, door="enterprise_sso",
                    verified=True, reject_unverified_existing=True)
            except signup.UnverifiedExistingUserError as exc:
                raise EnterpriseSSOError("verify this existing treg account before linking SSO") from exc
            except (signup.MachineIdentityError, signup.BlockedEmailError) as exc:
                raise EnterpriseSSOError("this identity cannot sign in") from exc
            existing_link = (await db.execute(select(EnterpriseSSOIdentity).where(
                EnterpriseSSOIdentity.org_id == org_id,
                EnterpriseSSOIdentity.user_id == user.id,
            ))).scalar_one_or_none()
            if existing_link is not None:
                raise EnterpriseSSOError("this account is linked to another SSO identity")
            db.add(EnterpriseSSOIdentity(
                org_id=org_id, user_id=user.id, profile_id=profile_id,
                idp_id=idp_id, connection_id=connection,
                directory_user_id=directory_user.directory_user_id if directory_user else None,
            ))
        if identity is not None and directory_user is not None:
            identity.directory_user_id = directory_user.directory_user_id
            db.add(identity)
        if user.suspended:
            raise EnterpriseSSOError("account suspended")
        membership = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user.id,
        ))).scalar_one_or_none()
        if membership is None:
            membership = Membership(user_id=user.id, org_id=org_id, role="member", token_hash="")
            db.add(membership)
            await db.flush()
            await managed_keys.ensure_default_key(db, membership, user)
        if not config.connection_id:
            config.connection_id = connection
            db.add(config)
        await db.commit()
        return SigninResult(user.id, user.token_version, org.id, org.slug)


async def set_enforced(org_id: int, enabled: bool) -> bool:
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        if config is None:
            raise EnterpriseSSOError("SSO is not configured for this team")
        if enabled and (config.subscription_status not in PAID_STATUSES or not config.connection_id):
            raise EnterpriseSSOError("test SSO sign-in before requiring it")
        config.enforced = enabled
        db.add(config)
        await db.commit()
        return enabled


async def apply_directory_event(event: dict) -> bool:
    """Deactivate only the matching team membership; never globally suspend a user."""
    event_id = event.get("id")
    kind = event.get("event")
    data = event.get("data")
    if not isinstance(event_id, str) or not isinstance(data, dict):
        raise EnterpriseSSOError("invalid directory event")
    try:
        event_ms = int(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
    except (KeyError, AttributeError, TypeError, ValueError):
        event_ms = 0
    if kind not in {"dsync.activated", "dsync.deleted", "dsync.user.created", "dsync.user.updated", "dsync.user.deleted"}:
        return False
    workos_org = data.get("organization_id")
    directory_id = data.get("id") if kind in {"dsync.activated", "dsync.deleted"} else data.get("directory_id")
    if not all(isinstance(v, str) and v for v in (workos_org, directory_id)):
        raise EnterpriseSSOError("directory event is missing organization")
    async with session_maker() as db:
        if await db.get(EnterpriseSSOEvent, event_id) is not None:
            return False
        config = (await db.execute(select(EnterpriseSSO).where(
            EnterpriseSSO.workos_org_id == workos_org,
        ))).scalar_one_or_none()
        if config is None or config.plan != "sso_directory" or config.subscription_status not in PAID_STATUSES:
            return False
        if config.directory_id and config.directory_id != directory_id and config.directory_active:
            raise EnterpriseSSOError("directory event belongs to another connection")
        if kind in {"dsync.activated", "dsync.deleted"} and event_ms and event_ms <= config.directory_event_ms:
            db.add(EnterpriseSSOEvent(id=event_id))
            await db.commit()
            return False
        if kind == "dsync.activated":
            config.directory_id = directory_id
            config.directory_active = True
            config.directory_event_ms = event_ms
        elif kind == "dsync.deleted":
            config.directory_active = False
            config.directory_event_ms = event_ms
            identities = (await db.execute(select(EnterpriseSSOIdentity).where(
                EnterpriseSSOIdentity.org_id == config.org_id,
            ))).scalars().all()
            for identity in identities:
                await _deactivate_identity(db, identity)
                identity.directory_user_id = None
                db.add(identity)
        else:
            directory_user_id = data.get("id")
            email = data.get("email")
            idp_id = data.get("idp_id")
            if not all(isinstance(v, str) and v for v in (directory_user_id, email, idp_id)):
                raise EnterpriseSSOError("directory event is missing identity fields")
            email = _norm_email(email)
            record = (await db.execute(select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.org_id == config.org_id,
                EnterpriseDirectoryUser.directory_user_id == directory_user_id,
            ))).scalar_one_or_none()
            if record is not None and event_ms and event_ms <= record.last_event_ms:
                db.add(EnterpriseSSOEvent(id=event_id))
                await db.commit()
                return False
            record = record or EnterpriseDirectoryUser(org_id=config.org_id,
                directory_user_id=directory_user_id, directory_id=directory_id,
                idp_id=idp_id, email=email)
            record.directory_id = directory_id
            record.idp_id = idp_id
            record.email = email
            record.active = kind != "dsync.user.deleted" and data.get("state") != "inactive"
            record.last_event_ms = event_ms
            db.add(record)
            if not config.directory_id:
                config.directory_id = directory_id
            identity = (await db.execute(select(EnterpriseSSOIdentity).where(
                EnterpriseSSOIdentity.org_id == config.org_id,
                EnterpriseSSOIdentity.idp_id == idp_id,
            ))).scalar_one_or_none()
            if identity is not None:
                if identity.directory_user_id and identity.directory_user_id != directory_user_id:
                    raise EnterpriseSSOError("directory identity mismatch")
                identity.directory_user_id = directory_user_id
                if not record.active:
                    await _deactivate_identity(db, identity)
                else:
                    identity.active = True
                    db.add(identity)
        db.add(config)
        db.add(EnterpriseSSOEvent(id=event_id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # concurrent webhook redelivery
            return False
        return True


async def _deactivate_identity(db, identity: EnterpriseSSOIdentity) -> None:
    identity.active = False
    membership = (await db.execute(select(Membership).where(
        Membership.org_id == identity.org_id,
        Membership.user_id == identity.user_id,
    ))).scalar_one_or_none()
    if membership is not None and membership.role != "owner":
        await teams.delete_membership(db, membership)
    db.add(identity)


async def revoke_team_keys_and_memberships(db, org_id: int) -> None:
    """End existing SSO access when its paid subscription ends; preserve owner recovery."""
    identities = (await db.execute(select(EnterpriseSSOIdentity).where(
        EnterpriseSSOIdentity.org_id == org_id,
    ))).scalars().all()
    for identity in identities:
        membership = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == identity.user_id,
        ))).scalar_one_or_none()
        if membership is not None and membership.role != "owner":
            await teams.delete_membership(db, membership)
