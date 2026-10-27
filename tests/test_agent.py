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
    assert "don enter" not in win.message.lower()
    assert win.tool_trace == ["check_stock", "get_price", "create_offer"]
    assert win.offer["offer_price_ngn"] == 70200
    assert "INTENT" not in win.message

    reply = handle_reply(shop, log, llm, 1, "Yes o, no wahala. Abeg send am today.", max_tokens=1000)
    assert reply.intent == "accept"
    assert reply.order["total_ngn"] == 70200
    assert "₦70,200" in reply.message
    assert "pickup" in reply.message.lower()
    assert "delivery" in reply.message.lower()
    assert "how you dey" not in reply.message.lower()
    stock = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    assert stock == 19
    verified = log.verify()
    assert verified.ok is True
    kinds = [entry["kind"] for entry in log.read_all()]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "message" in kinds
    assert "model_call" in kinds
    calls = [entry for entry in log.read_all() if entry["kind"] == "model_call"]
    assert calls[0]["payload"]["usage"]["completion_tokens"] == 0
    assert calls[0]["payload"]["latency_ms"] == 0
    assert calls[0]["payload"]["attempt"] == 1
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
    assert "payday" not in decline.message.lower()
    status = shop.execute(
        "SELECT status FROM offers WHERE customer_id = 2"
    ).fetchone()["status"]
    assert status == "declined"
    stock_before = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    question = handle_reply(shop, log, llm, 2, "How much be the rice now?", max_tokens=1000)
    assert question.intent == "question"
    assert question.order is None
    assert question.tool_trace == []
    assert "₦70,200" in question.message
    assert "₦78,000" not in question.message
    assert "hello" not in question.message.lower()
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


def test_decline_then_question_then_accept_keeps_the_offer(shop, log_path):
    """The live failure: decline, ask the price, then take 2 at list price with no offer."""
    from convoplex.metrics import snapshot

    log = ActionLog(log_path)
    llm = MockMuse()
    win = draft_winback(shop, log, llm, 1, max_tokens=1000)
    assert win.offer["offer_price_ngn"] == 70200
    assert "Payday window open." in win.message
    assert "dey near" not in win.message.lower()

    decline = handle_reply(shop, log, llm, 1, "No thank you, maybe next month", max_tokens=1000)
    assert decline.intent == "decline"
    assert decline.order is None
    assert "payday" not in decline.message.lower()
    assert shop.execute("SELECT status FROM offers").fetchone()["status"] == "declined"

    question = handle_reply(shop, log, llm, 1, "How much last price?", max_tokens=1000)
    assert question.intent == "question"
    assert question.order is None
    assert question.tool_trace == []
    assert "₦70,200" in question.message
    assert "₦78,000" not in question.message
    assert "how you dey" not in question.message.lower()
    assert "Mama Nkechi" not in question.message

    accept = handle_reply(shop, log, llm, 1, "Ok I go take 2", max_tokens=1000)
    assert accept.intent == "accept"
    assert accept.order["offer_id"] == win.offer["offer_id"]
    assert accept.order["qty"] == 2
    assert accept.order["unit_price_ngn"] == 70200
    assert accept.order["total_ngn"] == 140400
    assert "₦140,400" in accept.message
    assert "₦78,000" not in accept.message
    assert "pickup" in accept.message.lower()
    assert "delivery" in accept.message.lower()
    assert "how you dey" not in accept.message.lower()
    assert "check_stock" not in accept.tool_trace
    assert "get_price" not in accept.tool_trace
    assert "get_delivery_options" in accept.tool_trace
    status = shop.execute("SELECT status FROM offers").fetchone()["status"]
    assert status == "accepted"
    totals = snapshot(shop, log.read_all())
    assert totals["offers_accepted"] == 1
    assert totals["offers_declined"] == 0
    assert totals["win_back_rate"] == 1.0
    assert totals["naira_recovered"] == 140400


def test_language_override_and_prompt_guardrails(shop, log_path):
    from convoplex.prompts import build_messages

    win = draft_winback(shop, ActionLog(log_path), MockMuse(), 2, max_tokens=1000, language="pcm")
    assert win.language == "pcm"
    assert "dey" in win.message.lower()
    stored = shop.execute("SELECT language FROM offers WHERE customer_id = 2").fetchone()["language"]
    assert stored == "pcm"
    system = build_messages(shop, 1, "winback")[0]["content"]
    assert "tank you" in system
    assert "get_delivery_options" in system
    assert "don enter" in system
    assert "CONTEXT_JSON:" in system
    context = system.split("CONTEXT_JSON:\n", 1)[1]
    assert '"today": "2026-10-27"' in context
    assert '"status": "open"' in context
