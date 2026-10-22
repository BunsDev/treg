"""A new account's first team, made for it so the dashboard never asks for a name.

`first_team` makes it through the ordinary signup door (`signup.create_org`), so the signup credit,
attribution and referral behave exactly as for a team the user named, under the first of
`team_names` the registry accepts. The user renames it in Team.
"""
from __future__ import annotations

from sqlalchemy import update
from sqlmodel import select

from ...domain.catalog.routing.paths import email_domain
from ...infra.db import session_maker
from ...models import Membership, Org, User
from .. import signup


async def _named_team(db, email: str) -> bool:
    """Whether this account is in a team; the legacy door's personal team (named after the email) is
    not one the user named."""
    return (await db.execute(select(Membership.id).join(Org, Org.id == Membership.org_id).join(
        User, User.id == Membership.user_id).where(User.email == email, Org.name != email).limit(1))).first() is not None


def team_names(email: str) -> list[str]:
    """A first team's names before anyone has named it, best first: the work domain's company, then
    the person's, then a plain one. The next is tried when the registry refuses one (a reserved name)."""
    local = email.split("@")[0].replace("+", ".").split(".")[0]
    names = [f"{local[:1].upper()}{local[1:]}'s team"] if local else []
    if domain := email_domain(email):
        label = domain.split(".")[0]
        names.insert(0, label[:1].upper() + label[1:])
    return names + ["My team"]


async def _create_team(user: User, **cookies: str) -> dict:
    """The first team, under the first of `team_names` the registry accepts."""
    names = team_names(user.email)
    for name in names:
        try:
            return await signup.create_org(user=user, name=name, **cookies)
        except signup.SignupError as exc:
            if exc.kind not in ("reserved_name", "slug_conflict") or name == names[-1]:
                raise
    raise AssertionError("unreachable")


async def first_team(user: User, *, ad_cookie: str, utm_cookie: str, referral_cookie: str) -> dict:
    """The dashboard's first team for a new account. Claims the account's one first run by flipping
    `onboarded`, so a second tab makes no second team; a failed create hands the claim back.
    `created` is false when there was nothing to make: the account was already onboarded or already
    in a team it named."""
    async with session_maker() as db:
        claimed = await db.execute(update(User).where(
            User.id == user.id, User.onboarded == False).values(onboarded=True))  # noqa: E712
        if claimed.rowcount != 1 or await _named_team(db, user.email):
            await db.commit()
            return {"created": False}
        await db.commit()
    try:
        team = await _create_team(user, ad_cookie=ad_cookie, utm_cookie=utm_cookie, referral_cookie=referral_cookie)
    except BaseException:
        async with session_maker() as db:
            await db.execute(update(User).where(User.id == user.id).values(onboarded=False))
            await db.commit()
        raise
    return {"created": True, **team}
