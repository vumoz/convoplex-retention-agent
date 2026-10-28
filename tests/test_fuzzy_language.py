"""Misheard language names, the set_language tool, and an honest WhatsApp log."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.integrations.voice import Transcript
from convoplex.languages import CONFIRM, interpret_language
from convoplex.llm import MockMuse
from convoplex.tools import TOOLS
from tests.test_whatsapp import DEMO_DIGITS, SECRET, _live_app, _sign


def test_short_messages_accept_small_misspellings_and_ignore_ordinary_words():
    assert interpret_language("Yurba") == {"action": "set", "language": "yo"}
    assert interpret_language("Yurba?") == {"action": "set", "language": "yo"}
    assert interpret_language("Yaruba") == {"action": "set", "language": "yo"}
    assert interpret_language("Yorba") == {"action": "set", "language": "yo"}
    assert interpret_language("Egbo") == {"action": "set", "language": "ig"}
    assert interpret_language("Housa") == {"action": "set", "language": "ha"}
    assert interpret_language("Ausa") == {"action": "set", "language": "ha"}
    assert interpret_language("Hawsa") == {"action": "set", "language": "ha"}
    assert interpret_language("Pigin") == {"action": "set", "language": "pcm"}
    assert interpret_language("Brokin") == {"action": "set", "language": "pcm"}
    assert interpret_language("Inglish") == {"action": "set", "language": "en"}
    for ordinary in ("yes", "rice", "house", "hello", "broke"):
        assert interpret_language(ordinary) is None
    assert interpret_language("I broke the rice") is None
    assert interpret_language("The bag broke yesterday") is None
    assert interpret_language("How much last price?") is None


def test_set_language_tool_saves_and_replies_in_the_new_language(shop, log_path):
    names = [tool["function"]["name"] for tool in TOOLS]
    assert "set_language" in names
    result = handle_reply(
        shop,
        ActionLog(log_path),
        MockMuse(),
        1,
        "Please talk to me in Yoruba from now on",
        max_tokens=2500,
    )
    assert result.language == "yo"
    assert CONFIRM["yo"] in result.message
    assert "pidgin" not in result.message.lower()
    assert "Aga m eji Igbo" not in result.message
    assert "set_language" in result.tool_trace
    row = shop.execute(
        "SELECT language, language_opted_in, language_channel FROM customers WHERE id = 1"
    ).fetchone()
    assert row["language"] == "yo"
    assert row["language_opted_in"] == 1
    assert row["language_channel"] == "whatsapp"


def test_webhook_with_a_stale_window_logs_the_text_that_was_sent(shop, tmp_path, monkeypatch):
    """The real webhook path. A 48-hour-old stamp must not turn the confirmation into hello_world."""
    calls: list = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    stale = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.YORUBA",
                                    "from": DEMO_DIGITS,
                                    "timestamp": str(stale),
                                    "type": "text",
                                    "text": {"body": "Yoruba"},
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
    sent = json.loads(calls[-1].content)
    assert sent["type"] == "text"
    assert sent["text"]["body"] == CONFIRM["yo"]
    assert "hello_world" not in calls[-1].content.decode()

    logged = client.get("/v1/actions").json()["entries"]
    sends = [entry for entry in logged if entry["kind"] == "whatsapp_send"]
    assert sends
    assert sends[-1]["payload"]["kind"] == "text"
    assert sends[-1]["payload"]["body"] == CONFIRM["yo"]
    assert "hello_world" not in sends[-1]["payload"].get("body", "")

    thread = client.get("/v1/customers/1/thread").json()
    blob = json.dumps(thread)
    assert "hello_world" not in blob
    shop_lines = [item["text"] for item in thread["messages"] if item["role"] == "shop"]
    assert CONFIRM["yo"] in shop_lines
    attached = [item for item in thread["messages"] if item["role"] == "shop" and item.get("whatsapp")]
    assert attached
    assert attached[-1]["whatsapp"]["kind"] == "text"
    assert shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"] == "yo"


def test_voice_yurba_switches_to_yoruba_instead_of_staying_in_igbo(shop, tmp_path, monkeypatch):
    shop.execute(
        "UPDATE customers SET language = 'ig', language_opted_in = 1, language_channel = 'whatsapp' WHERE id = 1"
    )
    shop.commit()

    def fake_transcribe(raw, **kwargs):
        return Transcript(text="Yurba?", model="mock", latency_ms=0, source_format="wav", mode="mock")

    monkeypatch.setattr("convoplex.whatsapp_flow.transcribe_audio", fake_transcribe)
    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.WhatsAppClient.download_media",
        lambda self, media_id: (b"RIFFfake", "audio/wav"),
    )
    calls: list = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    old = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.YURBA",
                                    "from": DEMO_DIGITS,
                                    "timestamp": str(old),
                                    "type": "audio",
                                    "audio": {"id": "media-yurba", "mime_type": "audio/wav"},
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
    sent = json.loads(calls[-1].content)
    assert sent["type"] == "text"
    assert sent["text"]["body"] == CONFIRM["yo"]
    assert "pidgin" not in sent["text"]["body"].lower()
    assert "hello_world" not in calls[-1].content.decode()
    assert shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"] == "yo"
