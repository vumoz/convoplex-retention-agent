"""The discounted win-back is the price, and Pidgin English is a language switch."""

from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.context import retire_zero_discount_duplicates
from convoplex.integrations.images import overlay_copy
from convoplex.languages import CONFIRM, explicit_language
from convoplex.llm import Completion
from convoplex.money import naira
from tests.test_api import _app


class _Echo:
    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content="let's talking Broken English. Ọja ni ₦84,460.\nINTENT: question",
            finish_reason="stop",
        )


class _Cap:
    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content="Cap Rice na ₦40,788.\nINTENT: question", finish_reason="stop")


def _product(shop, offer_id: int) -> str:
    return shop.execute(
        """
        SELECT p.name FROM offers o JOIN products p ON p.id = o.product_id WHERE o.id = ?
        """,
        (offer_id,),
    ).fetchone()["name"]


def _seed(shop) -> int:
    shop.execute("UPDATE customers SET full_name = 'Chinyere Musa', language = 'pcm' WHERE id = 1")
    shop.execute("UPDATE products SET price_ngn = 84460 WHERE id = 1")
    shop.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (1, 'RICE-25', 'Cap Rice 25kg', 'provisions', 'bag', 45320, 44)
        """
    )
    cap_id = shop.execute("SELECT id FROM products WHERE sku = 'RICE-25'").fetchone()["id"]
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, ?, 10, 45320, 40788, 'old', 'pcm', 'declined', '2026-10-30T18:00:00+00:00')
        """,
        (cap_id,),
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 10, 84460, 76014, 'win-back', 'pcm', 'sent', '2026-10-31T04:00:00+00:00')
        """
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 0, 84460, 84460, 'shelf copy', 'pcm', 'accepted', '2026-10-31T05:00:00+00:00')
        """
    )
    shop.commit()
    return int(
        shop.execute(
            "SELECT id FROM offers WHERE customer_id = 1 AND discount_percent = 10 AND product_id = 1"
        ).fetchone()["id"]
    )


def test_picture_and_yes_use_the_discounted_win_back_not_the_zero_row(shop, log_path):
    winback = _seed(shop)
    before = shop.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"]
    log = ActionLog(log_path)
    picture = handle_reply(
        shop, log, _Cap(), 1, "Make I see the picture. picture.", max_tokens=1000, from_voice=True
    )
    assert picture.image_offer_id == winback
    assert _product(shop, picture.image_offer_id) == "Mama Gold Rice 50kg"
    assert naira(76014) in picture.message
    assert "84,460" not in picture.message
    assert "Cap Rice" not in picture.message
    card = overlay_copy(
        {
            "retailer_name": "Wuse Fresh Basket",
            "city": "Abuja",
            "product_name": "Mama Gold Rice 50kg",
            "offer_price_ngn": 76014,
            "list_price_ngn": 84460,
            "discount_percent": 10,
            "language": "pcm",
        }
    )
    assert card["deal"] is True
    assert card["off"] == "10% OFF"
    assert card["was"] == naira(84460)
    assert card["price"] == naira(76014)

    yes = handle_reply(shop, log, _Cap(), 1, "buy_yes", max_tokens=1000)
    assert yes.order["ok"] is True
    assert yes.order["offer_id"] == winback
    assert yes.order["unit_price_ngn"] == 76014
    assert naira(76014) in yes.message
    assert shop.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == before

    shelf = shop.execute(
        "SELECT id FROM offers WHERE customer_id = 1 AND discount_percent = 0"
    ).fetchone()["id"]
    retired = retire_zero_discount_duplicates(shop)
    assert shelf in retired
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (shelf,)).fetchone()["status"] == "declined"
    assert shop.execute("SELECT status FROM offers WHERE id = ?", (winback,)).fetchone()["status"] == "accepted"


def test_retire_endpoint_lists_the_zero_percent_copy(shop, tmp_path):
    _seed(shop)
    client = TestClient(_app(shop, tmp_path))
    response = client.post("/v1/offers/retire-shelf-duplicates")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "retired_offer_ids" in body
    left = shop.execute(
        """
        SELECT id, status, discount_percent FROM offers
        WHERE customer_id = 1 AND product_id = 1
        ORDER BY id
        """
    ).fetchall()
    discounted = [row for row in left if int(row["discount_percent"]) == 10]
    zeros = [row for row in left if int(row["discount_percent"]) == 0]
    assert discounted
    assert discounted[0]["status"] == "sent"
    assert zeros
    assert all(row["status"] == "declined" for row in zeros)


def test_garbled_pidgin_english_switches_from_yoruba_without_echoing(shop, log_path):
    assert explicit_language("let's talking um Pidgin English") == "pcm"
    assert explicit_language("Pidgin English") == "pcm"
    assert explicit_language("Broken English") == "pcm"
    assert explicit_language("talk pidgin") == "pcm"
    assert explicit_language("Please talk to me in English") == "en"
    assert explicit_language("The bag is broken") is None
    shop.execute("UPDATE customers SET language = 'yo' WHERE id = 1")
    shop.commit()
    result = handle_reply(
        shop,
        ActionLog(log_path),
        _Echo(),
        1,
        "let's talking um Pidgin English",
        max_tokens=1000,
        from_voice=True,
    )
    assert result.language == "pcm"
    assert result.message == CONFIRM["pcm"]
    assert "pidgin" not in result.message.lower()
    assert "let's" not in result.message.lower()
    assert "talking" not in result.message.lower()
    assert "84,460" not in result.message
    stored = shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"]
    assert stored == "pcm"
