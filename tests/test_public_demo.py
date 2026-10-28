"""Public landing page and the read-only demo. Writes stay behind the admin token."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.test_api import _app

PHONE = "+2348030001101"
RETAILER_PHONE = "+2348000001001"
TOKEN = "super-secret-token"
WHATSAPP = "graph-token-value"


def _public(shop, tmp_path):
    return TestClient(
        _app(
            shop,
            tmp_path,
            admin_token=TOKEN,
            whatsapp_token=WHATSAPP,
            whatsapp_demo_to=PHONE,
            meta_api_key="muse-key-value",
        )
    )


def test_landing_page_is_public_and_the_dashboard_moved(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    landing = client.get("/")
    assert landing.status_code == 200
    assert "text/html" in landing.headers["content-type"]
    text = landing.text
    assert "Convoplex Retention Agent, built in Lagos for Nigerian retailers." in text
    assert "View live demo" in text
    assert 'href="/demo"' in text
    assert "https://github.com/vumoz/convoplex-retention-agent" in text
    assert "muse-spark-1.3" in text
    assert "muse-image-1.0" in text
    assert "muse-voice-transcribe-1.0" in text
    assert "google-analytics" not in text
    assert "gtag" not in text
    assert "<script" not in text.lower()
    assert "admin-login" not in text

    demo = client.get("/demo")
    assert demo.status_code == 200
    assert 'data-mode="demo"' in demo.text
    assert 'body[data-mode="demo"] #composer' in demo.text
    assert 'body[data-mode="demo"] #reset' in demo.text
    assert "READ_ONLY" in demo.text
    assert "/demo/promo-card" in demo.text

    admin = client.get("/admin")
    assert admin.status_code == 200
    assert 'data-mode="admin"' in admin.text
    assert "Reset demo" in admin.text
    assert "Send on WhatsApp" in admin.text
    alias = client.get("/dashboard")
    assert alias.status_code == 200
    assert 'data-mode="admin"' in alias.text


def test_demo_reads_are_synthetic_and_hide_phones_and_secrets(shop, tmp_path):
    client = _public(shop, tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    drafted = client.post("/v1/customers/1/winback", headers=headers)
    assert drafted.status_code == 200

    pages = [
        client.get("/"),
        client.get("/demo"),
        client.get("/admin"),
        client.get("/v1/customers/at-risk"),
        client.get("/v1/customers/1"),
        client.get("/v1/customers/1/thread"),
        client.get("/v1/retailers"),
        client.get("/v1/actions"),
        client.get("/v1/actions/verify"),
        client.get("/v1/metrics"),
        client.get("/v1/whatsapp/status"),
        client.get("/health"),
    ]
    blob = "\n".join(page.text for page in pages)
    for secret in (PHONE, PHONE.lstrip("+"), RETAILER_PHONE, RETAILER_PHONE.lstrip("+"), TOKEN, WHATSAPP, "muse-key-value"):
        assert secret not in blob
    customer = client.get("/v1/customers/1").json()
    assert customer["phone"] == "••01"
    assert customer["synthetic"] is True
    retailer = client.get("/v1/retailers").json()["retailers"][0]
    assert retailer["phone"] == "••01"
    assert "by_language" in client.get("/v1/metrics").json()
    card = client.get("/demo/promo-card")
    assert card.status_code == 200
    assert card.headers["content-type"].startswith("image/webp")
    assert card.content[:4] == b"RIFF"


def test_write_routes_require_the_admin_token(shop, tmp_path):
    client = _public(shop, tmp_path)
    before = client.get("/v1/actions").json()["count"]
    writes = [
        ("/v1/demo/reset", None),
        ("/v1/whatsapp/channel", {"channel": "simulator"}),
        ("/v1/whatsapp/customer", {"customer_id": 1}),
        ("/v1/customers/1/whatsapp", None),
        ("/v1/offers/1/whatsapp", None),
        ("/v1/customers/1/winback", None),
        ("/v1/customers/1/reply", {"text": "How much last price?"}),
        ("/v1/customers/1/language", {"language": "yo"}),
        ("/v1/offers/1/promo-card", None),
        ("/v1/voice/transcribe", None),
    ]
    for path, body in writes:
        response = client.post(path, json=body) if body is not None else client.post(path)
        assert response.status_code == 401, path
        assert TOKEN not in response.text
    assert client.get("/v1/actions").json()["count"] == before

    # The webhook is Meta's callback, gated by signature, not the admin token.
    hook = client.post("/v1/whatsapp/webhook", json={"entry": []})
    assert hook.status_code == 200

    verified = client.get("/v1/actions/verify")
    assert verified.status_code == 200
    assert verified.json()["ok"] is True
    assert client.get("/v1/actions").json()["count"] == before

    opened = client.post("/v1/admin/login", json={"token": TOKEN})
    assert opened.status_code == 200
    sent = client.post("/v1/customers/1/winback")
    assert sent.status_code == 200
    assert json.dumps(sent.json()).find(PHONE) == -1
