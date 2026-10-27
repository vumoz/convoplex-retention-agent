from pathlib import Path

from fastapi.testclient import TestClient

from convoplex.api import create_app
from convoplex.config import Settings


def _app(shop, tmp_path, **extra):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    settings = Settings(
        db_path=Path(db_file),
        log_path=tmp_path / "actions.jsonl",
        llm_mode="mock",
        whatsapp_verify_token=extra.get("whatsapp_verify_token"),
    )
    return create_app(settings, generate_if_missing=False)


def test_at_risk_winback_reply_and_metrics(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["synthetic"] is True
    assert health.json()["llm_mode"] == "mock"
    assert "SYNTHETIC" in health.json()["label"]

    listed = client.get("/v1/customers/at-risk")
    assert listed.status_code == 200
    ids = {row["id"] for row in listed.json()["customers"]}
    assert ids == {1, 2}
    chinedu = next(row for row in listed.json()["customers"] if row["id"] == 1)
    assert chinedu["segment"] == "lapsed"
    assert chinedu["reasons"]

    missing = client.get("/v1/customers/99")
    assert missing.status_code == 404

    before = client.get("/v1/metrics").json()
    assert before["offers_sent"] == 0
    assert before["naira_recovered"] == 0

    win = client.post("/v1/customers/1/winback")
    assert win.status_code == 200
    body = win.json()
    assert body["llm"] == "mock"
    assert body["language"] == "pcm"
    assert "₦" in body["message"]
    assert body["offer"]["offer_price_ngn"] == 70200
    assert body["tool_calls"] == ["check_stock", "get_price", "create_offer"]

    reply = client.post("/v1/customers/1/reply", json={"text": "Yes o, no wahala. Abeg send am today."})
    assert reply.status_code == 200
    assert reply.json()["intent"] == "accept"
    assert reply.json()["order"]["total_ngn"] == 70200

    client.post("/v1/customers/2/winback")
    declined = client.post("/v1/customers/2/reply", json={"text": "No thank you, maybe next month"})
    assert declined.json()["intent"] == "decline"
    assert declined.json()["order"] is None

    metrics = client.get("/v1/metrics").json()
    assert metrics["offers_sent"] == 2
    assert metrics["offers_accepted"] == 1
    assert metrics["win_back_rate"] == 0.5
    assert metrics["naira_recovered"] == 70200
    assert metrics["currency"] == "NGN"

    verified = client.post("/v1/actions/verify")
    assert verified.json()["ok"] is True
    assert verified.json()["entries"] > 0
    actions = client.get("/v1/actions")
    assert actions.json()["count"] == verified.json()["entries"]
    assert client.post("/v1/customers/1/reply", json={"text": ""}).status_code == 422


def test_voice_image_and_whatsapp_stubs(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path, whatsapp_verify_token="hackathon-token"))
    bare = client.post("/v1/voice/transcribe")
    assert bare.status_code == 422

    wav = _tiny_wav()
    voice = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (
                None,
                '{"model":"muse-voice-transcribe-1.0","audioEncoding":"WAV","mode":"PUSH_TO_TALK"}',
                "application/json",
            ),
            "audio": ("note.wav", wav, "audio/wav"),
        },
    )
    assert voice.status_code == 501
    assert voice.json()["validated"] is True
    assert voice.json()["upstream"].endswith("/asr/transcribe")
    assert voice.json()["conversion_required"] is False

    ogg = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, "{}", "application/json"),
            "audio": ("note.ogg", b"ogg-bytes", "audio/ogg"),
        },
    )
    assert ogg.status_code == 501
    assert ogg.json()["conversion_required"] is True
    assert ogg.json()["ffmpeg"][0] == "ffmpeg"
    assert "16000" in ogg.json()["ffmpeg"]

    bad = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, "not-json", "application/json"),
            "audio": ("note.wav", wav, "audio/wav"),
        },
    )
    assert bad.status_code == 400

    missing_card = client.post("/v1/offers/99/promo-card")
    assert missing_card.status_code == 404
    client.post("/v1/customers/1/winback")
    card = client.post("/v1/offers/1/promo-card")
    assert card.status_code == 501
    request = card.json()["request"]
    assert request["model"] == "muse-image-1.0"
    assert request["n"] == 1
    assert "Mama Gold" in request["prompt"]
    assert card.json()["upstream"].endswith("/images/generations")

    challenge = client.get(
        "/v1/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "hackathon-token", "hub.challenge": "12345"},
    )
    assert challenge.status_code == 200
    assert challenge.text == "12345"
    forbidden = client.get(
        "/v1/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"},
    )
    assert forbidden.status_code == 403

    inbound = client.post(
        "/v1/whatsapp/webhook",
        json={
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "from": "2348030001101",
                                        "type": "text",
                                        "text": {"body": "Yes o"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )
    assert inbound.status_code == 200
    payload = inbound.json()
    assert payload["status"] == "stub"
    assert payload["processed"] is False
    assert payload["messages"][0]["text"] == "Yes o"

    unconfigured = TestClient(_app(shop, tmp_path))
    assert unconfigured.get("/v1/whatsapp/webhook").status_code == 501


def _tiny_wav() -> bytes:
    import io
    import struct
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * 160)
    return buffer.getvalue()
