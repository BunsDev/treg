"""Team SSO purchase, setup, sign-in and signed directory event routes."""

import json
import secrets
import time

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import select

from .. import crypto
from ..application import enterprise_billing, enterprise_sso
from ..config import get_settings
from ..domain.identity import session as identity_session
from ..domain.identity.access import Caller, require_member
from ..infra import workos
from ..infra.db import session_maker
from ..models import Org
from .auth_helpers import _is_https, _same_origin
from .billing import _return_base


router = APIRouter()
STATE_COOKIE = "treg_sso_state"


class CheckoutIn(BaseModel):
    plan: str


class EnforceIn(BaseModel):
    enabled: bool


def _owner(caller: Caller, org_id: int) -> None:
    if caller.org_id != org_id or caller.role != "owner":
        raise HTTPException(403, "team owner required")


@router.get("/orgs/{org_id}/sso")
async def sso_status(org_id: int, caller: Caller = Depends(require_member)) -> dict:
    if caller.org_id != org_id:
        raise HTTPException(403, "team membership required")
    return await enterprise_billing.status(org_id)


@router.post("/orgs/{org_id}/sso/checkout")
async def sso_checkout(org_id: int, body: CheckoutIn, request: Request,
                       caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        return await enterprise_billing.start_checkout(org_id, body.plan,
            base=_return_base(request), email=caller.email)
    except enterprise_billing.EnterpriseBillingError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/orgs/{org_id}/sso/setup/{intent}")
async def sso_setup(org_id: int, intent: str, request: Request,
                    caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    settings = get_settings()
    if not settings.workos_api_key:
        raise HTTPException(503, "WorkOS is not configured")
    try:
        org, config = await enterprise_sso.configured_team(org_id)
        if intent not in {"sso", "dsync"} or (intent == "dsync" and config.plan != "sso_directory"):
            raise HTTPException(422, "setup option is unavailable")
        workos_org_id = config.workos_org_id
        async with httpx.AsyncClient() as client:
            if not workos_org_id:
                workos_org_id = await workos.create_organization(client,
                    key=settings.workos_api_key, name=org.name, org_id=org_id)
                await enterprise_sso.bind_workos_org(org_id, workos_org_id)
            link = await workos.portal_link(client, key=settings.workos_api_key,
                organization=workos_org_id, intent=intent,
                return_url=settings.public_url.rstrip("/") + "/app#sso")
        return {"url": link}
    except (enterprise_sso.EnterpriseSSOError, workos.WorkOSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/auth/sso/{org_slug}/start")
async def sso_start(org_slug: str, request: Request):
    settings = get_settings()
    if not settings.workos_api_key or not settings.workos_client_id:
        raise HTTPException(503, "SSO is not configured")
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.slug == org_slug))).scalar_one_or_none()
        if org is None:
            raise HTTPException(404, "team not found")
        org_id = org.id
    try:
        _, config = await enterprise_sso.configured_team(org_id)
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not config.workos_org_id:
        raise HTTPException(409, "SSO setup is incomplete")
    nonce = secrets.token_urlsafe(32)
    cookie = crypto.encrypt(json.dumps({"org_id": org_id, "nonce": nonce,
                                        "expires": int(time.time()) + 600}))
    callback = settings.public_url.rstrip("/") + "/auth/sso/callback"
    url = workos.authorization_url(client_id=settings.workos_client_id,
        organization=config.workos_org_id, redirect_uri=callback, state=nonce)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(STATE_COOKIE, cookie, httponly=True, secure=_is_https(request),
                        samesite="lax", max_age=600)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/sso/callback")
async def sso_callback(request: Request, code: str = "", state: str = ""):
    cookie = request.cookies.get(STATE_COOKIE, "")
    try:
        saved = json.loads(crypto.decrypt(cookie))
        if not state or not secrets.compare_digest(state, saved["nonce"]):
            raise ValueError("state")
        if int(saved["expires"]) < time.time():
            raise ValueError("expired")
        org_id = int(saved["org_id"])
    except (InvalidToken, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "SSO sign-in expired; try again") from exc
    if not code:
        raise HTTPException(400, "SSO sign-in was not completed")
    settings = get_settings()
    try:
        async with httpx.AsyncClient() as client:
            profile = await workos.exchange_code(client, key=settings.workos_api_key,
                                                 client_id=settings.workos_client_id, code=code)
        result = await enterprise_sso.finish_signin(org_id, profile)
    except (workos.WorkOSError, enterprise_sso.EnterpriseSSOError) as exc:
        raise HTTPException(403, str(exc)) from exc
    response = RedirectResponse("/app?sso=success", status_code=303)
    response.delete_cookie(STATE_COOKIE)
    response.set_cookie(identity_session.COOKIE,
        identity_session.make_session(result.user_id, token_version=result.token_version,
                                      sso_org_id=result.org_id),
        httponly=True, secure=_is_https(request), samesite="lax",
        max_age=identity_session.TTL_SECONDS)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/orgs/{org_id}/sso/enforce")
async def sso_enforce(org_id: int, body: EnforceIn, request: Request,
                      caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        return {"enforced": await enterprise_sso.set_enforced(org_id, body.enabled)}
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/orgs/{org_id}/sso/cancel")
async def sso_cancel(org_id: int, request: Request,
                     caller: Caller = Depends(require_member)) -> dict:
    _owner(caller, org_id)
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        return await enterprise_billing.cancel_at_period_end(org_id)
    except enterprise_billing.EnterpriseBillingError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/auth/sso/workos/webhook", include_in_schema=False)
async def sso_workos_webhook(request: Request) -> dict:
    secret = get_settings().workos_webhook_secret
    if not secret:
        raise HTTPException(404)
    try:
        event = workos.verify_event(await request.body(),
                                    request.headers.get("workos-signature", ""), secret)
        return {"received": True, "processed": await enterprise_sso.apply_directory_event(event)}
    except workos.WorkOSError as exc:
        raise HTTPException(400, "invalid WorkOS webhook") from exc
    except enterprise_sso.EnterpriseSSOError as exc:
        raise HTTPException(422, str(exc)) from exc
