"""A yes reserves the pitched bag. It does not ask the same question again.

The first Yes marks offer 6 accepted. place_order used to reject that status,
the accept path became a question, and the empty reply was rewritten as another
pitch. An accepted win-back is still the bag, at its own price.
"""

from pathlib import Path

import pytest

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.config import Settings
from convoplex.integrations.images import load_offer_for_card, promo_card_key, remember_promo
from convoplex.llm import Completion
from convoplex.money import naira
from convoplex.whatsapp_flow import send_promo

PRICE = 76014
SHELF = 84460


class _Repitch:
    """What the live model did once Yes stopped reserving: ask again."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content=(
                "Chinyere, Mama Gold Rice 50kg is ₦76,014 right now, and we have 28 in stock. "
                "Would you like us to set one aside?\nINTENT: question"
            ),
            finish_reason="stop",
        )


def _seed(shop, language: str, *, status: str = "sent") -> int:
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
        ) VALUES (1, 1, 1, 10, ?, ?, 'win-back', ?, ?, '2026-10-31T04:00:00+00:00')
        """,
        (SHELF, PRICE, language, status),
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


def _offer_orders(shop, offer_id: int) -> int:
    return int(
        shop.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()["n"]
    )


def _stock(shop) -> int:
    return int(shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"])


def _assert_reserved(result, language: str, winback: int):
    assert result.intent == "accept"
    assert result.order and result.order["ok"] is True
    assert result.order["offer_id"] == winback
    assert result.order["qty"] == 1
    assert result.order["unit_price_ngn"] == PRICE
    assert result.order["total_ngn"] == PRICE
    assert result.message.count("76,014") == 1
    assert "84,460" not in result.message
    lowered = result.message.lower()
    assert "set one aside" not in lowered
    assert "keep one" not in lowered
    assert "right now" not in lowered
    assert "would you like" not in lowered
    if language == "pcm":
        assert result.message.startswith("E don set, Chinyere. We don reserve 1 x Mama Gold Rice 50kg for you.")
        assert "Total na ₦76,014." in result.message
        assert "pickup" in lowered
        assert "delivery" in lowered
    elif language == "en":
        assert result.message.startswith("Done, Chinyere. We have reserved 1 x Mama Gold Rice 50kg for you.")
        assert f"The total is {naira(PRICE)}." in result.message
        assert "pickup" in lowered
        assert "delivery" in lowered
    else:
        assert result.message.startswith("Ó ti ṣe, Chinyere. A ti fi 1 x Mama Gold Rice 50kg pamọ́ fún ọ.")
        assert naira(PRICE) in result.message


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
def test_buy_yes_after_the_pitch_reserves_and_a_second_yes_confirms_it(shop, log_path, language):
    winback = _seed(shop, language)
    log = ActionLog(log_path)
    stock = _stock(shop)
    pitch = handle_reply(
        shop,
        log,
        _Repitch(),
        1,
        "My customer, I won't buy rice.",
        max_tokens=1000,
        from_voice=True,
    )
    assert pitch.intent != "decline"
    assert pitch.confirm_offer_id == winback
    assert pitch.message.startswith("My customer")
    assert naira(PRICE) in pitch.message

    yes = handle_reply(shop, log, _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(yes, language, winback)
    assert _offer_orders(shop, winback) == 1
    assert _stock(shop) == stock - 1
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (winback,)).fetchone()["status"] == "accepted"

    again = handle_reply(shop, log, _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(again, language, winback)
    assert again.order["order_id"] == yes.order["order_id"]
    assert again.order.get("already_reserved") is True
    assert _offer_orders(shop, winback) == 1
    assert _stock(shop) == stock - 1


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
def test_buy_yes_after_the_picture_reserves(shop, log_path, language):
    winback = _seed(shop, language)
    log = ActionLog(log_path)
    picture = handle_reply(
        shop, log, _Repitch(), 1, "show me the rice picture", max_tokens=1000, from_voice=True
    )
    assert picture.image_offer_id == winback
    assert picture.order is None
    assert naira(PRICE) in picture.message
    yes = handle_reply(shop, log, _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(yes, language, winback)
    assert _offer_orders(shop, winback) == 1


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
def test_buy_yes_reserves_when_the_offer_is_already_accepted(shop, log_path, language):
    winback = _seed(shop, language, status="accepted")
    yes = handle_reply(shop, ActionLog(log_path), _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(yes, language, winback)
    assert _offer_orders(shop, winback) == 1
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (winback,)).fetchone()["status"] == "accepted"
    assert shop.execute(
        "SELECT offer_price_ngn FROM offers WHERE id = ?", (winback,)
    ).fetchone()["offer_price_ngn"] == PRICE


@pytest.mark.parametrize("language", ["en", "pcm", "yo"])
@pytest.mark.parametrize(
    "text",
    ["Yes keep am.", "Yes", "Yes, hold am", "hold am", "keep am", "ok", "abeg keep one"],
)
def test_clear_yes_phrases_reserve_one_bag(shop, log_path, language, text):
    winback = _seed(shop, language)
    result = handle_reply(
        shop, ActionLog(log_path), _Repitch(), 1, text, max_tokens=1000, from_voice=False
    )
    _assert_reserved(result, language, winback)
    assert _offer_orders(shop, winback) == 1


def test_a_wrong_price_order_is_not_the_reservation_we_confirm(shop, log_path):
    winback = _seed(shop, "pcm", status="accepted")
    shop.execute(
        """
        INSERT INTO orders (retailer_id, customer_id, ordered_on, status, total_ngn, channel, offer_id)
        VALUES (1, 1, '2026-10-31', 'completed', ?, 'whatsapp_sim', ?)
        """,
        (SHELF, winback),
    )
    order_id = int(shop.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    shop.execute(
        """
        INSERT INTO order_items (order_id, product_id, qty, unit_price_ngn, line_total_ngn)
        VALUES (?, 1, 1, ?, ?)
        """,
        (order_id, SHELF, SHELF),
    )
    shop.commit()
    yes = handle_reply(shop, ActionLog(log_path), _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(yes, "pcm", winback)
    assert yes.order["order_id"] != order_id
    assert yes.order["unit_price_ngn"] == PRICE
    assert _offer_orders(shop, winback) == 2


def test_add_one_more_reserves_another_bag(shop, log_path):
    winback = _seed(shop, "pcm")
    log = ActionLog(log_path)
    stock = _stock(shop)
    first = handle_reply(shop, log, _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(first, "pcm", winback)
    more = handle_reply(shop, log, _Repitch(), 1, "abeg add one more", max_tokens=1000)
    assert more.intent == "accept"
    assert more.order["ok"] is True
    assert more.order["qty"] == 1
    assert more.order["unit_price_ngn"] == PRICE
    assert more.order["order_id"] != first.order["order_id"]
    assert "set one aside" not in more.message.lower()
    assert _offer_orders(shop, winback) == 2
    assert _stock(shop) == stock - 2


def test_resend_leaves_the_accepted_offer_buyable_at_the_discount(shop, log_path, tmp_path, monkeypatch):
    winback = _seed(shop, "pcm", status="accepted")
    card = load_offer_for_card(shop, winback)
    remember_promo(shop, b"promo-bytes", tmp_path / "output", winback, card_key=promo_card_key(card))
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    settings = Settings(
        db_path=Path(db_file),
        log_path=log_path,
        output_dir=tmp_path / "output",
        channel="whatsapp",
        whatsapp_token="token",
        whatsapp_phone_number_id="123456789",
        whatsapp_demo_to="+1 555 000 1111",
    )
    monkeypatch.setattr(
        "convoplex.whatsapp_flow.deliver",
        lambda *args, **kwargs: {"sent": "image"},
    )
    sent = send_promo(settings, "whatsapp", ActionLog(log_path), None, None, winback)
    assert sent["sent"] == "image"
    row = shop.execute(
        "SELECT status, offer_price_ngn FROM offers WHERE id = ?",
        (winback,),
    ).fetchone()
    assert row["status"] == "accepted"
    assert int(row["offer_price_ngn"]) == PRICE
    yes = handle_reply(shop, ActionLog(log_path), _Repitch(), 1, "buy_yes", max_tokens=1000)
    _assert_reserved(yes, "pcm", winback)
