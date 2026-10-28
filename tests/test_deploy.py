"""Vercel deployment guards. Everything here stays offline (SQLite, no Graph, no Neon)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from convoplex.access import PromoCapError, access_record, select_mode
from convoplex.actionlog import GENESIS, DbActionLog, compute_hash
from convoplex.config import Settings
from convoplex.db import init_db, open_db
from convoplex.integrations.images import load_promo_blob
from convoplex.integrations.voice import ffmpeg_available, ffmpeg_binary, ffmpeg_to_wav_command
from convoplex.synthetic.generate import DEFAULT_CUSTOMERS, seed_if_empty
from convoplex.whatsapp_state import WhatsAppStore
from tests.test_api import _app


def _settings(shop, tmp_path, **extra) -> Settings:
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    values = dict(
        db_path=Path(db_file),
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        llm_mode="mock",
    )
    values.update(extra)
    return Settings(**values)


def _log_connect(settings):
    def connect():
        return open_db(settings)

    return connect


def test_db_action_log_links_and_detects_tampering(shop, tmp_path):
    settings = _settings(shop, tmp_path)
    log = DbActionLog(_log_connect(settings), postgres=False)
    assert log.verify().ok is True
    assert log.verify().entries == 0
    first = log.append(
        kind="message",
        actor="system",
        payload={"text": "hello"},
        ts="2026-10-27T00:00:00+00:00",
    )
    second = log.append(
        kind="tool_call",
        actor="agent",
        payload={"name": "get_price"},
        ts="2026-10-27T00:00:01+00:00",
    )
    assert first["prev_hash"] == GENESIS
    assert second["prev_hash"] == first["hash"]
    assert first["hash"] == compute_hash(first)
    assert log.verify().ok is True
    assert log.verify().entries == 2

    conn = open_db(settings)
    try:
        conn.execute(
            "UPDATE action_log SET payload = ? WHERE seq = 1",
            (json.dumps({"text": "rewritten"}, separators=(",", ":")),),
        )
        conn.commit()
    finally:
        conn.close()
    failed = log.verify()
    assert failed.ok is False
    assert "hash mismatch" in (failed.error or "")


def test_seed_if_empty_runs_once(tmp_path):
    conn = init_db(tmp_path / "empty.sqlite")
    try:
        assert seed_if_empty(conn) is True
        count = int(conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"])
        assert count == DEFAULT_CUSTOMERS
        assert seed_if_empty(conn) is False
    finally:
        conn.close()


def test_whatsapp_session_survives_in_the_database(shop, tmp_path):
    settings = _settings(shop, tmp_path)
    store = WhatsAppStore(tmp_path / "ignored.json", connect_fn=_log_connect(settings), postgres=False)
    assert store.claim("abc123") is True
    assert store.claim("abc123") is False
    store.set_demo_customer(1)
    assert store.demo_customer_id() == 1
    assert not (tmp_path / "ignored.json").exists()
    store.reset()
    assert store.demo_customer_id() is None
    assert store.claim("abc123") is True


def test_promo_card_roundtrips_from_the_database_after_the_file_is_gone(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    win = client.post("/v1/customers/1/winback")
    offer_id = win.json()["offer"]["offer_id"]
    card = client.post(f"/v1/offers/{offer_id}/promo-card")
    assert card.status_code == 200
    url = card.json()["url"]
    path = Path(card.json()["path"])
    assert path.is_file()
    path.unlink()
    served = client.get(url)
    assert served.status_code == 200
    assert served.content[:4] == b"RIFF"
    assert served.headers["content-type"].startswith("image/webp")

    settings = _settings(shop, tmp_path)
    conn = open_db(settings)
    try:
        blob = load_promo_blob(conn, offer_id)
    finally:
        conn.close()
    assert blob == served.content


def test_admin_token_locks_mutations_and_leaves_try_it_open(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path, admin_token="s3cret"))
    assert client.get("/v1/admin/session").json() == {"required": True, "admin": False}
    assert client.post("/v1/demo/reset").status_code == 401
    assert client.post("/v1/whatsapp/channel", json={"channel": "simulator"}).status_code == 401
    assert client.post("/v1/whatsapp/customer", json={"customer_id": 1}).status_code == 401
    assert client.post("/v1/customers/1/whatsapp").status_code == 401
    assert client.post("/v1/offers/1/whatsapp").status_code == 401
    assert client.post("/v1/admin/login", json={"token": "wrong"}).status_code == 401
    assert client.post("/v1/customers/1/winback").status_code == 401
    assert client.post("/v1/customers/1/reply", json={"text": "How much last price?"}).status_code == 401
    assert client.post("/v1/customers/1/language", json={"language": "ha"}).status_code == 401
    assert client.post("/v1/offers/1/promo-card").status_code == 401
    assert client.post("/v1/voice/transcribe").status_code == 401

    bearer = {"Authorization": "Bearer s3cret"}
    switched = client.post("/v1/whatsapp/channel", json={"channel": "simulator"}, headers=bearer)
    assert switched.status_code == 200
    header = {"X-Admin-Token": "s3cret"}
    assert client.post("/v1/whatsapp/channel", json={"channel": "simulator"}, headers=header).status_code == 200

    login = client.post("/v1/admin/login", json={"token": "s3cret"})
    assert login.status_code == 200
    assert "convoplex_admin" in login.headers["set-cookie"]
    assert "httponly" in login.headers["set-cookie"].lower()
    assert client.get("/v1/admin/session").json()["admin"] is True
    mapped = client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    assert mapped.status_code == 200
    assert mapped.json()["demo_customer_id"] == 1
    win = client.post("/v1/customers/1/winback")
    assert win.status_code == 200


def test_try_it_limit_is_per_ip_and_webhook_is_not_counted(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path, try_ip_limit=2))
    assert client.post("/v1/customers/1/winback").status_code == 200
    assert client.post("/v1/customers/1/reply", json={"text": "How much last price?"}).status_code == 200
    blocked = client.post("/v1/customers/2/winback")
    assert blocked.status_code == 429

    hook = client.post("/v1/whatsapp/webhook", json={"object": "whatsapp_business_account", "entry": []})
    assert hook.status_code == 200
    assert client.get("/v1/customers/at-risk").status_code == 200


def test_live_cap_falls_back_for_chat_and_blocks_public_promo(shop, tmp_path):
    settings = _settings(shop, tmp_path, llm_mode="live", meta_daily_cap=1, meta_api_key="not-sent")
    assert select_mode(settings, admin=False, task="chat") == "live"
    assert select_mode(settings, admin=False, task="chat") == "mock"
    with pytest.raises(PromoCapError):
        select_mode(settings, admin=False, task="promo")
    assert select_mode(settings, admin=True, task="promo") == "live"

    client = TestClient(_app(shop, tmp_path, llm_mode="live", meta_daily_cap=0, meta_api_key="not-sent"))
    win = client.post("/v1/customers/1/winback")
    assert win.status_code == 200, win.text
    assert win.json()["llm"] == "mock"
    offer_id = win.json()["offer"]["offer_id"]
    promo = client.post(f"/v1/offers/{offer_id}/promo-card")
    assert promo.status_code == 429
    allowed = client.post(
        f"/v1/offers/{offer_id}/promo-card",
        headers={"Authorization": "Bearer admin"},
    )
    # No ADMIN_TOKEN is configured, so the bearer is not an admin and the cap still applies.
    assert allowed.status_code == 429

    admin = TestClient(
        _app(shop, tmp_path, llm_mode="mock", meta_daily_cap=0, admin_token="s3cret")
    )
    drafted = admin.post("/v1/customers/2/winback", headers={"Authorization": "Bearer s3cret"})
    offer_id = drafted.json()["offer"]["offer_id"]
    card = admin.post(
        f"/v1/offers/{offer_id}/promo-card",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert card.status_code == 200
    assert card.json()["llm"] == "mock"


def test_live_webhook_stays_signature_gated_when_admin_token_is_set(shop, tmp_path):
    live = TestClient(
        _app(
            shop,
            tmp_path,
            admin_token="s3cret",
            channel="whatsapp",
            whatsapp_token="token",
            whatsapp_phone_number_id="1150414038145295",
            whatsapp_app_secret="app-secret",
            whatsapp_verify_token="verify-me",
            whatsapp_demo_to="+1 555 000 1111",
        )
    )
    rejected = live.post("/v1/whatsapp/webhook", json={"entry": []})
    assert rejected.status_code == 403
    simulator = TestClient(_app(shop, tmp_path, admin_token="s3cret"))
    accepted = simulator.post("/v1/whatsapp/webhook", json={"entry": []})
    assert accepted.status_code == 200


def test_healthz_security_headers_and_oversized_body(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    healthz = client.get("/healthz")
    assert healthz.status_code == 200
    assert healthz.json() == {"ok": True}
    assert healthz.headers["x-content-type-options"] == "nosniff"
    assert healthz.headers["x-frame-options"] == "DENY"
    assert "unsafe-inline" in healthz.headers["content-security-policy"]
    page = client.get("/admin")
    assert "admin-login" in page.text
    landing = client.get("/")
    assert "View live demo" in landing.text
    assert "admin-login" not in landing.text
    assert "credentials" in page.text

    denied = client.post(
        "/v1/customers/1/reply",
        content=b"{}",
        headers={"content-length": str(21 * 1024 * 1024), "content-type": "application/json"},
    )
    assert denied.status_code == 413


def test_settings_repr_hides_database_url_and_admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:hidden-pass@db.example/app")
    monkeypatch.setenv("META_DAILY_CAP", "12")
    monkeypatch.setenv("TRY_IP_LIMIT", "7")
    settings = Settings.from_env()
    text = repr(settings)
    assert "super-secret-token" not in text
    assert "hidden-pass" not in text
    assert settings.admin_token == "super-secret-token"
    assert settings.database_url.endswith("/app")
    assert settings.meta_daily_cap == 12
    assert settings.try_ip_limit == 7


def test_ffmpeg_resolver_prefers_path_then_the_bundled_binary(monkeypatch):
    monkeypatch.setattr("convoplex.integrations.voice.shutil.which", lambda name: "/usr/bin/ffmpeg")
    assert ffmpeg_binary() == "ffmpeg"
    monkeypatch.setattr("convoplex.integrations.voice.shutil.which", lambda name: None)
    monkeypatch.setattr("convoplex.integrations.voice._bundled_ffmpeg", lambda: "/opt/imageio/ffmpeg")
    assert ffmpeg_binary() == "/opt/imageio/ffmpeg"
    assert ffmpeg_to_wav_command("in.ogg", "out.wav")[0] == "/opt/imageio/ffmpeg"
    monkeypatch.setattr("convoplex.integrations.voice._bundled_ffmpeg", lambda: None)
    assert ffmpeg_available() is False


def test_access_record_has_no_query_string():
    record = access_record(method="GET", path="/v1/whatsapp/webhook", status=200)
    text = json.dumps(record)
    assert "hub.verify_token" not in text
    assert record["path"] == "/v1/whatsapp/webhook"
    assert "query" not in record


def test_vercel_entrypoint_and_duration_are_pinned():
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    assert config["fluid"] is True
    assert config["functions"]["app.py"]["maxDuration"] == 300
    entry = (root / "app.py").read_text(encoding="utf-8")
    assert "create_app()" in entry
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "## Deploy to Vercel" in readme
    assert "https://<project>.vercel.app/v1/whatsapp/webhook" in readme
    assert "1150414038145295" in readme
    assert "Hobby" in readme
    assert "800" in readme
    api = (root / "convoplex" / "api.py").read_text(encoding="utf-8")
    assert "background.add_task" in api
    assert "waitUntil" in api
