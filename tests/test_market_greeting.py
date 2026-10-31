"""'My customer' is a greeting, not a brand the shop is out of."""

import json

import httpx
from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.greeting import customer_address
from convoplex.lexicon import learn_phrase
from convoplex.llm import Completion, MockMuse
from convoplex.money import naira
from convoplex.substitute import unstocked_request


class _BrandModel:
    """What the live model did: treat the address as a missing product."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content="Costoma no dey for our shop.\nINTENT: decline",
            finish_reason="stop",
        )


def _orders(shop) -> int:
    return int(shop.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])


def _prepare(shop, log_path, customer_id, language):
    shop.execute("UPDATE customers SET language = ? WHERE id = ?", (language, customer_id))
    shop.commit()
    log = ActionLog(log_path)
    draft = draft_winback(shop, log, MockMuse(), customer_id, max_tokens=1000, language=language)
    return log, draft


def _assert_sale(shop, result, draft):
    price = naira(draft.offer["offer_price_ngn"])
    lowered = result.message.lower()
    assert "mama gold" in lowered
    assert price in result.message
    assert "no dey" not in lowered
    assert "we no get" not in lowered
    assert "you mean" not in lowered
    assert result.order is None
    assert result.intent == "question"
    assert result.confirm_offer_id == draft.offer["offer_id"]
    state = shop.execute(
        "SELECT state FROM purchase_states WHERE offer_id = ? ORDER BY id DESC LIMIT 1",
        (draft.offer["offer_id"],),
    ).fetchone()["state"]
    assert state == "offer_presented"
    assert shop.execute(
        "SELECT status FROM offers WHERE id = ?",
        (draft.offer["offer_id"],),
    ).fetchone()["status"] == "sent"


def test_customer_address_mirrors_my_customer_and_customer():
    assert customer_address("My customer, how you dey?") == "My customer"
    assert customer_address("My costoma, I wan buy rice") == "My customer"
    assert customer_address("Customer I wan buy rice") == "Customer"
    assert customer_address("kustoma, abeg rice") == "Customer"
    assert customer_address("I wan buy rice") is None


def test_my_customer_greeting_then_the_sale_by_text_and_voice(shop, log_path):
    phrases = (
        "My customer, how you dey? I wan buy rice",
        "My costoma how you dey, I wan buy rice",
    )
    for phrase in phrases:
        for voice in (False, True):
            log, draft = _prepare(shop, log_path, 1, "pcm")
            before = _orders(shop)
            result = handle_reply(
                shop, log, _BrandModel(), 1, phrase, max_tokens=1000, from_voice=voice
            )
            _assert_sale(shop, result, draft)
            assert result.message.startswith("My customer,")
            assert "I dey fine" in result.message
            assert "Make I hold one bag for you?" in result.message
            assert _orders(shop) == before
            assert shop.execute(
                "SELECT id FROM lexicon WHERE phrase IN ('customer', 'costoma', 'kustoma')"
            ).fetchone() is None


def test_customer_without_a_greeting_does_not_answer_how_you_dey(shop, log_path):
    for phrase in ("Customer, I wan buy rice", "costoma I wan buy rice"):
        for voice in (False, True):
            log, draft = _prepare(shop, log_path, 1, "pcm")
            result = handle_reply(
                shop, log, _BrandModel(), 1, phrase, max_tokens=1000, from_voice=voice
            )
            _assert_sale(shop, result, draft)
            assert result.message.startswith("Customer,")
            assert "I dey fine" not in result.message
            assert "how you dey" not in result.message.lower()
            assert "Make I hold one for you?" in result.message


def test_greeting_only_represents_the_open_offer(shop, log_path):
    log, draft = _prepare(shop, log_path, 1, "pcm")
    result = handle_reply(
        shop, log, _BrandModel(), 1, "My customer, how you dey?", max_tokens=1000, from_voice=True
    )
    _assert_sale(shop, result, draft)
    assert result.message.startswith("My customer,")
    assert "I dey fine" in result.message
    assert result.order is None


def test_oga_is_not_a_missing_brand(shop, log_path):
    log, draft = _prepare(shop, log_path, 1, "pcm")
    profile_text = "Oga, you get rice?"
    assert unstocked_request(shop, profile_text, {"retailer_id": 1, "open_offer": draft.offer}) is None
    result = handle_reply(shop, log, _BrandModel(), 1, profile_text, max_tokens=1000)
    _assert_sale(shop, result, draft)
    assert not result.message.lower().startswith("oga")
    assert "oga no dey" not in result.message.lower()


def test_igbo_and_yoruba_greetings_keep_the_sale(shop, log_path):
    log, draft = _prepare(shop, log_path, 1, "ig")
    igbo = handle_reply(shop, log, _BrandModel(), 1, "Ndewo", max_tokens=1000, from_voice=True)
    _assert_sale(shop, igbo, draft)
    assert "A dị m mma" in igbo.message
    assert "no dey" not in igbo.message.lower()

    log, draft = _prepare(shop, log_path, 2, "yo")
    yoruba = handle_reply(shop, log, _BrandModel(), 2, "E kaaro", max_tokens=1000)
    _assert_sale(shop, yoruba, draft)
    assert "Mo wa daadaa" in yoruba.message
    assert "no dey" not in yoruba.message.lower()


def test_voice_transcript_without_an_open_offer_still_quotes_the_rice(shop, log_path):
    """The live bug: no win-back yet, so the reply was only 'My customer,'."""
    phrases = (
        "my customer, i wan buy rice",
        "My customer I wan buy rice.",
        "my costoma i wan buy rice",
        "Customer i wan buy rice",
        "my customer, how you dey? i wan buy rice",
    )
    for phrase in phrases:
        for voice in (False, True):
            shop.execute("DELETE FROM purchase_states")
            shop.execute("DELETE FROM offers")
            shop.commit()
            log = ActionLog(log_path)
            before = _orders(shop)
            result = handle_reply(
                shop, log, _BrandModel(), 1, phrase, max_tokens=1000, from_voice=voice
            )
            lowered = result.message.lower()
            assert lowered.startswith("my customer") or lowered.startswith("customer,")
            assert "mama gold" in lowered
            assert "₦78,000" in result.message
            assert result.message.strip(" .,!") != "My customer"
            assert "no dey" not in lowered
            assert result.order is None
            assert result.confirm_offer_id
            assert _orders(shop) == before
            state = shop.execute(
                "SELECT state FROM purchase_states WHERE offer_id = ? ORDER BY id DESC LIMIT 1",
                (result.confirm_offer_id,),
            ).fetchone()["state"]
            assert state == "offer_presented"

    shop.execute("DELETE FROM offers")
    shop.commit()
    log = ActionLog(log_path)
    only = handle_reply(shop, log, _BrandModel(), 1, "My customer.", max_tokens=1000, from_voice=True)
    assert only.message.startswith("My customer,")
    assert "I dey fine" in only.message
    assert "mama gold" in only.message.lower() or "wetin you wan buy" in only.message.lower()
    assert only.message.strip(" .,!") != "My customer"
    assert only.order is None


def test_voice_webhook_sends_product_and_price_not_just_the_address(shop, tmp_path, monkeypatch):
    from convoplex.integrations.voice import Transcript
    from tests.test_whatsapp import DEMO, DEMO_DIGITS, PHONE_ID, SECRET, TOKEN, _app, _sign

    def fake_transcribe(raw, **kwargs):
        return Transcript(
            text="my customer, i wan buy rice",
            model="mock",
            latency_ms=0,
            source_format="wav",
            mode="mock",
        )

    monkeypatch.setattr("convoplex.whatsapp_flow.transcribe_audio", fake_transcribe)
    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.WhatsAppClient.download_media",
        lambda self, media_id: (b"RIFFfake", "audio/wav"),
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if b'"status"' in request.content and b"read" in request.content:
            return httpx.Response(200, json={"success": True})
        body = json.loads(request.content)
        if body.get("type") == "interactive":
            return httpx.Response(200, json={"messages": [{"id": "wamid.BTN"}]})
        return httpx.Response(200, json={"messages": [{"id": "wamid.TXT"}]})

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
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.CUST1",
                                    "from": DEMO_DIGITS,
                                    "timestamp": "1761523200",
                                    "type": "audio",
                                    "audio": {"id": "media-cust", "mime_type": "audio/wav"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw = json.dumps(payload).encode()
    sent = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert sent.status_code == 200
    assert sent.json()["processed"] is True
    bodies = []
    for call in calls:
        if not call.content or call.content[:1] != b"{":
            continue
        body = json.loads(call.content)
        if body.get("type") == "text":
            bodies.append(body["text"]["body"])
        elif body.get("type") == "interactive":
            bodies.append(body["interactive"]["body"]["text"])
    assert bodies
    outgoing = bodies[-1]
    assert outgoing.strip(" .,!") != "My customer"
    assert "Mama Gold" in outgoing
    assert "₦78,000" in outgoing
    assert "my customer" in outgoing.lower()

    calls.clear()
    again = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.CUST2",
                                    "from": DEMO_DIGITS,
                                    "timestamp": "1761523300",
                                    "type": "audio",
                                    "audio": {"id": "media-cust-2", "mime_type": "audio/wav"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw_again = json.dumps(again).encode()
    second = client.post(
        "/v1/whatsapp/webhook",
        content=raw_again,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw_again)},
    )
    assert second.status_code == 200
    again_bodies = []
    for call in calls:
        if not call.content or call.content[:1] != b"{":
            continue
        body = json.loads(call.content)
        if body.get("type") == "interactive":
            again_bodies.append(body["interactive"]["body"]["text"])
        elif body.get("type") == "text":
            again_bodies.append(body["text"]["body"])
    assert again_bodies
    assert "Mama Gold" in again_bodies[-1]
    assert "₦78,000" in again_bodies[-1]
    assert again_bodies[-1].strip(" .,!") != "My customer"


def test_a_salutation_is_not_learned_or_a_missing_product(shop, log_path):
    refused = learn_phrase(shop, "costoma", "pcm", "a person", customer_id=1)
    assert refused["ok"] is False
    log, draft = _prepare(shop, log_path, 1, "pcm")
    assert unstocked_request(shop, "Customer", {"retailer_id": 1, "open_offer": draft.offer}) is None
    assert unstocked_request(shop, "kustoma", {"retailer_id": 1, "open_offer": draft.offer}) is None
    assert log is not None
