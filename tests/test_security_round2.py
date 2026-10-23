"""Round-2 security fixes — proxy SSRF guard (B) + anonymous front door (C)."""
from __future__ import annotations

from httpx import AsyncClient


# --- B: no SSRF via base_url ------------------------------------------------------------------
async def test_base_url_rejects_internal_hosts(clients: AsyncClient):
    for bad in ("http://169.254.169.254/latest/meta-data/", "http://localhost/x",
                "http://127.0.0.1:8080", "http://10.0.0.1", "http://foo.internal",
                "http://2130706433/"):  # 127.0.0.1 as one decimal number
        r = await clients.post("/tools", json={"name": "ssrf", "base_url": bad})
        assert r.status_code == 422, f"{bad} should be refused, got {r.status_code}"
    ok = await clients.post("/tools", json={"name": "okpub", "base_url": "https://api.stripe.com/v1"})
    assert ok.status_code == 200, ok.text


# --- C2: install.sh token can't inject shell --------------------------------------------------
async def test_install_sh_rejects_injection_token(clients: AsyncClient):
    name = "stripe-billing"  # a real sample skill
    bad = await clients.get(f"/skills/{name}/install.sh", params={"token": "\nTREG_SKILL_EOF\nrm -rf ~\n"})
    assert bad.status_code == 422
    good = await clients.get(f"/skills/{name}/install.sh", params={"token": "sk_test_abc123"})
    assert good.status_code == 200 and "TREG_SKILL_EOF\nrm" not in good.text
