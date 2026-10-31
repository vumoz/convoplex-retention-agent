"""A named brand replaces the offer. Plain rice stays on the win-back product."""

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.llm import Completion
from convoplex.money import naira
from convoplex.tools import is_generic_rice, named_stocked_product


class _CapRiceModel:
    """The live model kept talking about the previous bag."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content="Chinedu, Cap Rice 25kg na ₦40,788 now, and we get 44 for shop.\nINTENT: question",
            finish_reason="stop",
        )


def _seed_cap_rice(shop) -> None:
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
        ) VALUES (1, 1, ?, 10, 45320, 40788, 'sticky', 'pcm', 'sent', '2026-10-30T18:00:00+00:00')
        """,
        (cap_id,),
    )
    shop.commit()


def _product(shop, offer_id: int) -> str:
    return shop.execute(
        """
        SELECT p.name FROM offers o
        JOIN products p ON p.id = o.product_id
        WHERE o.id = ?
        """,
        (offer_id,),
    ).fetchone()["name"]


def test_generic_rice_and_a_named_brand_are_not_the_same():
    assert is_generic_rice("Customer, I wan buy rice")
    assert is_generic_rice("I wan buy rice")
    assert is_generic_rice("How much be the rice now?")
    assert not is_generic_rice("What about Mama Gold Rice")
    assert not is_generic_rice("What about Cap Rice")
    assert not is_generic_rice("you get Caprice?")


def test_voice_rice_then_no_then_mama_gold(shop, log_path):
    """Victor's transcript: rice, No, then the brand by name. Cap Rice must not stick."""
    _seed_cap_rice(shop)
    log = ActionLog(log_path)
    first = handle_reply(
        shop,
        log,
        _CapRiceModel(),
        1,
        "Customer, I wan buy rice",
        max_tokens=1000,
        from_voice=True,
    )
    assert first.order is None
    assert first.confirm_offer_id
    assert _product(shop, first.confirm_offer_id) == "Mama Gold Rice 50kg"
    assert "Mama Gold" in first.message
    assert naira(70200) in first.message
    assert "Cap Rice" not in first.message
    assert "40,788" not in first.message
    assert first.message.startswith("Customer,")

    refused = handle_reply(shop, log, _CapRiceModel(), 1, "No, I no want", max_tokens=1000)
    assert refused.intent == "decline"
    assert refused.order is None
    assert "no wahala" in refused.message.lower()
    assert "chinedu" in refused.message.lower()

    named = handle_reply(shop, log, _CapRiceModel(), 1, "What about Mama Gold Rice", max_tokens=1000)
    assert named.intent == "question"
    assert named.order is None
    assert named.confirm_offer_id
    assert named.confirm_offer_id != first.confirm_offer_id
    assert _product(shop, named.confirm_offer_id) == "Mama Gold Rice 50kg"
    assert "Mama Gold" in named.message
    assert naira(70200) in named.message
    assert "Cap Rice" not in named.message
    assert "40,788" not in named.message
    assert "20" in named.message
    status = shop.execute(
        "SELECT status FROM offers WHERE id = ?",
        (named.confirm_offer_id,),
    ).fetchone()["status"]
    assert status == "sent"


def test_plain_rice_without_the_address_still_leads_with_mama_gold(shop, log_path):
    _seed_cap_rice(shop)
    result = handle_reply(
        shop,
        ActionLog(log_path),
        _CapRiceModel(),
        1,
        "I wan buy rice",
        max_tokens=1000,
        from_voice=True,
    )
    assert result.confirm_offer_id, result.message
    assert _product(shop, result.confirm_offer_id) == "Mama Gold Rice 50kg"
    assert "Mama Gold" in result.message
    assert "Cap Rice" not in result.message
    assert naira(70200) in result.message


def test_brand_switch_both_ways(shop, log_path):
    _seed_cap_rice(shop)
    log = ActionLog(log_path)
    mama = handle_reply(shop, log, _CapRiceModel(), 1, "What about Mama Gold", max_tokens=1000)
    assert _product(shop, mama.confirm_offer_id) == "Mama Gold Rice 50kg"
    assert naira(70200) in mama.message
    assert "Cap Rice" not in mama.message

    cap = handle_reply(shop, log, _CapRiceModel(), 1, "What about Cap Rice", max_tokens=1000)
    assert cap.confirm_offer_id != mama.confirm_offer_id
    assert _product(shop, cap.confirm_offer_id) == "Cap Rice 25kg"
    assert "Cap Rice" in cap.message
    assert naira(40788) in cap.message
    assert "Mama Gold" not in cap.message
    assert "44" in cap.message

    back = handle_reply(shop, log, _CapRiceModel(), 1, "What about Mama Gold Rice", max_tokens=1000)
    assert _product(shop, back.confirm_offer_id) == "Mama Gold Rice 50kg"
    assert naira(70200) in back.message
    assert "Cap Rice" not in back.message

    fresh = ActionLog(log_path)
    shop.execute("DELETE FROM purchase_states")
    shop.execute("DELETE FROM offers")
    shop.commit()
    cap_first = handle_reply(shop, fresh, _CapRiceModel(), 1, "What about Cap Rice", max_tokens=1000)
    assert _product(shop, cap_first.confirm_offer_id) == "Cap Rice 25kg"
    mama_next = handle_reply(
        shop, fresh, _CapRiceModel(), 1, "What about Mama Gold Rice", max_tokens=1000
    )
    assert _product(shop, mama_next.confirm_offer_id) == "Mama Gold Rice 50kg"
    assert mama_next.confirm_offer_id != cap_first.confirm_offer_id


def test_picture_follows_the_brand_being_discussed(shop, log_path):
    _seed_cap_rice(shop)
    log = ActionLog(log_path)
    cap = handle_reply(shop, log, _CapRiceModel(), 1, "What about Cap Rice", max_tokens=1000)
    picture = handle_reply(shop, log, _CapRiceModel(), 1, "show me the picture", max_tokens=1000)
    assert picture.image_offer_id == cap.confirm_offer_id
    assert _product(shop, picture.image_offer_id) == "Cap Rice 25kg"

    switched = handle_reply(
        shop, log, _CapRiceModel(), 1, "show me the mama gold picture", max_tokens=1000
    )
    assert _product(shop, switched.image_offer_id) == "Mama Gold Rice 50kg"
    assert switched.image_offer_id != cap.confirm_offer_id


def test_caprice_is_still_not_cap_rice(shop, log_path):
    _seed_cap_rice(shop)
    assert named_stocked_product(shop, 1, "you get Caprice?") is None
    log = ActionLog(log_path)
    handle_reply(shop, log, _CapRiceModel(), 1, "Customer, I wan buy rice", max_tokens=1000)
    missing = handle_reply(shop, log, _CapRiceModel(), 1, "you get Caprice?", max_tokens=1000)
    assert "caprice" in missing.message.lower()
    assert "no dey" in missing.message.lower()
    assert "Mama Gold" in missing.message
    assert "Cap Rice" not in missing.message
    assert missing.order is None
