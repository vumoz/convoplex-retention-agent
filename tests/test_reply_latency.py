"""Skip a second model call for a language-only switch, and time the WhatsApp turn."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.languages import CONFIRM, message_asks_something_else, message_is_language_only
from convoplex.llm import MockMuse
from tests.test_whatsapp import DEMO_DIGITS, SECRET, TOKEN, _inbound, _live_app, _sign


class _Counting:
    mode = "mock"

    def __init__(self):
        self.calls: list[dict] = []
        self.inner = MockMuse()

    def complete(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.inner.complete(messages, **kwargs)


def test_hausa_phrase_is_language_only_and_a_price_question_is_not():
    phrase = "Make you talk to me for Hausa."
    assert message_is_language_only(phrase) is True
    assert message_asks_something_else(phrase) is False
    mixed = "Make you talk to me for Hausa. How much is the rice?"
    assert message_is_language_only(mixed) is False
    assert message_asks_something_else(mixed) is True


def test_language_only_switch_skips_the_second_model_call(shop, log_path):
    llm = _Counting()
    result = handle_reply(
        shop,
        ActionLog(log_path),
        llm,
        1,
        "Make you talk to me for Hausa.",
        max_tokens=2500,
    )
    assert len(llm.calls) == 1
    assert llm.calls[0]["reasoning_effort"] == "minimal"
    assert llm.calls[0]["retry_on_length"] is False
    assert result.tool_trace == ["set_language"]
    assert result.language == "ha"
    assert result.message == CONFIRM["ha"]
    assert "pidgin" not in result.message.lower()
    calls = [entry for entry in ActionLog(log_path).read_all() if entry["kind"] == "model_call"]
    assert len(calls) == 1
    assert calls[0]["payload"]["reasoning_effort"] == "minimal"
    assert calls[0]["payload"]["retry_on_length"] is False


def test_language_plus_a_shop_question_uses_one_followup_in_the_new_language(shop, log_path):
    llm = _Counting()
    result = handle_reply(
        shop,
        ActionLog(log_path),
        llm,
        1,
        "Make you talk to me for Hausa. How much is the rice?",
        max_tokens=2500,
    )
    assert len(llm.calls) == 2
    assert all(call["reasoning_effort"] == "minimal" for call in llm.calls)
    assert all(call["retry_on_length"] is False for call in llm.calls)
    assert result.tool_trace == ["set_language"]
    assert result.language == "ha"
    assert result.message.startswith(CONFIRM["ha"])
    assert "₦78,000" in result.message
    assert "rice" in result.message


def test_inbound_marks_read_shows_typing_and_logs_e2e(shop, tmp_path):
    calls: list = []
    client = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    raw_id = "wamid.HAUSA.1"
    payload = _inbound(raw_id, DEMO_DIGITS, "Make you talk to me for Hausa.")
    raw = json.dumps(payload).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    assert len(calls) == 2
    read = json.loads(calls[0].content)
    assert read["messaging_product"] == "whatsapp"
    assert read["status"] == "read"
    assert read["message_id"] == raw_id
    assert read["typing_indicator"] == {"type": "text"}
    sent = json.loads(calls[1].content)
    assert sent["type"] == "text"
    assert sent["text"]["body"] == CONFIRM["ha"]

    logged = client.get("/v1/actions").json()["entries"]
    sends = [entry for entry in logged if entry["kind"] == "whatsapp_send"]
    assert sends[-1]["payload"]["body"] == CONFIRM["ha"]
    assert isinstance(sends[-1]["payload"]["e2e_ms"], int)
    assert sends[-1]["payload"]["e2e_ms"] >= 0
    reply_calls = [
        entry
        for entry in logged
        if entry["kind"] == "model_call" and entry["payload"].get("task") == "reply"
    ]
    assert len(reply_calls) == 1
    blob = json.dumps(logged)
    assert raw_id not in blob
    assert TOKEN not in blob
    assert DEMO_DIGITS not in blob
    assert "wamid." not in blob
