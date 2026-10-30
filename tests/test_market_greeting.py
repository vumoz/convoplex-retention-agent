"""'My customer' is a greeting, not a brand the shop is out of."""

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


def test_a_salutation_is_not_learned_or_a_missing_product(shop, log_path):
    refused = learn_phrase(shop, "costoma", "pcm", "a person", customer_id=1)
    assert refused["ok"] is False
    log, draft = _prepare(shop, log_path, 1, "pcm")
    assert unstocked_request(shop, "Customer", {"retailer_id": 1, "open_offer": draft.offer}) is None
    assert unstocked_request(shop, "kustoma", {"retailer_id": 1, "open_offer": draft.offer}) is None
    assert log is not None
