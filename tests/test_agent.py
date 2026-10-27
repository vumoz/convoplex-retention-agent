import re

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.llm import Completion, classify_reply, split_intent
from convoplex.llm import MockMuse


def test_classify_and_split_intent():
    assert classify_reply("Yes o, no wahala. Abeg send am today.") == "accept"
    assert classify_reply("No thank you, maybe next month") == "decline"
    assert classify_reply("How much be the rice now?") == "question"
    assert classify_reply("ok") == "accept"
    text, intent = split_intent("E don set.\nINTENT: accept")
    assert intent == "accept"
    assert "INTENT" not in text


def test_pidgin_winback_and_accept_places_order(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    win = draft_winback(shop, log, llm, 1, max_tokens=1000)
    assert win.language == "pcm"
    assert "dey" in win.message.lower()
    assert "₦70,200" in win.message
    assert "Chinedu" in win.message
    assert "payday" in win.message.lower()
    assert win.tool_trace == ["check_stock", "get_price", "create_offer"]
    assert win.offer["offer_price_ngn"] == 70200
    assert "INTENT" not in win.message

    reply = handle_reply(shop, log, llm, 1, "Yes o, no wahala. Abeg send am today.", max_tokens=1000)
    assert reply.intent == "accept"
    assert reply.order["total_ngn"] == 70200
    assert "₦70,200" in reply.message
    stock = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    assert stock == 19
    verified = log.verify()
    assert verified.ok is True
    kinds = [entry["kind"] for entry in log.read_all()]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "message" in kinds
    assert all(entry["prev_hash"] for entry in log.read_all())


def test_english_winback_decline_and_question(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    win = draft_winback(shop, log, llm, 2, max_tokens=1000)
    assert win.language == "en"
    assert "It's been" in win.message
    assert re.search(r"\b(dey|abeg|wahala)\b", win.message.lower()) is None
    decline = handle_reply(shop, log, llm, 2, "No thank you, maybe next month", max_tokens=1000)
    assert decline.intent == "decline"
    assert decline.order is None
    status = shop.execute(
        "SELECT status FROM offers WHERE customer_id = 2"
    ).fetchone()["status"]
    assert status == "declined"
    stock_before = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    question = handle_reply(shop, log, llm, 2, "How much be the rice now?", max_tokens=1000)
    assert question.intent == "question"
    assert question.order is None
    assert "₦" in question.message
    stock_after = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    assert stock_after == stock_before


def test_out_of_stock_does_not_invent_an_offer(shop, log_path):
    shop.execute("UPDATE products SET stock_qty = 0 WHERE id = 1")
    result = draft_winback(shop, ActionLog(log_path), MockMuse(), 1, max_tokens=1000)
    assert result.offer is None
    assert "no dey" in result.message.lower()
    assert shop.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 0


def test_accept_without_a_tool_call_still_places_the_order(shop, log_path):
    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)

    class QuietModel:
        mode = "mock"

        def complete(self, messages, *, tools, max_tokens):
            del messages, tools, max_tokens
            return Completion(content="INTENT: accept", tool_calls=[], finish_reason="stop")

    reply = handle_reply(shop, log, QuietModel(), 1, "I will take it", max_tokens=1000)
    assert reply.intent == "accept"
    assert reply.order["ok"] is True
    assert "₦" in reply.message
    fallback = [
        entry
        for entry in log.read_all()
        if entry["kind"] == "tool_call" and entry["payload"].get("source") == "intent_fallback"
    ]
    assert len(fallback) == 1
    assert log.verify().ok is True


def test_active_customer_is_not_listed_as_at_risk(shop):
    from convoplex.context import get_customer, list_at_risk

    rows = list_at_risk(shop)
    ids = {row["id"] for row in rows}
    assert 1 in ids and 2 in ids
    assert 3 not in ids
    chinedu = get_customer(shop, 1)
    assert chinedu["segment"] == "lapsed"
    assert chinedu["recency_days"] == 89
    assert chinedu["frequency"] == 6
    assert any("Mama Gold" in reason for reason in chinedu["reasons"])
    tunde = get_customer(shop, 3)
    assert tunde["segment"] == "active"
    assert tunde["frequency"] == 5
