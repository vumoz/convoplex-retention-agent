"""Yes reserves the discounted win-back, and garbled voice is not a new price."""

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.languages import CONFIRM, explicit_language
from convoplex.llm import Completion
from convoplex.money import naira


class _Shelf:
    """What the live model did: quote the shelf price and echo the transcript."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content="Mama Gold Rice 50kg na ₦84,460. You mean cawonbarry?\nINTENT: question",
            finish_reason="stop",
        )


def _seed(shop) -> int:
    shop.execute("UPDATE customers SET full_name = 'Chinyere Musa', language = 'pcm' WHERE id = 1")
    shop.execute("UPDATE products SET price_ngn = 84460 WHERE id = 1")
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 0, 84460, 84460, 'shelf copy', 'pcm', 'declined', '2026-10-31T05:00:00+00:00')
        """
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 10, 84460, 76014, 'win-back', 'pcm', 'sent', '2026-10-31T04:00:00+00:00')
        """
    )
    shop.commit()
    return int(
        shop.execute(
            "SELECT id FROM offers WHERE customer_id = 1 AND offer_price_ngn = 76014"
        ).fetchone()["id"]
    )


def test_buy_yes_after_the_pitch_and_after_the_picture_uses_the_discount(shop, log_path):
    winback = _seed(shop)
    log = ActionLog(log_path)
    pitch = handle_reply(
        shop, log, _Shelf(), 1, "Nouna mama go da one buy.", max_tokens=1000, from_voice=True
    )
    assert naira(76014) in pitch.message
    assert "84,460" not in pitch.message
    assert pitch.confirm_offer_id == winback

    yes = handle_reply(shop, log, _Shelf(), 1, "buy_yes", max_tokens=1000)
    assert yes.intent == "accept"
    assert yes.order["ok"] is True
    assert yes.order["offer_id"] == winback
    assert yes.order["unit_price_ngn"] == 76014
    assert yes.order["total_ngn"] == 76014
    assert naira(76014) in yes.message
    assert "84,460" not in yes.message
    assert yes.message.count("76,014") == 1
    assert shop.execute(
        "SELECT status FROM offers WHERE id = ?", (winback,)
    ).fetchone()["status"] == "accepted"

    shop.execute("UPDATE offers SET status = 'sent' WHERE id = ?", (winback,))
    shop.commit()
    picture = handle_reply(
        shop, log, _Shelf(), 1, "make I see the picture.", max_tokens=1000, from_voice=True
    )
    assert picture.image_offer_id == winback
    assert naira(76014) in picture.message
    assert "84,460" not in picture.message
    again = handle_reply(shop, log, _Shelf(), 1, "buy_yes", max_tokens=1000)
    assert again.order["offer_id"] == winback
    assert again.order["unit_price_ngn"] == 76014
    assert again.message.count("76,014") == 1


def test_unstocked_brand_repitches_the_live_offer_price(shop, log_path):
    winback = _seed(shop)
    shop.execute("UPDATE customers SET language = 'yo' WHERE id = 1")
    shop.commit()
    result = handle_reply(shop, ActionLog(log_path), _Shelf(), 1, "Ṣé ẹ ní Caprice?", max_tokens=1000)
    assert "Caprice" in result.message
    assert naira(76014) in result.message
    assert "84,460" not in result.message
    assert result.confirm_offer_id == winback
    assert result.order is None


def test_garbled_hausa_and_broken_english_are_switches_not_prices(shop, log_path):
    for phrase in (
        "housa",
        "housea",
        "hausa",
        "ausa",
        "howsa",
        "owsa",
        "house a",
        "make we talk for Hausa",
        "cawonbarry rice",
        "cawonbarry",
    ):
        assert explicit_language(phrase) == "ha", phrase
    for phrase in (
        "brooking",
        "May I talk to me for brooking?",
        "brookin",
        "brooklyn",
        "brokeen",
        "broken english",
        "brooklyn english",
        "talk to me for Brooklyn English.",
        "pidgin",
        "pigin",
        "pidgeon",
        "No broken",
        "no, broken",
    ):
        assert explicit_language(phrase) == "pcm", phrase
    assert explicit_language("Please talk to me in English") == "en"
    assert explicit_language("The bag is broken") is None
    assert explicit_language("house") is None

    _seed(shop)
    shop.execute("UPDATE customers SET language = 'yo' WHERE id = 1")
    shop.commit()
    hausa = handle_reply(
        shop,
        ActionLog(log_path),
        _Shelf(),
        1,
        "cawonbarry rice",
        max_tokens=1000,
        from_voice=True,
    )
    assert hausa.language == "ha"
    assert hausa.message == CONFIRM["ha"]
    assert hausa.order is None
    assert "84,460" not in hausa.message
    assert "76,014" not in hausa.message
    assert "cawonbarry" not in hausa.message.lower()
    assert "you mean" not in hausa.message.lower()
    stored = shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"]
    assert stored == "ha"

    shop.execute("UPDATE customers SET language = 'yo' WHERE id = 1")
    shop.commit()
    broken = handle_reply(
        shop,
        ActionLog(log_path),
        _Shelf(),
        1,
        "No broken",
        max_tokens=1000,
        from_voice=True,
    )
    assert broken.language == "pcm"
    assert broken.intent != "decline"
    assert broken.order is None
    assert broken.message == CONFIRM["pcm"]
    assert "84,460" not in broken.message


def test_unclear_voice_repeats_the_offer_price(shop, log_path):
    _seed(shop)
    result = handle_reply(
        shop,
        ActionLog(log_path),
        _Shelf(),
        1,
        "blorptang something",
        max_tokens=1000,
        from_voice=True,
    )
    assert result.order is None
    assert naira(76014) in result.message
    assert "84,460" not in result.message
    assert "you mean" not in result.message.lower()
