"""Pidgin 'wan buy' is heard as 'won't buy'. An open offer should be accepted, not asked forever."""

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.integrations.whatsapp import confirm_purchase_interactive
from convoplex.llm import Completion, MockMuse, classify_reply, explicit_refusal, purchase_is_ambiguous

# Live transcripts. The customer said "I wan buy …" each time.
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


def test_pcm_voice_sequence_accepts_on_the_first_message(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    draft_winback(shop, log, llm, 1, max_tokens=1000)
    results = [
        handle_reply(shop, log, _DeclineModel(), 1, line, max_tokens=1000, from_voice=True)
        for line in LIVE_SEQUENCE
    ]
    first = results[0]
    assert first.intent == "accept"
    assert first.order and first.order["ok"]
    assert first.order["total_ngn"] == 70200
    assert first.language == "pcm"
    assert "Mama Gold Rice 50kg na ₦70,200." in first.message
    assert "pickup" in first.message.lower()
    assert "delivery" in first.message.lower()
    assert "₦2,500" in first.message
    assert "You wan buy am abi you no want am?" not in first.message
    assert "cancel" not in first.message.lower()
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "accepted"
    assert shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"] == 19


def test_english_voice_asks_the_confirm_only_once(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    draft_winback(shop, log, llm, 2, max_tokens=1000, language="en")
    first = handle_reply(
        shop, log, _DeclineModel(), 2, "I won't buy rice.", max_tokens=1000, from_voice=True
    )
    assert first.intent == "question"
    assert first.order is None
    assert first.confirm_offer_id
    assert first.message == "Do you want to buy it, or not? Mama Gold Rice 50kg is ₦70,200."
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 2").fetchone()["status"] == "sent"

    second = handle_reply(
        shop, log, _DeclineModel(), 2, "I won't buy rice.", max_tokens=1000, from_voice=True
    )
    assert second.intent == "accept"
    assert second.order and second.order["ok"]
    assert "Do you want to buy it, or not?" not in second.message
    assert "Mama Gold Rice 50kg is ₦70,200." in second.message
    assert "₦2,500" in second.message
    assert "pickup" in second.message.lower() and "delivery" in second.message.lower()

    third = handle_reply(
        shop, log, _DeclineModel(), 2, "I won't buy.", max_tokens=1000, from_voice=True
    )
    assert "Do you want to buy it, or not?" not in third.message


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
    interactive = confirm_purchase_interactive("Do you want to buy it, or not? Mama Gold Rice 50kg is ₦70,200.")
    assert interactive["type"] == "button"
    buttons = interactive["action"]["buttons"]
    assert [button["reply"]["title"] for button in buttons] == ["Yes, I want it", "No"]
    assert [button["reply"]["id"] for button in buttons] == ["buy_yes", "buy_no"]
    assert all(len(button["reply"]["title"]) <= 20 for button in buttons)

    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 2, max_tokens=1000, language="en")
    asked = handle_reply(shop, log, MockMuse(), 2, "I won't buy rice.", max_tokens=1000, from_voice=True)
    assert asked.confirm_offer_id
    yes = handle_reply(shop, log, _DeclineModel(), 2, "buy_yes", max_tokens=1000)
    assert yes.intent == "accept"
    assert yes.order and yes.order["ok"]
    assert "₦70,200" in yes.message

    log_no = ActionLog(log_path)
    # A second offer is not created for customer 2 because the yes above accepted it.
    draft_winback(shop, log_no, MockMuse(), 1, max_tokens=1000)
    no = handle_reply(shop, log_no, _DeclineModel(), 1, "buy_no", max_tokens=1000, from_voice=True)
    assert no.intent == "decline"
    assert no.order is None
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "declined"
