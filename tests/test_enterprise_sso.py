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
from treg.models import EnterpriseSSO, Membership, Org, User


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
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="not active"):
        await enterprise_sso.finish_signin(org_id, profile())


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
    assert bearer_key.status_code == 403


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
