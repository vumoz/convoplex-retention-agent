"""An unstocked brand keeps the offer open and points at what the shop does sell."""

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.llm import MockMuse
from convoplex.money import naira
from convoplex.prompts import _RULES


class _GiveUp:
    """The live model closed the chat. Trusted code must not use that draft."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        raise AssertionError("an unstocked brand must not reach the model")


def _orders(shop) -> int:
    return int(shop.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])


def test_sales_policy_never_gives_up_on_an_open_offer():
    rules = _RULES.lower()
    assert "never give up on an open offer" in rules
    assert "brand question is not a decline" in rules
    assert "in-stock alternative" in rules


def test_unstocked_brand_reoffers_mama_gold_in_pcm_english_and_yoruba(shop, log_path):
    cases = (
        (1, "pcm", None, "you get Caprice?", "no dey"),
        (2, "en", "en", "Do you have Caprice?", "don't have"),
        (2, "yo", "yo", "Ṣé ẹ ní Caprice?", "kò sí"),
    )
    for customer_id, language, draft_language, text, unavailable in cases:
        log = ActionLog(log_path)
        shop.execute("UPDATE customers SET language = ? WHERE id = ?", (language, customer_id))
        shop.commit()
        draft = draft_winback(
            shop, log, MockMuse(), customer_id, max_tokens=1000, language=draft_language
        )
        price = naira(draft.offer["offer_price_ngn"])
        before = _orders(shop)
        result = handle_reply(shop, log, _GiveUp(), customer_id, text, max_tokens=1000)
        assert unavailable in result.message.lower(), result.message
        assert "Caprice" in result.message
        assert "Mama Gold" in result.message
        assert price in result.message
        assert "?" in result.message
        assert result.intent == "question"
        assert result.order is None
        assert result.confirm_offer_id == draft.offer["offer_id"]
        assert "whenever you" not in result.message.lower()
        assert "we dey for" not in result.message.lower()
        assert "you mean" not in result.message.lower()
        assert _orders(shop) == before
        status = shop.execute(
            "SELECT status FROM offers WHERE id = ?",
            (draft.offer["offer_id"],),
        ).fetchone()["status"]
        assert status == "sent"
        state = shop.execute(
            """
            SELECT state FROM purchase_states
            WHERE offer_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (draft.offer["offer_id"],),
        ).fetchone()["state"]
        assert state == "offer_presented"
        assert shop.execute(
            "SELECT id FROM lexicon WHERE phrase = 'caprice'"
        ).fetchone() is None

    log = ActionLog(log_path)
    shop.execute("UPDATE customers SET language = 'pcm' WHERE id = 1")
    shop.commit()
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    royal = handle_reply(shop, log, _GiveUp(), 1, "Royal Stallion", max_tokens=1000)
    assert "Royal Stallion" in royal.message
    assert "no dey" in royal.message.lower()
    assert "Mama Gold" in royal.message
    assert royal.order is None
    assert royal.confirm_offer_id

    refused = handle_reply(shop, log, _GiveUp(), 1, "I no want am", max_tokens=1000)
    assert refused.intent == "decline"
    assert refused.order is None
