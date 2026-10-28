"""Live WhatsApp bugs: voice-note language switches, and free-text confirmation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from convoplex.integrations.voice import Transcript, VoiceError
from convoplex.languages import CONFIRM, guard_reply, interpret_language
from convoplex.prompts import _RULES
from tests.test_whatsapp import DEMO_DIGITS, SECRET, _inbound, _live_app, _sign


def test_voice_aliases_and_short_phrases_match_after_normalizing():
    assert interpret_language("Ibo.") == {"action": "set", "language": "ig"}
    assert interpret_language("Ibo") == {"action": "set", "language": "ig"}
    assert interpret_language("Ịgbo") == {"action": "set", "language": "ig"}
    assert interpret_language("Yoruba.") == {"action": "set", "language": "yo"}
    assert interpret_language("Yorùbá") == {"action": "set", "language": "yo"}
    assert interpret_language("Èdè Yorùbá") == {"action": "set", "language": "yo"}
    assert interpret_language("Yoruba please") == {"action": "set", "language": "yo"}
    assert interpret_language("speak Igbo") == {"action": "set", "language": "ig"}
    assert interpret_language("change to Hausa") == {"action": "set", "language": "ha"}
    assert interpret_language("I want Yoruba") == {"action": "set", "language": "yo"}
    assert interpret_language("Hausa language") == {"action": "set", "language": "ha"}
    assert interpret_language("pidgin") == {"action": "set", "language": "pcm"}
    assert interpret_language("broken") == {"action": "set", "language": "pcm"}
    assert interpret_language("broken english") == {"action": "set", "language": "pcm"}
    assert interpret_language("naija") == {"action": "set", "language": "pcm"}
    assert interpret_language("english") == {"action": "set", "language": "en"}
    assert interpret_language("formal english") == {"action": "set", "language": "en"}
    assert interpret_language("standard english") == {"action": "set", "language": "en"}
    assert interpret_language("How much last price?") is None
    assert interpret_language("The bag is broken") is None


def test_prompt_never_refuses_a_supported_language_or_says_pidgin():
    lowered = _RULES.lower()
    # The glossary may name the language once. A reply still must not say it.
    assert "pidgin" not in lowered.replace("nigerian pidgin", "")
    assert "the only name you may use in a message is broken english" in lowered
    assert "broken english" in lowered
    assert "never tell the customer you cannot speak" in lowered
    refused = "Chinyere, I no sabi Yoruba, I go talk for pidgin wey you understand."
    guarded = guard_reply(refused, "pcm")
    assert "pidgin" not in guarded.lower()
    assert guarded == CONFIRM["pcm"]


def test_language_confirmation_is_free_text_when_the_stored_window_looks_closed(shop, tmp_path, monkeypatch):
    """Inbound 'Igbo' is a live session even if the saved stamp would send hello_world."""
    monkeypatch.setattr("convoplex.whatsapp_flow.window_open", lambda *_args, **_kwargs: False)
    calls: list = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    customer_id = 1
    payload = _inbound("wamid.IGBO", DEMO_DIGITS, "Igbo")
    raw = json.dumps(payload).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    assert calls, "a WhatsApp send should have gone out"
    sent = json.loads(calls[-1].content)
    assert sent["type"] == "text"
    assert sent["text"]["body"] == CONFIRM["ig"]
    assert "hello_world" not in calls[-1].content.decode()
    row = shop.execute("SELECT language FROM customers WHERE id = ?", (customer_id,)).fetchone()
    assert row["language"] == "ig"


def test_voice_note_transcript_switches_language_and_still_replies(shop, tmp_path, monkeypatch):
    def fake_transcribe(raw, **kwargs):
        return Transcript(text="Ibo.", model="mock", latency_ms=0, source_format="wav", mode="mock")

    monkeypatch.setattr("convoplex.whatsapp_flow.transcribe_audio", fake_transcribe)
    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.WhatsAppClient.download_media",
        lambda self, media_id: (b"RIFFfake", "audio/wav"),
    )
    calls: list = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    raw = json.dumps(_audio("wamid.IBO", "media-ibo")).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    sent = json.loads(calls[-1].content)
    assert sent["type"] == "text"
    assert sent["text"]["body"] == CONFIRM["ig"]
    assert shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"] == "ig"
    logged = client.get("/v1/actions").json()["entries"]
    assert any(entry["kind"] == "model_call" and entry["payload"].get("task") == "transcribe" for entry in logged)


def test_a_failed_voice_note_is_logged_and_still_answered(shop, tmp_path, monkeypatch):
    def explode(raw, **kwargs):
        raise VoiceError("ffmpeg is not installed")

    monkeypatch.setattr("convoplex.whatsapp_flow.transcribe_audio", explode)
    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.WhatsAppClient.download_media",
        lambda self, media_id: (b"RIFFfake", "audio/wav"),
    )
    calls: list = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    raw = json.dumps(_audio("wamid.BAD", "media-bad")).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    sent = json.loads(calls[-1].content)
    assert sent["type"] == "text"
    assert "voice note" in sent["text"]["body"].lower()
    assert "hello_world" not in calls[-1].content.decode()
    logged = client.get("/v1/actions").json()["entries"]
    errors = [entry for entry in logged if entry["kind"] == "whatsapp_error"]
    assert errors
    assert "ffmpeg" in errors[-1]["payload"]["error"]


def _audio(message_id: str, media_id: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": DEMO_DIGITS,
                                    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                    "type": "audio",
                                    "audio": {"id": media_id, "mime_type": "audio/wav"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
