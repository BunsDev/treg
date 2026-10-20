"""Documented WorkOS response shapes exercised without a WorkOS account or network."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlmodel import select

from treg.application import billing, enterprise_billing, enterprise_sso
from treg.domain.identity import session as identity_session
from treg.infra import workos
from treg.infra.db import session_maker
from treg.models import EnterpriseSSO, EnterpriseSSOAudit, Membership, Org, User


def profile(org="org_workos_1", email="employee@example.com"):
    return {"id": "prof_01DMC79VCBZ0NY2099737PSVF1", "organization_id": org,
            "connection_id": "conn_01E4ZCR3C56J083X43JQXF3JK5", "idp_id": "103456789012345678901",
            "email": email, "connection_type": "OktaSAML"}


async def team(clients, *, plan="sso"):
    orgs = (await clients.get("/orgs")).json()
    org_id = orgs[0]["org_id"]
    async with session_maker() as db:
        db.add(EnterpriseSSO(org_id=org_id, plan=plan, subscription_status="active",
                             workos_org_id="org_workos_1"))
        await db.commit()
    return org_id


async def test_sso_profile_must_match_team_and_jit_is_member(clients):
    org_id = await team(clients)
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="another team"):
        await enterprise_sso.finish_signin(org_id, profile(org="org_other"))
    result = await enterprise_sso.finish_signin(org_id, profile())
    assert result.org_id == org_id
    assert (await enterprise_sso.finish_signin(org_id, profile())).user_id == result.user_id
    async with session_maker() as db:
        member = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one()
        assert member.role == "member"
        audit = (await db.execute(select(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.org_id == org_id))).scalars().all()
        assert [row.event for row in audit] == ["sso.signin", "sso.signin"]
        assert all(row.subject_user_id == result.user_id for row in audit)


async def test_owner_can_restore_manually_removed_sso_member(clients):
    org_id = await team(clients)
    result = await enterprise_sso.finish_signin(org_id, profile())
    removed = await clients.delete(f"/orgs/{org_id}/members/{result.user_id}")
    assert removed.status_code == 200
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="deactivated"):
        await enterprise_sso.finish_signin(org_id, profile())
    blocked = await clients.get(f"/orgs/{org_id}/sso/blocked")
    assert blocked.json() == {"members": [{"user_id": result.user_id, "email": "employee@example.com"}]}
    restored = await clients.post(f"/orgs/{org_id}/sso/blocked/{result.user_id}/restore")
    assert restored.status_code == 200
    assert (await enterprise_sso.finish_signin(org_id, profile())).user_id == result.user_id
    assert (await clients.get(f"/orgs/{org_id}/sso/blocked")).json() == {"members": []}
    async with session_maker() as db:
        events = (await db.execute(select(EnterpriseSSOAudit.event).where(
            EnterpriseSSOAudit.org_id == org_id))).scalars().all()
        assert "sso.manual_access_removed" in events
        assert "sso.manual_access_restored" in events


async def test_directory_cannot_restore_a_manually_removed_member(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "create", "event": "dsync.user.created", "data": base})
    result = await enterprise_sso.finish_signin(org_id, profile())
    assert (await clients.delete(f"/orgs/{org_id}/members/{result.user_id}")).status_code == 200
    await enterprise_sso.apply_directory_event({"id": "update", "event": "dsync.user.updated", "data": base})
    await enterprise_sso.reconcile_directory(org_id, "directory_1", [base], 1000)
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="deactivated"):
        await enterprise_sso.finish_signin(org_id, profile())
    assert (await clients.post(f"/orgs/{org_id}/sso/blocked/{result.user_id}/restore")).status_code == 200
    assert (await enterprise_sso.finish_signin(org_id, profile())).user_id == result.user_id


async def test_sso_does_not_link_an_unverified_existing_account(clients):
    org_id = await team(clients)
    async with session_maker() as db:
        db.add(User(email="employee@example.com"))
        await db.commit()
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="verify this existing"):
        await enterprise_sso.finish_signin(org_id, profile())
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "employee@example.com"))).scalar_one()
        assert user.email_verified_at is None
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user.id))).scalar_one_or_none() is None


async def test_company_sso_cookie_cannot_open_another_team_or_mint_its_key(clients):
    org_id = await team(clients)
    result = await enterprise_sso.finish_signin(org_id, profile())
    async with session_maker() as db:
        other = Org(name="Unrelated", slug="unrelated")
        db.add(other)
        await db.flush()
        db.add(Membership(org_id=other.id, user_id=result.user_id, role="member", token_hash=""))
        await db.commit()
        other_id = other.id
    clients.headers.pop("X-Treg-Token")
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(result.user_id, token_version=result.token_version,
                                      sso_org_id=org_id))
    assert {row["org_id"] for row in (await clients.get("/orgs")).json()} == {org_id}
    assert (await clients.get(f"/orgs/{org_id}/balance",
        headers={"X-Treg-Org": str(org_id)})).status_code == 200
    assert (await clients.get(f"/orgs/{other_id}/balance",
        headers={"X-Treg-Org": str(other_id)})).status_code == 403
    key = (await clients.get("/auth/cli-token", headers={"X-Treg-Org": str(org_id)})).json()["token"]
    assert identity_session.read_identity_claims(key)["sso_org_id"] == org_id
    assert (await clients.get(f"/orgs/{other_id}/balance", headers={
        "X-Treg-Token": key, "X-Treg-Org": str(other_id)})).status_code == 403
    assert (await clients.post("/orgs", json={"name": "Another"},
        headers={"X-Treg-Token": key})).status_code == 403
    assert (await clients.get("/auth/cli-token",
        headers={"X-Treg-Org": str(other_id)})).status_code == 403
    assert (await clients.post("/orgs", json={"name": "Another"})).status_code == 403


async def test_company_sso_session_cannot_become_platform_admin(clients):
    org_id = await team(clients)
    result = await enterprise_sso.finish_signin(org_id, profile())
    async with session_maker() as db:
        user = await db.get(User, result.user_id)
        user.is_superadmin = True
        db.add(user)
        await db.commit()
    clients.headers.pop("X-Treg-Token")
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(result.user_id, token_version=result.token_version,
                                      sso_org_id=org_id))
    assert (await clients.get("/admin/stats")).status_code == 403
    key = (await clients.get("/auth/cli-token", headers={"X-Treg-Org": str(org_id)})).json()["token"]
    assert (await clients.get("/admin/stats", headers={"X-Treg-Token": key})).status_code == 403


async def test_role_change_on_sso_team_is_durable_with_old_and_new_role(clients):
    org_id = await team(clients)
    result = await enterprise_sso.finish_signin(org_id, profile())
    response = await clients.patch(f"/orgs/{org_id}/members/{result.user_id}",
        json={"role": "admin"})
    assert response.status_code == 200, response.text
    async with session_maker() as db:
        change = (await db.execute(select(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.org_id == org_id,
            EnterpriseSSOAudit.event == "sso.role_changed"))).scalar_one()
        assert (change.subject_user_id, change.from_role, change.to_role) == (
            result.user_id, "member", "admin")


async def test_sso_email_discovery_routes_but_does_not_join(clients, monkeypatch):
    from treg.config import get_settings
    await team(clients)
    monkeypatch.setattr(get_settings(), "workos_api_key", "sk_test")
    async def find(_client, *, key, domain):
        assert key == "sk_test" and domain == "example.com"
        return [{"id": "org_workos_1"}]
    monkeypatch.setattr(workos, "verified_organizations_for_domain", find)
    result = await clients.post("/auth/sso/discover", json={"email": "employee@example.com"})
    assert result.status_code == 200
    assert len(result.json()["teams"]) == 1
    async with session_maker() as db:
        assert (await db.execute(select(User).where(
            User.email == "employee@example.com"))).scalar_one_or_none() is None


async def test_workos_discovery_uses_verified_org_domain_without_listing_test_idp_connection():
    """WorkOS staging excludes its Test IdP from connection lists; org domains still route."""
    def response(request):
        assert request.url.path == "/organizations"
        assert request.url.params.get("domains[]") == "example.com"
        return httpx.Response(200, json={"data": [
            {"id": "org_verified", "domains": [{"domain": "example.com", "state": "verified"}]},
            {"id": "org_pending", "domains": [{"domain": "example.com", "state": "pending"}]},
        ], "list_metadata": {"after": None}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        found = await workos.verified_organizations_for_domain(client,
            key="sk_test", domain="example.com")
    assert [item["id"] for item in found] == ["org_verified"]


async def test_directory_deactivation_removes_team_access(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com"}
    assert await enterprise_sso.apply_directory_event({"id": "event_activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    assert await enterprise_sso.apply_directory_event({"id": "event_user", "event": "dsync.user.created",
        "data": {**base, "state": "active"}})
    result = await enterprise_sso.finish_signin(org_id, profile())
    event = {"id": "event_delete", "event": "dsync.user.deleted", "data": {**base, "state": "inactive"}}
    assert await enterprise_sso.apply_directory_event(event)
    assert not await enterprise_sso.apply_directory_event(event)
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None
        assert (await db.execute(select(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.org_id == org_id,
            EnterpriseSSOAudit.event == "sso.directory_access_removed"))).scalar_one().subject_user_id == result.user_id
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="not active"):
        await enterprise_sso.finish_signin(org_id, profile())


async def test_reconciliation_repairs_a_missed_directory_removal(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "add", "event": "dsync.user.created", "data": base})
    result = await enterprise_sso.finish_signin(org_id, profile())
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="wrong directory roster"):
        await enterprise_sso.reconcile_directory(org_id, "directory_1", [{**base, "directory_id": "other"}], 1000)
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none()
    repaired = await enterprise_sso.reconcile_directory(org_id, "directory_1", [], 1000)
    assert repaired["memberships_removed"] == 1
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_whole_directory_removal_revokes_members(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "event_activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "event_user", "event": "dsync.user.created", "data": base})
    result = await enterprise_sso.finish_signin(org_id, profile())
    await enterprise_sso.apply_directory_event({"id": "event_directory_delete", "event": "dsync.deleted",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="directory access is inactive"):
        await enterprise_sso.finish_signin(org_id, profile())


async def test_whole_directory_removal_revokes_pre_sync_members(clients):
    org_id = await team(clients, plan="sso_directory")
    result = await enterprise_sso.finish_signin(org_id, profile())
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "delete", "event": "dsync.deleted",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_older_directory_events_cannot_restore_access(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com"}
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "created_at": "2026-01-01T00:00:00Z",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "deleted", "event": "dsync.user.deleted",
        "created_at": "2026-01-03T00:00:00Z", "data": {**base, "state": "inactive"}})
    assert not await enterprise_sso.apply_directory_event({"id": "old-create", "event": "dsync.user.created",
        "created_at": "2026-01-02T00:00:00Z", "data": {**base, "state": "active"}})
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="not active"):
        await enterprise_sso.finish_signin(org_id, profile())


def test_workos_webhook_signature_and_portal_response():
    body = json.dumps({"id": "event_1", "event": "dsync.user.deleted", "data": {}}).encode()
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(b"whsec_test", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert workos.verify_event(body, f"t={timestamp},v1={signature}", "whsec_test")["id"] == "event_1"
    with pytest.raises(workos.WorkOSError):
        workos.verify_event(body + b" ", f"t={timestamp},v1={signature}", "whsec_test")


async def test_workos_portal_accepts_documented_url():
    def handle(request):
        assert request.url.path == "/portal/generate_link"
        return httpx.Response(200, json={"link": "https://setup.workos.com?token=example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        url = await workos.portal_link(client, key="sk_test", organization="org_1", intent="sso")
    assert url == "https://setup.workos.com?token=example"


async def test_workos_portal_accepts_live_staging_url():
    def handle(request):
        return httpx.Response(201, json={"link": "https://setup.workos.com/init?apiToken=example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        url = await workos.portal_link(client, key="sk_test", organization="org_1", intent="sso")
    assert url == "https://setup.workos.com/init?apiToken=example"


async def test_workos_invites_it_contact_with_both_setup_intents():
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/invite"):
            assert json.loads(request.content) == {"intents": ["sso", "directory_sync"]}
            return httpx.Response(204)
        assert json.loads(request.content) == {"email": "it@example.com"}
        return httpx.Response(201, json={"id": "it_contact_123"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await workos.invite_it_contact(client, key="sk_test", organization="org_123",
            email="it@example.com", directory_sync=True)
    assert calls == [("GET", "/organizations/org_123/it_contacts"),
                     ("POST", "/organizations/org_123/it_contacts"),
                     ("POST", "/organizations/org_123/it_contacts/it_contact_123/invite")]


async def test_workos_code_exchange_uses_documented_profile_shape():
    def handle(request):
        assert request.url.path == "/sso/token"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"client_id": "client_test", "client_secret": "sk_test",
                        "code": "code_test", "grant_type": "authorization_code"}
        return httpx.Response(200, json={"access_token": "token_test", "profile": profile()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await workos.exchange_code(client, key="sk_test", client_id="client_test", code="code_test")
    assert result["organization_id"] == "org_workos_1"
    assert result["connection_type"] == "OktaSAML"


def test_catalog_can_offer_bundle_only(monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "enterprise_sso_only_enabled", False)
    assert {item["id"] for item in enterprise_billing.catalog()} == {"sso", "sso_directory"}
    monkeypatch.setattr(settings, "enterprise_sso_only_enabled", True)
    assert [item["id"] for item in enterprise_billing.catalog()] == ["sso_directory"]


async def test_browser_callback_requires_company_proof_for_enforced_team(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    org_id = await team(clients)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        slug = org.slug
    async def exchange(_client, **_kwargs):
        return profile()
    monkeypatch.setattr(workos, "exchange_code", exchange)
    start = await clients.get(f"/auth/sso/{slug}/start")
    assert start.status_code == 303
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await clients.get("/auth/sso/callback", params={"state": state, "code": "code_test"})
    assert callback.status_code == 303
    await enterprise_sso.set_enforced(org_id, True)
    clients.headers.pop("X-Treg-Token")
    sso_access = await clients.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Org": slug})
    assert sso_access.status_code == 200
    sso_key = await clients.get("/auth/cli-token", headers={"X-Treg-Org": slug})
    assert sso_key.status_code == 200
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "employee@example.com"))).scalar_one()
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(user.id, token_version=user.token_version))
    regular_access = await clients.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Org": slug})
    assert regular_access.status_code == 403
    assert "SSO" in regular_access.json()["detail"]
    regular_key = await clients.get("/auth/cli-token", headers={"X-Treg-Org": slug})
    assert regular_key.status_code == 403
    bearer_key = await clients.get("/auth/cli-token", headers={
        "X-Treg-Token": sso_key.json()["token"], "X-Treg-Org": slug})
    assert bearer_key.status_code == 200
    assert identity_session.read_identity_claims(bearer_key.json()["token"])["sso_org_id"] == org_id


async def test_workos_domain_rejection_returns_to_signin_without_raw_error(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    org_id = await team(clients)
    async with session_maker() as db:
        slug = (await db.get(Org, org_id)).slug
    start = await clients.get(f"/auth/sso/{slug}/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await clients.get("/auth/sso/callback", params={
        "state": state,
        "error": "profile_not_allowed_outside_organization",
        "error_description": "Do not show this raw message",
    })
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/?sso_error=domain&team={slug}"
    assert "Do not show this raw message" not in callback.headers["location"]
    assert "treg_sso_state=" in callback.headers["set-cookie"]
    landing = await clients.get(callback.headers["location"])
    assert landing.status_code == 200
    assert 'id="sso-team"' in landing.text
    assert "Company SSO sign-in did not finish" in landing.text


async def test_subscription_webhooks_activate_then_cancel_without_balance_credit(clients):
    orgs = (await clients.get("/orgs")).json()
    org_id = orgs[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.stripe_customer_id = "cus_test"
        db.add(org)
        await db.commit()
    metadata = {"treg_kind": "enterprise_sso", "treg_org_id": str(org_id),
                "treg_plan": "sso_directory"}
    async with session_maker() as db:
        activated = await billing.handle_webhook_event(db, {"type": "customer.subscription.created",
            "created": 100, "data": {"object": {"id": "sub_test", "customer": "cus_test",
            "status": "active", "metadata": metadata}}})
        assert activated["handled"]
        row = await db.get(EnterpriseSSO, org_id)
        assert row.plan == "sso_directory" and row.subscription_status == "active"
        row.workos_org_id = "org_workos_1"
        db.add(row)
        await db.commit()
    result = await enterprise_sso.finish_signin(org_id, profile())
    async with session_maker() as db:
        canceled = await billing.handle_webhook_event(db, {"type": "customer.subscription.deleted",
            "created": 200, "data": {"object": {"id": "sub_test", "customer": "cus_test",
            "metadata": metadata}}})
        assert canceled["handled"]
        stale = await billing.handle_webhook_event(db, {"type": "checkout.session.completed",
            "created": 150, "data": {"object": {"customer": "cus_test", "mode": "subscription",
            "subscription": "sub_test", "payment_status": "paid", "metadata": metadata}}})
        assert stale["stale"]
        row = await db.get(EnterpriseSSO, org_id)
        assert row.subscription_status == "canceled"
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_owner_can_lift_enforcement_after_subscription_ends(clients):
    org_id = await team(clients)
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        row.connection_id = profile()["connection_id"]
        db.add(row)
        await db.commit()
    await enterprise_sso.set_enforced(org_id, True)
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        row.subscription_status = "canceled"
        db.add(row)
        await db.commit()
    assert await enterprise_sso.set_enforced(org_id, False) is False


async def test_checkout_uses_monthly_team_subscription_without_touching_balance(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "enterprise_sso_only_enabled", True)
    monkeypatch.setattr(settings, "enterprise_sso_markup_usd", 0)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    calls = []
    async def fake_call(operation, *, api_key, **kwargs):
        assert api_key == "sk_stripe_test"
        calls.append((operation, kwargs))
        if operation == enterprise_billing.stripe_adapter.CUSTOMER_CREATE:
            return {"id": "cus_test"}
        return {"url": "https://checkout.stripe.com/c/pay/test"}
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", fake_call)
    result = await enterprise_billing.start_checkout(org_id, "sso_directory",
        base="https://treg.example", email="owner@example.com")
    assert result["url"].startswith("https://checkout.stripe.com/")
    checkout = calls[-1][1]
    assert checkout["mode"] == "subscription"
    assert checkout["line_items"][0]["price_data"]["unit_amount"] == 25000
    assert checkout["line_items"][0]["price_data"]["recurring"] == {"interval": "month"}
    assert checkout["subscription_data"]["metadata"]["treg_org_id"] == str(org_id)
