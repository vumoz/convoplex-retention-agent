"""Voice 'won't buy rice' is 'wan buy rice'. It pitches offer 6. It does not decline.

muse-voice-transcribe-1.0 writes Broken English 'wan' as 'won't'. Customer 184,
seq 1209 and 1216, were answered with a goodbye while language was English.
The same goodbye happened earlier in Yoruba. A bare no, with no product, still declines.
"""

import pytest

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.llm import Completion, explicit_refusal
from convoplex.money import naira

# Live transcripts. Victor said "My customer, I wan buy rice".
TRANSCRIPT_1209 = "My customer, I won't buy rice."
TRANSCRIPT_1216 = "My customer, I won't I won't buy rice."
PRICE = 76014
SHELF = 84460


class _DeclineModel:
    """The model that produced the live goodbye."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content="No problem, Chinyere. We are here when you are ready.\nINTENT: decline",
            finish_reason="stop",
        )


def _seed(shop, language: str) -> int:
    shop.execute(
        "UPDATE customers SET full_name = 'Chinyere Musa', language = ? WHERE id = 1",
        (language,),
    )
    shop.execute("UPDATE products SET price_ngn = ? WHERE id = 1", (SHELF,))
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 10, ?, ?, 'win-back', ?, 'sent', '2026-10-31T04:00:00+00:00')
        """,
        (SHELF, PRICE, language),
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 0, ?, ?, 'shelf copy', ?, 'declined', '2026-10-31T05:00:00+00:00')
        """,
        (SHELF, SHELF, language),
    )
    shop.commit()
    return int(
        shop.execute(
            "SELECT id FROM offers WHERE customer_id = 1 AND offer_price_ngn = ?",
            (PRICE,),
        ).fetchone()["id"]
    )


def _orders(shop) -> int:
    return int(shop.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])


def _assert_pitch(result, winback: int, *, greeted: bool):
    assert result.intent != "decline"
    assert result.intent == "question"
    assert result.order is None
    assert result.confirm_offer_id == winback
    assert "Mama Gold" in result.message
    assert naira(PRICE) in result.message
    assert "84,460" not in result.message
    assert "No problem" not in result.message
    assert "No wahala" not in result.message
    assert "here when you" not in result.message.lower()
    if greeted:
        assert result.message.startswith("My customer")
    else:
        assert not result.message.startswith("My customer")


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
@pytest.mark.parametrize(
    "transcript",
    [TRANSCRIPT_1209, TRANSCRIPT_1216],
    ids=["1209", "1216"],
)
def test_voice_wont_buy_rice_with_greeting_pitches_the_winback(shop, log_path, language, transcript):
    winback = _seed(shop, language)
    before = _orders(shop)
    result = handle_reply(
        shop, ActionLog(log_path), _DeclineModel(), 1, transcript, max_tokens=1000, from_voice=True
    )
    _assert_pitch(result, winback, greeted=True)
    assert result.language == language
    assert _orders(shop) == before
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (winback,)).fetchone()["status"] == "sent"


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
@pytest.mark.parametrize(
    "transcript",
    ["I won't buy rice.", "I won't I won't buy rice."],
    ids=["1209", "1216"],
)
def test_voice_wont_buy_rice_without_greeting_still_pitches(shop, log_path, language, transcript):
    winback = _seed(shop, language)
    result = handle_reply(
        shop, ActionLog(log_path), _DeclineModel(), 1, transcript, max_tokens=1000, from_voice=True
    )
    _assert_pitch(result, winback, greeted=False)
    assert result.language == language


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
@pytest.mark.parametrize(
    "transcript",
    ["I wont buy rice.", "I want buy rice.", "I wan buy rice.", "won't won't buy rice."],
)
def test_voice_spellings_of_wan_buy_rice_pitch(shop, log_path, language, transcript):
    winback = _seed(shop, language)
    result = handle_reply(
        shop, ActionLog(log_path), _DeclineModel(), 1, transcript, max_tokens=1000, from_voice=True
    )
    _assert_pitch(result, winback, greeted=False)


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
def test_typed_wont_buy_rice_pitches_and_does_not_decline(shop, log_path, language):
    winback = _seed(shop, language)
    result = handle_reply(
        shop,
        ActionLog(log_path),
        _DeclineModel(),
        1,
        "I won't buy rice",
        max_tokens=1000,
        from_voice=False,
    )
    _assert_pitch(result, winback, greeted=False)


@pytest.mark.parametrize(
    "text",
    ["No", "I no want", "No, I no want", "buy_no", "I don't want it"],
)
def test_clear_no_without_a_product_still_declines(shop, log_path, text):
    winback = _seed(shop, "en")
    before = _orders(shop)
    result = handle_reply(
        shop, ActionLog(log_path), _DeclineModel(), 1, text, max_tokens=1000, from_voice=True
    )
    assert result.intent == "decline"
    assert result.order is None
    assert result.confirm_offer_id is None
    assert naira(PRICE) not in result.message
    assert "Mama Gold" not in result.message
    assert _orders(shop) == before
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (winback,)).fetchone()["status"] == "declined"


def test_english_wont_buy_without_a_product_still_declines(shop, log_path):
    winback = _seed(shop, "en")
    result = handle_reply(
        shop, ActionLog(log_path), _DeclineModel(), 1, "I won't buy.", max_tokens=1000, from_voice=True
    )
    assert result.intent == "decline"
    assert result.order is None
    assert result.confirm_offer_id is None
    assert naira(PRICE) not in result.message
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (winback,)).fetchone()["status"] == "declined"


def test_i_dont_want_it_is_an_explicit_no():
    assert explicit_refusal("I don't want it")
    assert explicit_refusal("I dont want it")
    assert explicit_refusal("I no want")
    assert explicit_refusal("No")
    assert explicit_refusal(TRANSCRIPT_1209) is False
    assert explicit_refusal(TRANSCRIPT_1216) is False
