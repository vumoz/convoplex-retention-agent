"""WhatsApp Cloud API channel. Every test uses a fake transport."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import wave
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from convoplex.__main__ import main
from convoplex.config import Settings
from convoplex.integrations.whatsapp import (
    SendBlocked,
    WhatsAppClient,
    WhatsAppError,
    build_media_metadata_url,
    build_send_image_request,
    build_send_template_request,
    build_send_text_request,
    build_upload_media_request,
    mask_phone,
    normalize_phone,
    parse_statuses,
    phone_base64,
    signature_matches,
    wamid_hash,
    wamid_tail,
    webp_to_jpeg,
)
from convoplex.tunnel import parse_quick_tunnel_url, start_quick_tunnel
from convoplex.whatsapp_state import WhatsAppStore, prefer_status, window_open
from tests.test_api import _app

TOKEN = "super-secret-token"
SECRET = "app-secret-value"
DEMO = "+1 555 000 1111"
DEMO_DIGITS = "15550001111"
SYNTHETIC = "2348030001101"
PHONE_ID = "123456789"


def test_request_shapes_cover_text_template_image_and_media():
    text = build_send_text_request(PHONE_ID, DEMO_DIGITS, "Hello", version="v22.0")
    assert text["url"] == "https://graph.facebook.com/v22.0/123456789/messages"
    assert text["json"]["messaging_product"] == "whatsapp"
    assert text["json"]["type"] == "text"
    assert text["json"]["text"]["body"] == "Hello"
    assert text["json"]["to"] == DEMO_DIGITS

    template = build_send_template_request(PHONE_ID, DEMO_DIGITS)
    assert template["json"]["type"] == "template"
    assert template["json"]["template"] == {"name": "hello_world", "language": {"code": "en_US"}}

    image = build_send_image_request(PHONE_ID, DEMO_DIGITS, "media-1", "Rice · ₦70,200")
    assert image["json"]["type"] == "image"
    assert image["json"]["image"] == {"id": "media-1", "caption": "Rice · ₦70,200"}

    upload = build_upload_media_request(PHONE_ID, version="v22.0")
    assert upload["url"] == "https://graph.facebook.com/v22.0/123456789/media"
    assert upload["data"]["messaging_product"] == "whatsapp"
    assert upload["content_type"] == "image/jpeg"
    assert build_media_metadata_url("media-9") == "https://graph.facebook.com/v21.0/media-9"
    assert normalize_phone(DEMO) == DEMO_DIGITS
    assert mask_phone(DEMO) == "••11"
    assert DEMO_DIGITS not in mask_phone(DEMO)


def test_allow_list_refuses_simulator_and_synthetic_numbers(tmp_path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    blocked = WhatsAppClient(_settings(tmp_path, channel="simulator"), channel="simulator", http=http)
    with pytest.raises(SendBlocked):
        blocked.send_text(DEMO, "Hello")
    assert calls == []

    live = WhatsAppClient(_settings(tmp_path), channel="whatsapp", http=http)
    with pytest.raises(SendBlocked):
        live.send_text(SYNTHETIC, "Hello")
    with pytest.raises(SendBlocked):
        live.send_template(SYNTHETIC)
    assert calls == []

    sent = live.send_text(DEMO, "Hello from Convoplex")
    assert sent.to == DEMO_DIGITS
    assert sent.wamid_hash == wamid_hash("wamid.OUT")
    assert sent.wamid_tail == wamid_tail("wamid.OUT")
    assert "wamid.OUT" not in sent.wamid_hash
    body = json.loads(calls[0].content)
    assert body["to"] == DEMO_DIGITS
    assert SYNTHETIC not in calls[0].content.decode()
    assert calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(calls[0].url)


def test_media_download_sends_the_bearer_twice_and_rejects_other_hosts(tmp_path):
    calls: list[httpx.Request] = []
    wav = _tiny_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/media-9"):
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
                    "mime_type": "audio/wav",
                    "id": "media-9",
                },
            )
        if request.url.host == "lookaside.fbsbx.com":
            return httpx.Response(200, content=wav)
        raise AssertionError(f"unexpected {request.url}")

    client = WhatsAppClient(_settings(tmp_path), channel="whatsapp", http=httpx.Client(transport=httpx.MockTransport(handler)))
    data, mime = client.download_media("media-9")
    assert data == wav
    assert mime == "audio/wav"
    assert len(calls) == 2

    def evil(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media-9"):
            return httpx.Response(200, json={"url": "https://evil.example/steal", "mime_type": "audio/ogg"})
        raise AssertionError("bearer token was sent to an unexpected host")

    evil_client = WhatsAppClient(
        _settings(tmp_path),
        channel="whatsapp",
        http=httpx.Client(transport=httpx.MockTransport(evil)),
    )
    with pytest.raises(WhatsAppError):
        evil_client.download_media("media-9")


def test_window_logic_and_status_rank():
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    assert window_open(None, now) is False
    assert window_open("2026-10-27T11:30:00+00:00", now) is True
    assert window_open("2026-10-26T12:00:00+00:00", now) is False
    assert window_open("2026-10-26T12:00:01+00:00", now) is True
    assert prefer_status("sent", "delivered") == "delivered"
    assert prefer_status("read", "sent") == "read"
    assert prefer_status("delivered", "failed") == "failed"
    assert prefer_status("failed", "read") == "failed"


def test_signature_verification_and_settings_hide_secrets(monkeypatch):
    body = b'{"entry":[]}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert signature_matches(body, f"sha256={digest}", SECRET) is True
    assert signature_matches(body, "sha256=deadbeef", SECRET) is False
    assert signature_matches(body, None, SECRET) is False
    assert signature_matches(body, f"sha256={digest}", None) is False
    assert signature_matches(body, None, None) is False

    monkeypatch.setenv("WHATSAPP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_DEMO_TO", DEMO)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("GRAPH_API_VERSION", "v22.0")
    monkeypatch.setenv("CONVOPLEX_CHANNEL", "whatsapp")
    settings = Settings.from_env()
    assert settings.graph_api_version == "v22.0"
    assert settings.channel == "whatsapp"
    assert settings.whatsapp_token == TOKEN
    text = repr(settings)
    assert TOKEN not in text
    assert SECRET not in text
    assert DEMO_DIGITS not in text
    assert "verify-me" not in text

    monkeypatch.delenv("WHATSAPP_TOKEN")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "legacy-token")
    assert Settings.from_env().whatsapp_token == "legacy-token"
    monkeypatch.setenv("GRAPH_API_VERSION", "v21.0/../x")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("GRAPH_API_VERSION", "v21.0")
    monkeypatch.setenv("CONVOPLEX_CHANNEL", "sms")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_status_payload_parses_delivery_ticks():
    updates = parse_statuses(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.OUT",
                                        "status": "delivered",
                                        "timestamp": "1710000000",
                                        "recipient_id": DEMO_DIGITS,
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert updates[0].message_id == "wamid.OUT"
    assert updates[0].status == "delivered"
    assert updates[0].recipient == DEMO_DIGITS


def test_simulator_send_does_not_call_graph(shop, tmp_path):
    calls: list[httpx.Request] = []
    client = TestClient(_live_app(shop, tmp_path, calls, channel="simulator"))
    response = client.post("/v1/customers/1/whatsapp", json={"to": SYNTHETIC})
    assert response.status_code == 409
    assert "Nothing was sent" in response.json()["detail"]
    assert calls == []
    assert TOKEN not in response.text
    status = client.get("/v1/whatsapp/status").json()
    assert status["channel"] == "simulator"
    assert status["demo_to_masked"] == "••11"
    assert DEMO_DIGITS not in client.get("/v1/whatsapp/status").text
    assert TOKEN not in client.get("/v1/whatsapp/status").text


def test_closed_window_sends_hello_world_and_open_window_sends_text(shop, tmp_path):
    calls: list[httpx.Request] = []
    client = TestClient(_live_app(shop, tmp_path, calls))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    closed = client.post("/v1/customers/1/whatsapp", json={"to": SYNTHETIC})
    assert closed.status_code == 200
    body = closed.json()
    assert body["sent"] == "template"
    assert body["to_masked"] == "••11"
    assert "24-hour" in body["note"]
    assert "hello_world" in body["note"]
    sent = json.loads(calls[0].content)
    assert sent["type"] == "template"
    assert sent["template"]["name"] == "hello_world"
    assert sent["template"]["language"]["code"] == "en_US"
    assert sent["to"] == DEMO_DIGITS
    assert SYNTHETIC not in calls[0].content.decode()
    thread = client.get("/v1/customers/1/thread").json()
    shop_message = next(item for item in thread["messages"] if item["role"] == "shop")
    assert shop_message["whatsapp"]["kind"] == "template"
    assert shop_message["whatsapp"]["status"] == "sent"
    assert "24-hour" in shop_message["whatsapp"]["note"]

    WhatsAppStore(tmp_path / "output" / "whatsapp_state.json").note_inbound(datetime.now(timezone.utc))
    opened = client.post("/v1/customers/1/whatsapp")
    assert opened.status_code == 200
    assert opened.json()["sent"] == "text"
    text = json.loads(calls[1].content)
    assert text["type"] == "text"
    assert text["to"] == DEMO_DIGITS
    assert text["text"]["body"]
    assert SYNTHETIC not in calls[1].content.decode()
    log_text = (tmp_path / "actions.jsonl").read_text()
    assert TOKEN not in log_text
    assert DEMO_DIGITS not in log_text
    assert "customer_id" in log_text
    assert client.post("/v1/actions/verify").json()["ok"] is True


def test_inbound_text_is_deduped_and_replied_only_to_the_demo_phone(shop, tmp_path):
    calls: list[httpx.Request] = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/customers/1/winback")
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    payload = _inbound("wamid.IN.1", DEMO_DIGITS, "How much last price?")
    raw = json.dumps(payload).encode()
    bad = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=nope"},
    )
    assert bad.status_code == 403
    assert calls == []

    first = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert first.status_code == 200
    assert first.json()["processed"] is True
    assert first.json()["accepted"] == [wamid_tail("wamid.IN.1")]
    second = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert second.status_code == 200
    assert second.json()["processed"] is False
    assert second.json()["duplicates"] == [wamid_tail("wamid.IN.1")]

    stranger = _inbound("wamid.IN.2", SYNTHETIC, "Yes o")
    stranger_raw = json.dumps(stranger).encode()
    ignored = client.post(
        "/v1/whatsapp/webhook",
        content=stranger_raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(stranger_raw)},
    )
    assert ignored.json()["ignored"] == 1
    assert ignored.json()["processed"] is False

    assert len(calls) == 1
    outbound = json.loads(calls[0].content)
    assert outbound["type"] == "text"
    assert outbound["to"] == DEMO_DIGITS
    assert SYNTHETIC not in calls[0].content.decode()
    thread = client.get("/v1/customers/1/thread").json()
    roles = [item["role"] for item in thread["messages"]]
    assert roles.count("customer") == 1
    assert any(item["text"] == "How much last price?" for item in thread["messages"])
    log_text = (tmp_path / "actions.jsonl").read_text()
    assert TOKEN not in log_text
    assert SECRET not in log_text
    assert DEMO_DIGITS not in log_text
    assert '"customer_id":1' in log_text.replace(" ", "")


def test_inbound_audio_is_transcribed_and_answered(shop, tmp_path):
    calls: list[httpx.Request] = []
    wav = _tiny_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/media-9"):
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=9",
                    "mime_type": "audio/wav",
                },
            )
        if request.url.host == "lookaside.fbsbx.com":
            return httpx.Response(200, content=wav)
        if request.url.path.endswith("/messages"):
            body = json.loads(request.content)
            assert body["to"] == DEMO_DIGITS
            assert body["type"] == "text"
            assert SYNTHETIC not in request.content.decode()
            return httpx.Response(200, json={"messages": [{"id": "wamid.AUDIO.OUT"}]})
        raise AssertionError(str(request.url))

    client = TestClient(
        _app(
            shop,
            tmp_path,
            channel="whatsapp",
            whatsapp_token=TOKEN,
            whatsapp_phone_number_id=PHONE_ID,
            whatsapp_verify_token="verify-me",
            whatsapp_app_secret=SECRET,
            whatsapp_demo_to=DEMO,
            whatsapp_http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )
    client.post("/v1/customers/1/winback")
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.AUDIO",
                                    "from": DEMO_DIGITS,
                                    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                    "type": "audio",
                                    "audio": {"id": "media-9", "mime_type": "audio/wav"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw = json.dumps(payload).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    assert response.json()["processed"] is True
    hosts = [call.url.host for call in calls]
    assert hosts[0] == "graph.facebook.com"
    assert hosts[1] == "lookaside.fbsbx.com"
    assert calls[2].url.path.endswith("/messages")
    logged = client.get("/v1/actions").json()["entries"]
    assert any(entry["kind"] == "model_call" and entry["payload"].get("task") == "transcribe" for entry in logged)
    thread = client.get("/v1/customers/1/thread").json()
    assert any(item.get("voice") for item in thread["messages"])
    assert any(item.get("whatsapp", {}) and item["whatsapp"].get("kind") == "text" for item in thread["messages"] if item["role"] == "shop")


def test_status_webhooks_show_ticks_and_do_not_move_backwards(shop, tmp_path):
    calls: list[httpx.Request] = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    WhatsAppStore(tmp_path / "output" / "whatsapp_state.json").note_inbound(datetime.now(timezone.utc))
    sent = client.post("/v1/customers/1/whatsapp")
    raw_id = "wamid.TEXT"
    assert sent.json()["wamid_hash"] == wamid_hash(raw_id)
    assert sent.json()["wamid_tail"] == wamid_tail(raw_id)
    assert raw_id not in sent.text
    assert client.get("/v1/customers/1/thread").json()["messages"][-1]["whatsapp"]["status"] == "sent"

    def post_status(status: str) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {"id": raw_id, "status": status, "recipient_id": DEMO_DIGITS, "timestamp": "1"}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        raw = json.dumps(payload).encode()
        response = client.post(
            "/v1/whatsapp/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
        )
        assert response.status_code == 200

    post_status("delivered")
    assert client.get("/v1/customers/1/thread").json()["messages"][-1]["whatsapp"]["status"] == "delivered"
    assert client.get("/v1/whatsapp/status").json()["last_status"] == "delivered"
    post_status("read")
    assert client.get("/v1/customers/1/thread").json()["messages"][-1]["whatsapp"]["status"] == "read"
    post_status("sent")
    assert client.get("/v1/customers/1/thread").json()["messages"][-1]["whatsapp"]["status"] == "read"
    log_text = (tmp_path / "actions.jsonl").read_text()
    assert DEMO_DIGITS not in log_text
    assert TOKEN not in log_text


def test_promo_card_uploads_jpeg_inside_the_window_and_uses_the_template_outside(shop, tmp_path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            body = request.content
            assert b"image/jpeg" in body
            assert b"promo.jpg" in body
            assert b"messaging_product" in body
            assert b"\xff\xd8" in body
            assert SYNTHETIC.encode() not in body
            return httpx.Response(200, json={"id": "uploaded-1"})
        body = json.loads(request.content)
        assert body["to"] == DEMO_DIGITS
        if body["type"] == "template":
            return httpx.Response(200, json={"messages": [{"id": "wamid.TPL"}]})
        assert body["type"] == "image"
        assert body["image"]["id"] == "uploaded-1"
        assert body["image"]["caption"]
        return httpx.Response(200, json={"messages": [{"id": "wamid.IMG"}]})

    client = TestClient(
        _app(
            shop,
            tmp_path,
            channel="whatsapp",
            whatsapp_token=TOKEN,
            whatsapp_phone_number_id=PHONE_ID,
            whatsapp_verify_token="verify-me",
            whatsapp_demo_to=DEMO,
            whatsapp_http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )
    win = client.post("/v1/customers/1/winback")
    offer_id = win.json()["offer"]["offer_id"]
    missing = client.post(f"/v1/offers/{offer_id}/whatsapp")
    assert missing.status_code == 409
    assert calls == []

    folder = tmp_path / "output" / "promo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"offer-{offer_id}.webp").write_bytes(_tiny_webp())
    closed = client.post(f"/v1/offers/{offer_id}/whatsapp")
    assert closed.status_code == 200
    assert closed.json()["sent"] == "template"
    assert "promo card" in closed.json()["note"]
    assert json.loads(calls[0].content)["type"] == "template"

    WhatsAppStore(tmp_path / "output" / "whatsapp_state.json").note_inbound(
        datetime.now(timezone.utc) - timedelta(hours=1)
    )
    opened = client.post(f"/v1/offers/{offer_id}/whatsapp")
    assert opened.status_code == 200
    assert opened.json()["sent"] == "image"
    assert opened.json()["to_masked"] == "••11"
    assert calls[1].url.path.endswith("/media")
    assert json.loads(calls[2].content)["type"] == "image"
    assert TOKEN not in (tmp_path / "actions.jsonl").read_text()
    jpeg = webp_to_jpeg(_tiny_webp())
    assert jpeg.startswith(b"\xff\xd8")


def test_cli_check_send_template_customer_and_serve_banner(shop, tmp_path, monkeypatch, capsys):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("CONVOPLEX_DB", db_file)
    monkeypatch.setenv("CONVOPLEX_LOG", str(tmp_path / "actions.jsonl"))
    monkeypatch.setenv("CONVOPLEX_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("WHATSAPP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_DEMO_TO", DEMO)
    monkeypatch.setenv("CONVOPLEX_CHANNEL", "simulator")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert request.url.params["fields"] == "display_phone_number,verified_name,quality_rating"
        return httpx.Response(
            200,
            json={
                "display_phone_number": "+1 555-0199",
                "verified_name": "Convoplex Test",
                "quality_rating": "GREEN",
            },
        )

    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.default_graph_http",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert main(["whatsapp", "check"]) == 0
    checked = capsys.readouterr().out
    assert "Display number: +1 555-0199" in checked
    assert "Quality: GREEN" in checked
    assert "Verified name: Convoplex Test" in checked
    assert TOKEN not in checked

    assert main(["whatsapp", "send-template"]) == 1
    refused = capsys.readouterr().err
    assert "CONVOPLEX_CHANNEL" in refused
    assert TOKEN not in refused

    monkeypatch.setenv("CONVOPLEX_CHANNEL", "whatsapp")

    def send_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["type"] == "template"
        assert body["template"]["name"] == "hello_world"
        assert body["to"] == DEMO_DIGITS
        return httpx.Response(200, json={"messages": [{"id": "wamid.HELLO"}]})

    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.default_graph_http",
        lambda: httpx.Client(transport=httpx.MockTransport(send_handler)),
    )
    assert main(["whatsapp", "send-template"]) == 0
    sent = capsys.readouterr().out
    assert "hello_world" in sent
    assert "••11" in sent
    assert wamid_tail("wamid.HELLO") in sent
    assert "wamid.HELLO" not in sent
    assert DEMO_DIGITS not in sent
    assert TOKEN not in sent

    assert main(["whatsapp", "customer", "1"]) == 0
    assert WhatsAppStore(tmp_path / "output" / "whatsapp_state.json").demo_customer_id() == 1
    assert main(["whatsapp", "customer", "99"]) == 1

    captured = {}

    def fake_create(settings, generate_if_missing=True):
        app = FastAPI()
        app.state.channel = settings.channel
        app.state.settings = settings
        captured["channel"] = settings.channel
        return app

    monkeypatch.setattr("convoplex.api.create_app", fake_create)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: captured.update(host=host, port=port))
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    assert main(["serve", "--channel", "whatsapp", "--port", "8765"]) == 0
    banner = capsys.readouterr().out
    assert captured["channel"] == "whatsapp"
    assert captured["port"] == 8765
    assert "http://127.0.0.1:8765/v1/whatsapp/webhook" in banner
    assert "verify-me" in banner
    assert "messages" in banner
    assert "cloudflared" in banner
    assert TOKEN not in banner

    monkeypatch.setattr(
        "convoplex.__main__.start_quick_tunnel",
        lambda port: (None, "https://demo.trycloudflare.com"),
    )
    assert main(["serve", "--channel", "whatsapp", "--tunnel", "--port", "8765"]) == 0
    public = capsys.readouterr().out
    assert "https://demo.trycloudflare.com/v1/whatsapp/webhook" in public
    assert "verify-me" in public
    assert TOKEN not in public

    monkeypatch.setenv("CONVOPLEX_CHANNEL", "simulator")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    assert main(["serve", "--port", "8766"]) == 0
    assert "verify-me" not in capsys.readouterr().out


def test_live_serve_refuses_to_start_without_app_secret(shop, tmp_path, monkeypatch, capsys):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("CONVOPLEX_DB", db_file)
    monkeypatch.setenv("CONVOPLEX_LOG", str(tmp_path / "actions.jsonl"))
    monkeypatch.setenv("CONVOPLEX_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("WHATSAPP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_DEMO_TO", DEMO)
    monkeypatch.setenv("CONVOPLEX_CHANNEL", "whatsapp")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)

    started = {"tunnel": 0, "app": 0}

    def fail_tunnel(port):
        started["tunnel"] += 1
        return None, "https://should-not-start.trycloudflare.com"

    def fail_app(settings, generate_if_missing=True):
        started["app"] += 1
        raise AssertionError("create_app must not run without WHATSAPP_APP_SECRET")

    monkeypatch.setattr("convoplex.__main__.start_quick_tunnel", fail_tunnel)
    monkeypatch.setattr("convoplex.api.create_app", fail_app)

    assert main(["serve", "--channel", "whatsapp", "--tunnel", "--port", "8765"]) == 1
    refused = capsys.readouterr()
    assert "WHATSAPP_APP_SECRET" in refused.err
    assert "Refusing to start" in refused.err
    assert TOKEN not in refused.err
    assert SECRET not in refused.err
    assert started == {"tunnel": 0, "app": 0}
    assert "trycloudflare" not in refused.out

    assert main(["serve", "--tunnel"]) == 1
    again = capsys.readouterr().err
    assert "WHATSAPP_APP_SECRET" in again
    assert started == {"tunnel": 0, "app": 0}

    monkeypatch.setenv("CONVOPLEX_CHANNEL", "simulator")
    import uvicorn

    def fake_create(settings, generate_if_missing=True):
        started["app"] += 1
        app = FastAPI()
        app.state.channel = settings.channel
        return app

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: None)
    monkeypatch.setattr("convoplex.api.create_app", fake_create)
    monkeypatch.setattr(
        "convoplex.__main__.start_quick_tunnel",
        lambda port: started.__setitem__("tunnel", started["tunnel"] + 1) or (None, "https://sim.trycloudflare.com"),
    )
    assert main(["serve", "--tunnel", "--port", "8766"]) == 0
    simulator = capsys.readouterr().out
    assert "https://sim.trycloudflare.com/v1/whatsapp/webhook" in simulator
    assert started["tunnel"] == 1
    assert started["app"] == 1


def test_live_webhook_rejects_unsigned_posts_and_simulator_does_not(shop, tmp_path):
    calls: list[httpx.Request] = []
    live = TestClient(_live_app(shop, tmp_path, calls))
    status = live.get("/v1/whatsapp/status").json()
    assert status["signature_required"] is True
    assert "WHATSAPP_APP_SECRET" in status["missing"]
    raw = json.dumps(_inbound("wamid.FAKE", DEMO_DIGITS, "forged")).encode()
    unsigned = live.post("/v1/whatsapp/webhook", content=raw, headers={"Content-Type": "application/json"})
    assert unsigned.status_code == 403
    assert "WHATSAPP_APP_SECRET" in unsigned.json()["detail"]
    forged = live.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert forged.status_code == 403
    assert calls == []

    simulator = TestClient(_live_app(shop, tmp_path, calls, channel="simulator"))
    sim_status = simulator.get("/v1/whatsapp/status").json()
    assert sim_status["channel"] == "simulator"
    assert "WHATSAPP_APP_SECRET" not in sim_status["missing"]
    assert sim_status["signature_required"] is False
    accepted = simulator.post(
        "/v1/whatsapp/webhook",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["processed"] is False

    switched = simulator.post("/v1/whatsapp/channel", json={"channel": "whatsapp"})
    assert switched.status_code == 200
    assert "WHATSAPP_APP_SECRET" in switched.json()["missing"]
    blocked = simulator.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert blocked.status_code == 403
    assert calls == []

    secured = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    missing_header = secured.post(
        "/v1/whatsapp/webhook",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert missing_header.status_code == 403
    assert missing_header.json()["detail"] == "invalid signature"


def test_outputs_omit_the_base64_demo_number_and_status_matches_by_hash(shop, tmp_path, monkeypatch, capsys):
    raw_id = _embedded_wamid(DEMO_DIGITS)
    coded = phone_base64(DEMO_DIGITS)
    assert coded in raw_id
    assert coded not in wamid_tail(raw_id)
    assert coded not in wamid_hash(raw_id)

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": raw_id}]})

    client = TestClient(
        _app(
            shop,
            tmp_path,
            channel="whatsapp",
            whatsapp_token=TOKEN,
            whatsapp_phone_number_id=PHONE_ID,
            whatsapp_verify_token="verify-me",
            whatsapp_app_secret=SECRET,
            whatsapp_demo_to=DEMO,
            whatsapp_http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )
    sent = client.post("/v1/customers/1/whatsapp")
    assert sent.status_code == 200
    assert sent.json()["sent"] == "template"
    assert sent.json()["wamid_hash"] == wamid_hash(raw_id)
    assert sent.json()["wamid_tail"] == wamid_tail(raw_id)
    _assert_number_hidden(sent.text, raw_id, coded)

    thread = client.get("/v1/customers/1/thread")
    assert thread.json()["messages"][-1]["whatsapp"]["wamid_tail"] == wamid_tail(raw_id)
    assert thread.json()["messages"][-1]["whatsapp"]["status"] == "sent"
    _assert_number_hidden(thread.text, raw_id, coded)
    actions = client.get("/v1/actions")
    _assert_number_hidden(actions.text, raw_id, coded)
    log_path = tmp_path / "actions.jsonl"
    state_path = tmp_path / "output" / "whatsapp_state.json"
    _assert_number_hidden(log_path.read_text(), raw_id, coded)
    _assert_number_hidden(state_path.read_text(), raw_id, coded)
    assert wamid_hash(raw_id) in state_path.read_text()

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {"id": raw_id, "status": "read", "recipient_id": DEMO_DIGITS, "timestamp": "1"}
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    status = client.post(
        "/v1/whatsapp/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
    )
    assert status.status_code == 200
    _assert_number_hidden(status.text, raw_id, coded)
    updated = client.get("/v1/customers/1/thread").json()
    assert updated["messages"][-1]["whatsapp"]["status"] == "read"
    _assert_number_hidden(json.dumps(updated), raw_id, coded)
    _assert_number_hidden(log_path.read_text(), raw_id, coded)
    _assert_number_hidden(state_path.read_text(), raw_id, coded)
    _assert_number_hidden(client.get("/").text, raw_id, coded)

    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("CONVOPLEX_DB", db_file)
    monkeypatch.setenv("CONVOPLEX_LOG", str(log_path))
    monkeypatch.setenv("CONVOPLEX_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("WHATSAPP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_DEMO_TO", DEMO)
    monkeypatch.setenv("CONVOPLEX_CHANNEL", "whatsapp")
    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.default_graph_http",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert main(["whatsapp", "send-template"]) == 0
    printed = capsys.readouterr()
    assert wamid_tail(raw_id) in printed.out
    _assert_number_hidden(printed.out + printed.err, raw_id, coded)
    _assert_number_hidden(log_path.read_text(), raw_id, coded)


def test_whatsapp_winback_cli_matches_the_customer_send(shop, tmp_path, monkeypatch, capsys):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("CONVOPLEX_DB", db_file)
    monkeypatch.setenv("CONVOPLEX_LOG", str(tmp_path / "actions.jsonl"))
    monkeypatch.setenv("CONVOPLEX_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("WHATSAPP_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_DEMO_TO", DEMO)
    monkeypatch.setenv("CONVOPLEX_CHANNEL", "simulator")
    raw_id = _embedded_wamid(DEMO_DIGITS)
    coded = phone_base64(DEMO_DIGITS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": raw_id}]})

    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.default_graph_http",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert main(["whatsapp", "winback", "--customer", "1"]) == 1
    refused = capsys.readouterr().err
    assert "CONVOPLEX_CHANNEL" in refused
    assert coded not in refused

    monkeypatch.setenv("CONVOPLEX_CHANNEL", "whatsapp")
    assert main(["whatsapp", "winback", "--customer", "99"]) == 1
    assert "Unknown customer" in capsys.readouterr().err

    assert main(["whatsapp", "winback", "--customer", "2", "--lang", "pcm"]) == 0
    drafted = capsys.readouterr().out
    assert "hello_world" in drafted
    assert "24-hour" in drafted
    assert wamid_tail(raw_id) in drafted
    _assert_number_hidden(drafted, raw_id, coded)
    language = shop.execute(
        "SELECT language FROM offers WHERE customer_id = 2 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert language["language"] == "pcm"

    assert main(["whatsapp", "winback", "--customer", "1", "--lang", "en", "--promo-card"]) == 0
    promo = capsys.readouterr().out
    assert "Promo card:" in promo
    assert "hello_world" in promo
    _assert_number_hidden(promo, raw_id, coded)
    cards = list((tmp_path / "output" / "promo").glob("offer-*.webp"))
    assert len(cards) == 1
    spoken = shop.execute(
        "SELECT language FROM offers WHERE customer_id = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert spoken["language"] == "en"
    _assert_number_hidden((tmp_path / "actions.jsonl").read_text(), raw_id, coded)
    _assert_number_hidden((tmp_path / "output" / "whatsapp_state.json").read_text(), raw_id, coded)


def test_quick_tunnel_parser_and_missing_binary(monkeypatch):
    sample = (
        "INF |  https://random-name.trycloudflare.com                                                     |\n"
    )
    assert parse_quick_tunnel_url(sample) == "https://random-name.trycloudflare.com"
    assert parse_quick_tunnel_url("no tunnel here") is None
    monkeypatch.setattr("convoplex.tunnel.shutil.which", lambda name: None)
    assert start_quick_tunnel(8000) == (None, None)


def _settings(tmp_path, **extra) -> Settings:
    values = dict(
        db_path=tmp_path / "shop.sqlite",
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        whatsapp_token=TOKEN,
        whatsapp_phone_number_id=PHONE_ID,
        whatsapp_demo_to=DEMO,
        whatsapp_verify_token="verify-me",
        channel="whatsapp",
        graph_api_version="v21.0",
    )
    values.update(extra)
    return Settings(**values)


def _live_app(shop, tmp_path, calls: list[httpx.Request], *, channel: str = "whatsapp", app_secret: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if b"hello_world" in request.content:
            return httpx.Response(200, json={"messages": [{"id": "wamid.TPL"}]})
        return httpx.Response(200, json={"messages": [{"id": "wamid.TEXT"}]})

    return _app(
        shop,
        tmp_path,
        channel=channel,
        whatsapp_token=TOKEN,
        whatsapp_phone_number_id=PHONE_ID,
        whatsapp_verify_token="verify-me",
        whatsapp_app_secret=app_secret,
        whatsapp_demo_to=DEMO,
        whatsapp_http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _embedded_wamid(phone: str) -> str:
    """A Cloud-API-like id whose base64 body contains the phone digits."""
    body = b"\x1c\x18" + bytes([len(phone)]) + phone.encode() + b"\x15\x02\x00\x01\x12\x14" + b"0123456789abcdefABCD"
    token = base64.b64encode(body).decode().rstrip("=")
    return "wamid." + token


def _assert_number_hidden(text: str, raw_id: str, coded: str) -> None:
    assert raw_id not in text
    assert coded not in text
    assert "wamid." not in text


def _inbound(message_id: str, sender: str, text: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": sender,
                                    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _sign(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _tiny_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


def _tiny_webp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 90, 60)).save(buffer, format="WEBP")
    return buffer.getvalue()
