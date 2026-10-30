"""Picture requests send the offer card. An explicit language request wins."""

import json

import httpx
from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.languages import CONFIRM, LANGUAGES, explicit_language
from convoplex.llm import Completion, MockMuse
from convoplex.money import naira
from tests.test_whatsapp import DEMO, DEMO_DIGITS, PHONE_ID, SECRET, TOKEN, _app, _inbound, _sign

_ASK = {
    "ha": "Please talk to me in Hausa.",
    "ig": "Please talk to me in Igbo.",
    "yo": "Please talk to me in Yoruba.",
    "pcm": "Please talk to me in pidgin.",
    "en": "Please talk to me in English.",
}


class _OldLanguage:
    """Writes in Igbo and never calls set_language."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content="Kedu? Osikapa ka dị.\nINTENT: question", finish_reason="stop")


class _NoImage:
    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        raise AssertionError("a picture request must not be left to a model that refuses")


def _orders(shop) -> int:
    return int(shop.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])


def test_misheard_language_names():
    assert explicit_language("Hausa") == "ha"
    assert explicit_language("house a") == "ha"
    assert explicit_language("ausa") == "ha"
    assert explicit_language("talk to me for house a") == "ha"
    assert explicit_language("yaruba") == "yo"
    assert explicit_language("Yoruba") == "yo"
    assert explicit_language("ibo") == "ig"
    assert explicit_language("Igbo") == "ig"
    assert explicit_language("The bag is broken") is None
    assert explicit_language("I wan buy rice") is None


def test_every_language_switch_by_text_and_voice(shop, log_path):
    pairs = [(source, target) for source in LANGUAGES for target in LANGUAGES if source != target]
    for source, target in pairs:
        for voice in (False, True):
            shop.execute("UPDATE customers SET language = ? WHERE id = 1", (source,))
            shop.commit()
            result = handle_reply(
                shop,
                ActionLog(log_path),
                _OldLanguage(),
                1,
                _ASK[target],
                max_tokens=1000,
                from_voice=voice,
            )
            assert result.language == target, (source, target, voice, result.message)
            assert result.message.startswith(CONFIRM[target]), result.message
            assert CONFIRM[source] not in result.message
            stored = shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"]
            assert stored == target


def test_igbo_voice_to_hausa_is_not_left_in_igbo(shop, log_path):
    shop.execute("UPDATE customers SET language = 'ig' WHERE id = 1")
    shop.commit()
    result = handle_reply(
        shop,
        ActionLog(log_path),
        _OldLanguage(),
        1,
        "Make you talk to me for Hausa.",
        max_tokens=1000,
        from_voice=True,
    )
    assert result.language == "ha"
    assert result.message == CONFIRM["ha"]
    assert "Kedu" not in result.message
    assert shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"] == "ha"


def test_picture_request_keeps_the_offer_and_names_the_price(shop, log_path):
    cases = (
        (1, "pcm", "give me picture", "Make I hold"),
        (1, "pcm", "make I see am", "Make I hold"),
        (1, "pcm", "show me the rice", "Make I hold"),
        (2, "en", "send me the image", "set one aside"),
        (2, "en", "wetin e look like", "set one aside"),
    )
    for customer_id, language, text, cta in cases:
        log = ActionLog(log_path)
        shop.execute("UPDATE customers SET language = ? WHERE id = ?", (language, customer_id))
        shop.commit()
        draft = draft_winback(shop, log, MockMuse(), customer_id, max_tokens=1000, language=language)
        before = _orders(shop)
        result = handle_reply(shop, log, _NoImage(), customer_id, text, max_tokens=1000, from_voice=True)
        price = naira(draft.offer["offer_price_ngn"])
        lowered = result.message.lower()
        assert "Mama Gold" in result.message
        assert price in result.message
        assert cta.lower() in lowered
        assert "can't" not in lowered and "cannot" not in lowered
        assert result.order is None
        assert result.confirm_offer_id == draft.offer["offer_id"]
        assert result.image_offer_id == draft.offer["offer_id"]
        assert result.intent == "question"
        assert _orders(shop) == before
        state = shop.execute(
            "SELECT state FROM purchase_states WHERE offer_id = ? ORDER BY id DESC LIMIT 1",
            (draft.offer["offer_id"],),
        ).fetchone()["state"]
        assert state == "offer_presented"
        status = shop.execute(
            "SELECT status FROM offers WHERE id = ?",
            (draft.offer["offer_id"],),
        ).fetchone()["status"]
        assert status == "sent"


def test_picture_words_are_seeded_as_image():
    from convoplex.lexicon import SEED

    phrases = {phrase for phrase, _meaning, _example in SEED}
    for word in ("picture", "pic", "foto", "image", "flier"):
        assert word in phrases


def test_whatsapp_picture_sends_the_cached_card_and_the_buttons(shop, tmp_path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "uploaded-pic"})
        if b"messaging_product" in request.content and b"status" in request.content and b"read" in request.content:
            return httpx.Response(200, json={"success": True})
        body = json.loads(request.content)
        if body.get("type") == "image":
            return httpx.Response(200, json={"messages": [{"id": "wamid.PIC"}]})
        if body.get("type") == "interactive":
            titles = [button["reply"]["title"] for button in body["interactive"]["action"]["buttons"]]
            assert titles == ["Yes, hold am", "No, I no want"]
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
    drafted = client.post("/v1/customers/1/winback")
    offer_id = drafted.json()["offer"]["offer_id"]
    raw = json.dumps(_inbound("wamid.PICASK", DEMO_DIGITS, "give me picture")).encode()
    sent = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert sent.status_code == 200
    assert sent.json()["processed"] is True
    kinds = []
    for call in calls:
        if call.url.path.endswith("/media"):
            kinds.append("media")
            continue
        if not call.content or call.content[:1] != b"{":
            continue
        kinds.append(json.loads(call.content).get("type"))
    assert "media" in kinds
    assert "image" in kinds
    assert "interactive" in kinds
    assert shop.execute(
        "SELECT state FROM purchase_states WHERE offer_id = ? ORDER BY id DESC LIMIT 1",
        (offer_id,),
    ).fetchone()["state"] == "offer_presented"
