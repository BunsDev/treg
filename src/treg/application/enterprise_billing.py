"""Recurring enterprise SSO entitlement; never changes the prepaid call balance."""

from sqlmodel import select

from ..config import get_settings
from ..infra import stripe as stripe_adapter
from ..infra.db import session_maker
from ..models import EnterpriseSSO, Org
from . import enterprise_sso


PLANS = {"sso": 125, "sso_directory": 250}
ACTIVE = {"active", "trialing"}


class EnterpriseBillingError(Exception):
    pass


def catalog() -> list[dict]:
    settings = get_settings()
    names = ("sso",) if settings.enterprise_sso_only_enabled else ()
    return [
        {"id": plan, "usd_monthly": base + settings.enterprise_sso_markup_usd,
         "directory_sync": plan == "sso_directory"}
        for plan, base in PLANS.items() if plan not in names
    ]


async def status(org_id: int) -> dict:
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        return {"configured": bool(get_settings().workos_api_key and get_settings().workos_client_id),
                "billing_configured": bool(get_settings().stripe_secret_key and get_settings().stripe_webhook_secret),
                "plans": catalog(), "plan": row.plan if row else None,
                "subscription_status": row.subscription_status if row else None,
                "sso_ready": bool(row and row.connection_id),
                "directory_ready": bool(row and row.directory_id and row.directory_active),
                "enforced": bool(row and row.enforced),
                "cancel_at_period_end": bool(row and row.cancel_at_period_end)}


async def start_checkout(org_id: int, plan: str, *, base: str, email: str) -> dict:
    settings = get_settings()
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        raise EnterpriseBillingError("Stripe billing is not configured")
    if not settings.workos_api_key or not settings.workos_client_id:
        raise EnterpriseBillingError("WorkOS is not configured")
    if plan not in {item["id"] for item in catalog()}:
        raise EnterpriseBillingError("plan is not available")
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        row = await db.get(EnterpriseSSO, org_id)
        if org is None or org.suspended:
            raise EnterpriseBillingError("team is unavailable")
        if row is not None and row.stripe_subscription_id and row.subscription_status not in {"canceled", "incomplete_expired"}:
            raise EnterpriseBillingError("manage the existing subscription in Billing")
        customer_id, name, slug = org.stripe_customer_id, org.name, org.slug
    if not customer_id:
        customer = await stripe_adapter.call(stripe_adapter.CUSTOMER_CREATE,
            api_key=settings.stripe_secret_key, name=name or slug, email=email,
            metadata={"treg_org_id": str(org_id), "treg_org_slug": slug})
        customer_id = customer["id"]
        async with session_maker() as db:
            org = await db.get(Org, org_id)
            if org is None:
                raise EnterpriseBillingError("team is unavailable")
            if not org.stripe_customer_id:
                org.stripe_customer_id = customer_id
                db.add(org)
                await db.commit()
            else:
                customer_id = org.stripe_customer_id
    metadata = {"treg_kind": "enterprise_sso", "treg_org_id": str(org_id), "treg_plan": plan}
    base = base.rstrip("/")
    session = await stripe_adapter.call(stripe_adapter.CHECKOUT_SESSION_CREATE,
        api_key=settings.stripe_secret_key, mode="subscription", customer=customer_id,
        line_items=[{"quantity": 1, "price_data": {"currency": "usd",
            "unit_amount": (PLANS[plan] + settings.enterprise_sso_markup_usd) * 100,
            "recurring": {"interval": "month"},
            "product_data": {"name": "treg SSO + Directory Sync" if plan == "sso_directory" else "treg SSO"}}}],
        metadata=metadata, subscription_data={"metadata": metadata},
        success_url=f"{base}/app?sso=success#billing",
        cancel_url=f"{base}/app?sso=cancelled#billing")
    return {"url": session["url"]}


async def cancel_at_period_end(org_id: int) -> dict:
    settings = get_settings()
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        if row is None or not row.stripe_subscription_id or row.subscription_status not in ACTIVE:
            raise EnterpriseBillingError("there is no active SSO subscription")
        subscription_id = row.stripe_subscription_id
    if not settings.stripe_secret_key:
        raise EnterpriseBillingError("Stripe billing is not configured")
    result = await stripe_adapter.schedule_subscription_cancel(
        subscription_id, api_key=settings.stripe_secret_key)
    if result.get("id") != subscription_id or not result.get("cancel_at_period_end"):
        raise EnterpriseBillingError("Stripe did not schedule cancellation")
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        if row is not None and row.stripe_subscription_id == subscription_id:
            row.cancel_at_period_end = True
            db.add(row)
            await db.commit()
    return {"cancel_at_period_end": True}


async def handle_event(db, event: dict) -> dict:
    """Called only after the shared Stripe webhook verifies its signature."""
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return {"handled": False, "type": kind}
    meta = obj.get("metadata") or {}
    if meta.get("treg_kind") != "enterprise_sso":
        return {"handled": False, "type": kind}
    try:
        org_id = int(meta["treg_org_id"])
    except (KeyError, ValueError, TypeError):
        return {"handled": False, "type": kind}
    org = await db.get(Org, org_id)
    if org is None or (obj.get("customer") and obj["customer"] != org.stripe_customer_id):
        return {"handled": False, "type": kind}
    row = await db.get(EnterpriseSSO, org_id)
    try:
        created = int(event.get("created") or 0)
    except (TypeError, ValueError):
        created = 0
    if row and created and row.stripe_event_created and created < row.stripe_event_created:
        return {"handled": True, "type": kind, "stale": True}
    if kind == "checkout.session.completed":
        subscription_id = obj.get("subscription")
        plan = meta.get("treg_plan")
        if obj.get("mode") != "subscription" or not subscription_id or plan not in PLANS:
            return {"handled": False, "type": kind}
        if obj.get("payment_status") not in {"paid", "no_payment_required"}:
            return {"handled": False, "type": kind}
        if (row and row.stripe_subscription_id and row.stripe_subscription_id != subscription_id
                and row.subscription_status not in {"canceled", "incomplete_expired"}):
            raise EnterpriseBillingError("multiple SSO subscriptions for one team")
        if row and row.stripe_subscription_id == subscription_id and row.subscription_status == "canceled":
            return {"handled": True, "type": kind, "stale": True}
        row = row or EnterpriseSSO(org_id=org_id)
        row.plan = plan
        row.stripe_subscription_id = subscription_id
        row.subscription_status = "active"
        row.cancel_at_period_end = False
    elif kind in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        subscription_id = obj.get("id")
        plan = meta.get("treg_plan")
        if not subscription_id or plan not in PLANS:
            return {"handled": False, "type": kind}
        if (row and row.stripe_subscription_id and row.stripe_subscription_id != subscription_id
                and row.subscription_status not in {"canceled", "incomplete_expired"}):
            return {"handled": False, "type": kind}
        if row and row.stripe_subscription_id == subscription_id and row.subscription_status == "canceled":
            return {"handled": True, "type": kind, "stale": True}
        row = row or EnterpriseSSO(org_id=org_id)
        row.plan = plan
        row.stripe_subscription_id = subscription_id
        row.subscription_status = "canceled" if kind.endswith(".deleted") else str(obj.get("status") or "incomplete")
        row.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    else:
        return {"handled": False, "type": kind}
    row.stripe_event_created = created
    db.add(row)
    if row.subscription_status in {"canceled", "incomplete_expired"}:
        await enterprise_sso.revoke_team_keys_and_memberships(db, org_id)
    await db.commit()
    return {"handled": True, "type": kind}
