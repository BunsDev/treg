"""Narrow WorkOS transport for enterprise sign-in; no treg authorization decisions here."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlsplit

import httpx

BASE_URL = "https://api.workos.com"


class WorkOSError(Exception):
    pass


def authorization_url(*, client_id: str, organization: str, redirect_uri: str, state: str) -> str:
    return BASE_URL + "/sso/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "organization": organization,
        "redirect_uri": redirect_uri, "state": state,
    })


async def _post(client: httpx.AsyncClient, path: str, key: str, payload: dict) -> dict:
    try:
        response = await client.post(
            BASE_URL + path, json=payload,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise WorkOSError("invalid WorkOS response")
        return data
    except (httpx.HTTPError, ValueError) as exc:
        raise WorkOSError("WorkOS request failed") from exc


async def create_organization(client: httpx.AsyncClient, *, key: str, name: str, org_id: int) -> str:
    data = await _post(client, "/organizations", key, {"name": name, "external_id": str(org_id)})
    value = data.get("id")
    if not isinstance(value, str) or not value.startswith("org_"):
        raise WorkOSError("WorkOS organization missing")
    return value


async def portal_link(client: httpx.AsyncClient, *, key: str, organization: str, intent: str,
                      return_url: str | None = None) -> str:
    if intent not in {"sso", "dsync"}:
        raise ValueError("invalid portal intent")
    payload = {"organization": organization, "intent": intent}
    if return_url:
        payload["return_url"] = return_url
    data = await _post(client, "/portal/generate_link", key, payload)
    value = data.get("link")
    parsed = urlsplit(value) if isinstance(value, str) else None
    if parsed is None or parsed.scheme != "https" or parsed.netloc != "setup.workos.com" or parsed.path not in ("", "/"):
        raise WorkOSError("WorkOS portal link missing")
    return value


async def exchange_code(client: httpx.AsyncClient, *, key: str, client_id: str, code: str) -> dict:
    data = await _post(client, "/sso/token", key, {
        "client_id": client_id, "client_secret": key, "code": code,
        "grant_type": "authorization_code",
    })
    profile = data.get("profile")
    if not isinstance(profile, dict):
        raise WorkOSError("WorkOS profile missing")
    return profile


def verify_event(body: bytes, signature: str, secret: str) -> dict:
    """WorkOS-Signature is t=milliseconds,v1=hex(HMAC(secret, t + '.' + body))."""
    if not secret or len(body) > 1024 * 1024:
        raise WorkOSError("invalid WorkOS webhook")
    try:
        fields = dict(item.strip().split("=", 1) for item in signature.split(","))
        timestamp = fields["t"]
        issued = int(timestamp)
        if abs(time.time() * 1000 - issued) > 5 * 60 * 1000:
            raise ValueError("stale")
        expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(fields["v1"], expected):
            raise ValueError("signature")
        event = json.loads(body)
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise ValueError("payload")
        return event
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise WorkOSError("invalid WorkOS webhook") from exc
