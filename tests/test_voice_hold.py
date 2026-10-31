"""Broken English voice 'won't buy' asks once. A repeat never becomes an order."""

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.integrations.whatsapp import confirm_purchase_interactive
from convoplex.llm import Completion, MockMuse, classify_reply, explicit_refusal, purchase_is_ambiguous

# Live transcripts. The customer said "I wan buy …" each time. The note is heard as "won't".
LIVE_SEQUENCE = (
    "I won't buy rice.",
    "I won't buy. Mama gold.",
    "I won't buy.",
)


class _DeclineModel:
    """The live model heard 'wan' as 'won't' and tried to cancel."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content="I don cancel rice.\nINTENT: decline", finish_reason="stop")


def _orders(shop) -> int:
    return int(shop.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])


def _stock(shop) -> int:
    return int(shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"])


def test_wont_buy_is_not_an_explicit_no():
    for line in LIVE_SEQUENCE:
        assert purchase_is_ambiguous(line)
        assert explicit_refusal(line) is False
        assert classify_reply(line) == "question"
    assert explicit_refusal("I no want am")
    assert explicit_refusal("I no go buy")
    assert explicit_refusal("abeg no")
    assert explicit_refusal("No thank you, maybe next month")
    assert explicit_refusal("no wahala") is False
    assert classify_reply("No thank you, maybe next month") == "decline"


def test_pcm_voice_wont_buy_asks_once_and_does_not_order(shop, log_path):
    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    before = _orders(shop)
    stock = _stock(shop)
    results = [
        handle_reply(shop, log, _DeclineModel(), 1, line, max_tokens=1000, from_voice=True)
        for line in LIVE_SEQUENCE
    ]
    first = results[0]
    assert first.intent == "question"
    assert first.order is None
    assert first.confirm_offer_id
    assert first.language == "pcm"
    assert "₦70,200" in first.message
    assert "mama gold" in first.message.lower()
    assert "cancel" not in first.message.lower()
    assert _orders(shop) == before
    assert _stock(shop) == stock
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "sent"
    for later in results[1:]:
        assert later.order is None
        assert later.intent != "accept"
        assert later.confirm_offer_id is None
    assert _orders(shop) == before
    assert _stock(shop) == stock

    yes = handle_reply(shop, log, _DeclineModel(), 1, "ehen", max_tokens=1000)
    assert yes.intent == "accept"
    assert yes.order and yes.order["ok"]
    assert yes.order["total_ngn"] == 70200
    assert "₦70,200" in yes.message
    assert yes.message.count("70,200") == 1
    assert "pickup" in yes.message.lower()
    assert "delivery" in yes.message.lower()
    assert "₦2,500" in yes.message
    assert _stock(shop) == stock - 1


def test_english_refusal_repeated_does_not_order(shop, log_path):
    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 2, max_tokens=1000, language="en")
    before = _orders(shop)
    stock = _stock(shop)
    first = handle_reply(
        shop, log, _DeclineModel(), 2, "I won't buy rice.", max_tokens=1000, from_voice=True
    )
    assert first.intent != "decline"
    assert first.order is None
    assert first.confirm_offer_id
    assert "₦70,200" in first.message
    second = handle_reply(
        shop, log, _DeclineModel(), 2, "I won't buy rice.", max_tokens=1000, from_voice=False
    )
    assert second.intent != "decline"
    assert second.order is None
    assert "₦70,200" in second.message
    assert _orders(shop) == before
    assert _stock(shop) == stock


def test_explicit_i_no_want_am_still_declines(shop, log_path):
    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    result = handle_reply(
        shop, log, _DeclineModel(), 1, "I no want am", max_tokens=1000, from_voice=True
    )
    assert result.intent == "decline"
    assert result.order is None
    assert result.confirm_offer_id is None
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "declined"


def test_confirm_buttons_and_their_replies(shop, log_path):
    interactive = confirm_purchase_interactive("Mama Gold Rice 50kg na ₦70,200.", language="pcm")
    assert interactive["type"] == "button"
    buttons = interactive["action"]["buttons"]
    assert [button["reply"]["title"] for button in buttons] == ["Yes, hold am", "No, I no want"]
    assert [button["reply"]["id"] for button in buttons] == ["buy_yes", "buy_no"]
    assert all(len(button["reply"]["title"]) <= 20 for button in buttons)

    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    asked = handle_reply(shop, log, MockMuse(), 1, "I won't buy rice.", max_tokens=1000, from_voice=True)
    assert asked.confirm_offer_id
    assert "₦70,200" in asked.message
    yes = handle_reply(shop, log, _DeclineModel(), 1, "buy_yes", max_tokens=1000)
    assert yes.intent == "accept"
    assert yes.order and yes.order["ok"]
    assert "₦70,200" in yes.message

    log_no = ActionLog(log_path)
    draft_winback(shop, log_no, MockMuse(), 2, max_tokens=1000, language="en")
    no = handle_reply(shop, log_no, _DeclineModel(), 2, "buy_no", max_tokens=1000, from_voice=True)
    assert no.intent == "decline"
    assert no.order is None
    assert no.confirm_offer_id is None
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 2").fetchone()["status"] == "declined"


def test_no_button_and_typed_or_voice_no_do_not_buy(shop, log_path):
    from convoplex.integrations.whatsapp import CONFIRM_LABELS

    for language, (yes, no) in CONFIRM_LABELS.items():
        buttons = confirm_purchase_interactive("Hold?", language=language)["action"]["buttons"]
        assert [button["reply"]["title"] for button in buttons] == [yes, no]
        assert [button["reply"]["id"] for button in buttons] == ["buy_yes", "buy_no"]
        assert yes.lower() != no.lower()
        assert all(len(button["reply"]["title"]) <= 20 for button in buttons)
        assert len(buttons) <= 3

    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    before = _orders(shop)
    stock = _stock(shop)
    tapped = handle_reply(shop, log, _DeclineModel(), 1, "buy_no", max_tokens=1000)
    assert tapped.intent == "decline"
    assert tapped.order is None
    assert tapped.confirm_offer_id is None
    assert "we dey here when you ready" in tapped.message.lower()
    assert "₦" not in tapped.message
    assert _orders(shop) == before
    assert _stock(shop) == stock
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "declined"

    titled = handle_reply(shop, log, _DeclineModel(), 1, "No, I no want", max_tokens=1000, from_voice=True)
    assert titled.intent == "decline"
    assert titled.order is None
    assert _orders(shop) == before

    draft_winback(shop, log, MockMuse(), 2, max_tokens=1000, language="en")
    typed = handle_reply(shop, log, _DeclineModel(), 2, "no", max_tokens=1000, from_voice=False)
    assert typed.intent == "decline"
    assert typed.order is None
    assert "we are here when you are ready" in typed.message.lower()
    voice = handle_reply(shop, log, _DeclineModel(), 2, "No", max_tokens=1000, from_voice=True)
    assert voice.intent == "decline"
    assert voice.order is None
    assert _stock(shop) == stock

    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    yes = handle_reply(shop, log, _DeclineModel(), 1, "Yes, hold am", max_tokens=1000)
    assert yes.intent == "accept"
    assert yes.order and yes.order["ok"]
    assert _stock(shop) == stock - yes.order["qty"]
