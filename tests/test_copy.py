"""Customer copy: one seasonal hook, and the delivery fee in both languages."""

from convoplex.llm import MockMuse
from convoplex.messages import (
    delivery_sentence,
    ensure_delivery_fee,
    one_seasonal_hook,
)
from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply


SAME_CITY = {
    "ok": True,
    "options": [
        {
            "method": "pickup",
            "available": True,
            "window": "same day",
            "fee_ngn": 0,
            "where": "Wuse Fresh Basket, Wuse",
        },
        {
            "method": "delivery",
            "available": True,
            "window": "1 to 2 days",
            "fee_ngn": 2000,
            "where": "Abuja",
        },
    ],
}

OTHER_CITY = {
    "ok": True,
    "options": [
        {
            "method": "pickup",
            "available": True,
            "window": "same day",
            "fee_ngn": 0,
            "where": "Wuse Fresh Basket, Wuse",
        },
        {
            "method": "delivery",
            "available": False,
            "window": None,
            "fee_ngn": None,
            "where": "Kano",
        },
    ],
}


def test_stacked_seasonal_hooks_collapse_to_the_payday_sentence():
    stacked = (
        "Hello Ada, this is the shop in Wuse. "
        "With Detty December and Christmas sewing approaching. "
        "Month-end is this week, a good time to restock. "
        "Reply YES and we will set it aside for you."
    )
    cleaned = one_seasonal_hook(stacked)
    assert "Detty" not in cleaned
    assert "Christmas" not in cleaned
    assert "Month-end is this week, a good time to restock." in cleaned
    assert cleaned.lower().count("month-end") == 1

    payday_only = "Month-end dey this week, e good time to stock up."
    assert one_seasonal_hook(payday_only) == payday_only
    festive_only = "Detty December is close, a good time to sew."
    assert one_seasonal_hook(festive_only) == festive_only


def test_delivery_fee_is_stated_in_english_and_pidgin():
    pidgin = delivery_sentence({"language": "pcm"}, SAME_CITY)
    english = delivery_sentence({"language": "en"}, SAME_CITY)
    assert "₦2,000" in pidgin
    assert "delivery fee na ₦2,000" in pidgin
    assert "pickup" in pidgin
    assert "₦2,000" in english
    assert "delivery fee ₦2,000" in english
    assert "pickup" in english

    omitted = (
        "E don set, Chinyere. We don reserve 1 x Ankara for you. "
        "Total na ₦18,000. You fit choose pickup or delivery."
    )
    patched = ensure_delivery_fee(omitted, {"language": "pcm"}, SAME_CITY)
    assert "Delivery fee na ₦2,000." in patched
    assert patched.startswith("E don set")

    english_omitted = "Done, Ada. The total is ₦18,000. Pickup or delivery."
    english_patched = ensure_delivery_fee(english_omitted, {"language": "en"}, SAME_CITY)
    assert "The delivery fee is ₦2,000." in english_patched

    away = delivery_sentence({"language": "pcm"}, OTHER_CITY)
    assert "₦" not in away
    assert ensure_delivery_fee("Total na ₦18,000.", {"language": "pcm"}, OTHER_CITY) == "Total na ₦18,000."


def test_live_style_drafts_keep_one_hook_and_the_fee(shop, log_path):
    """A draft that stacks hooks, or a confirmation that skips the fee, is repaired."""

    class StackedHooks:
        mode = "mock"

        def __init__(self):
            self.inner = MockMuse()

        def complete(self, messages, *, tools, max_tokens):
            completion = self.inner.complete(messages, tools=tools, max_tokens=max_tokens)
            if completion.content and not completion.tool_calls:
                completion.content = (
                    "Chinedu, how you dey? Na Mama Nkechi Provisions for Ikeja. "
                    "With Detty December and Christmas sewing approaching. "
                    "Month-end dey this week, e good time to stock up. "
                    "Mama Gold Rice 50kg still dey. I fit give you 10% off: ₦70,200 instead of ₦78,000. "
                    "Reply YES make we keep am for you."
                )
            return completion

    log = ActionLog(log_path)
    win = draft_winback(shop, log, StackedHooks(), 1, max_tokens=1000)
    assert "Detty" not in win.message
    assert "Christmas" not in win.message
    assert "Month-end dey this week, e good time to stock up." in win.message
    assert "₦70,200" in win.message

    class NoFee:
        mode = "mock"

        def __init__(self):
            self.inner = MockMuse()

        def complete(self, messages, *, tools, max_tokens):
            completion = self.inner.complete(messages, tools=tools, max_tokens=max_tokens)
            if completion.content and "INTENT: accept" in completion.content:
                completion.content = (
                    "E don set, Chinedu. We don reserve 1 x Mama Gold Rice 50kg for you. "
                    "Total na ₦70,200. You fit choose pickup (same day for the shop) "
                    "or delivery (1 to 2 days for Lagos).\nINTENT: accept"
                )
            return completion

    reply = handle_reply(shop, log, NoFee(), 1, "Yes o, no wahala. Abeg send am today.", max_tokens=1000)
    assert reply.intent == "accept"
    assert "₦70,200" in reply.message
    assert "pickup" in reply.message.lower()
    assert "delivery" in reply.message.lower()
    assert "delivery fee na ₦2,500" in reply.message
    assert "₦2,000" not in reply.message
