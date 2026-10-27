from datetime import date

from convoplex.context import local_context, payday_calendar
from convoplex.scoring import OrderFact, classify, score_orders


def _facts(days: list[str], total: int = 10_000) -> list[OrderFact]:
    return [OrderFact(date.fromisoformat(day), total) for day in days]


def test_regular_then_quiet_is_lapsed_with_reasons():
    orders = _facts(
        ["2026-05-01", "2026-05-19", "2026-06-06", "2026-06-24", "2026-07-12", "2026-07-30"]
    )
    scored = score_orders(orders, date(2026, 10, 27))
    assert scored.segment == "lapsed"
    assert scored.recency_days == 89
    assert scored.frequency == 6
    assert scored.monetary_ngn == 60_000
    assert scored.median_gap_days == 18
    assert scored.score == 74.0
    text = " ".join(scored.reasons)
    assert "89 days" in text
    assert "18 days" in text
    assert "4.9×" in text
    assert "Lapsed" in text
    assert "₦60,000" in text


def test_recent_buyer_is_active_and_low_priority():
    orders = _facts(["2026-08-01", "2026-08-21", "2026-09-10", "2026-09-30", "2026-10-20"], total=10_000)
    scored = score_orders(orders, date(2026, 10, 27))
    assert scored.segment == "active"
    assert scored.recency_days == 7
    assert scored.score < 20


def test_segment_boundaries():
    assert classify(0, None, None) == "new"
    assert classify(1, 20, None) == "new"
    assert classify(1, 80, None) == "at_risk"
    assert classify(1, 130, None) == "lost"
    assert classify(4, 10, 20) == "active"
    assert classify(4, 34, 21) == "at_risk"
    assert classify(6, 70, 18) == "lapsed"
    assert classify(2, 160, 10) == "lost"
    assert classify(3, 29, 30) == "active"
    assert classify(4, 60, 30) == "at_risk"


def test_cancelled_orders_are_not_passed_in_by_the_caller_contract():
    """The pure scorer trusts its input. Callers must pass completed orders only."""
    scored = score_orders([], date(2026, 10, 27))
    assert scored.segment == "new"
    assert scored.score == 0
    assert scored.reasons == ["No completed orders in the window."]


def test_local_context_mentions_payday_and_festivals():
    october = " ".join(local_context(date(2026, 10, 27))).lower()
    assert "detty december" in october
    assert "payday window open" not in october
    assert "eid" in " ".join(local_context(date(2026, 5, 27))).lower()
    assert "democracy day" in " ".join(local_context(date(2026, 6, 12))).lower()
    assert "new yam" in " ".join(local_context(date(2026, 8, 15))).lower()
    assert "christmas" in " ".join(local_context(date(2026, 12, 20))).lower()
    january = payday_calendar(date(2026, 1, 15))
    assert january["status"] == "upcoming"
    assert january["customer_line"]["en"] == "Payday is still coming, a good time to plan ahead."
    assert "don enter" not in january["customer_line"]["pcm"]
    october_line = payday_calendar(date(2026, 10, 27))["customer_line"]["pcm"]
    assert october_line == "Month-end dey this week, e good time to stock up."
    assert "payday window open" not in october_line
    assert "dey near" not in october_line
    assert "easter" in " ".join(local_context(date(2026, 4, 5))).lower()
    opened = payday_calendar(date(2026, 10, 27))
    assert opened["status"] == "open"
    assert opened["customer_line"]["en"] == "Month-end is this week, a good time to restock."
    assert "status is context only" in opened["instruction"]
    assert payday_calendar(date(2026, 1, 2))["customer_line"]["pcm"] == (
        "Salary don land for many people, e good time to stock up."
    )
    assert payday_calendar(date(2026, 10, 27))["today"] == "2026-10-27"
    assert payday_calendar(date(2026, 1, 2))["status"] == "passed"
    assert payday_calendar(date(2026, 1, 15))["status"] == "upcoming"
