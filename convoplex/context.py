"""Customer profiles and Nigerian retail calendar notes for the agent."""

from __future__ import annotations

import sqlite3
from datetime import date

from convoplex.db import scenario_as_of
from convoplex.scoring import LapseScore, OrderFact, score_orders

AT_RISK_SEGMENTS = ("at_risk", "lapsed")


def payday_calendar(as_of: date) -> dict:
    """Payday as context, plus one natural sentence per language.

    status is for the agent only. Customer copy comes from customer_line.
    The window is the 25th through the 3rd. On the 27th salary has not
    necessarily landed, so the sentence does not say that it has.
    """
    today = as_of.isoformat()
    if as_of.day <= 3:
        status = "passed"
        pcm = "Salary don land for many people, e good time to stock up."
        en = "Salary has landed for many people, a good time to restock."
    elif as_of.day >= 25:
        status = "open"
        pcm = "Month-end dey this week, e good time to stock up."
        en = "Month-end is this week, a good time to restock."
    else:
        status = "upcoming"
        pcm = "Payday still dey come, e good time to plan am."
        en = "Payday is still coming, a good time to plan ahead."
    return {
        "today": today,
        "status": status,
        "customer_line": {"pcm": pcm, "en": en},
        "instruction": (
            f"Today is {today}. status is context only. Never write the status value in the message. "
            "The first win-back uses exactly one seasonal hook. Write payday.customer_line for the "
            "customer's language as one natural sentence, and do not also write a local_context note. "
            "Do not mention payday in a decline."
        ),
    }


def local_context(as_of: date) -> list[str]:
    """Payday and festive notes a Lagos shop would actually use in a message."""
    notes: list[str] = []
    if as_of.month == 4 and as_of.day <= 12:
        notes.append("Easter season: family visits lift provisions and outfits.")
    if as_of.month == 5 and as_of.day >= 20:
        notes.append(
            "Late May covers Children's Day (27 May) and the Eid al-Adha period in 2026. Gifts, outfits, and provisions move."
        )
    if as_of.month == 6 and as_of.day <= 14:
        notes.append("Democracy Day (12 June) long weekend.")
    if as_of.month == 8:
        notes.append(
            "August is New Yam festival season across much of the South-East. Provisions and ceremonial wrappers move."
        )
    if as_of.month == 10 and as_of.day <= 7:
        notes.append("Independence Day (1 October) week: green-white outfits and party provisions move.")
    elif as_of.month == 10 and as_of.day >= 20:
        notes.append(
            "End-of-year festivities are approaching (Detty December, Christmas sewing, family provisions). Shops start reminding regulars now."
        )
    if as_of.month == 11 and as_of.day >= 15:
        notes.append("Detty December is close. Tailors and provision shops get busy from mid-November.")
    if as_of.month == 12:
        notes.append(
            "Detty December and Christmas: Ankara and lace for aso-ebi, plus rice, oil, and drinks for family visits."
        )
    return notes


def _orders_for(conn: sqlite3.Connection) -> dict[int, list[OrderFact]]:
    grouped: dict[int, list[OrderFact]] = {}
    rows = conn.execute(
        """
        SELECT customer_id, ordered_on, total_ngn
        FROM orders
        WHERE status = 'completed'
        ORDER BY ordered_on, id
        """
    ).fetchall()
    for row in rows:
        grouped.setdefault(int(row["customer_id"]), []).append(
            OrderFact(date.fromisoformat(row["ordered_on"]), int(row["total_ngn"]))
        )
    return grouped


def _favourites(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        """
        SELECT o.customer_id, p.id AS product_id, p.name, p.sku, p.unit,
               p.price_ngn, p.stock_qty, SUM(oi.qty) AS qty
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.status = 'completed'
        GROUP BY o.customer_id, p.id
        ORDER BY qty DESC, p.name
        """
    ).fetchall()
    best: dict[int, dict] = {}
    for row in rows:
        customer_id = int(row["customer_id"])
        if customer_id in best:
            continue
        best[customer_id] = {
            "product_id": int(row["product_id"]),
            "name": row["name"],
            "sku": row["sku"],
            "unit": row["unit"],
            "price_ngn": int(row["price_ngn"]),
            "stock_qty": int(row["stock_qty"]),
            "qty": int(row["qty"]),
        }
    return best


def _payday_flags(conn: sqlite3.Connection) -> dict[int, bool]:
    rows = conn.execute(
        """
        SELECT customer_id, ordered_on
        FROM orders
        WHERE status = 'completed'
        """
    ).fetchall()
    grouped: dict[int, list[date]] = {}
    for row in rows:
        grouped.setdefault(int(row["customer_id"]), []).append(date.fromisoformat(row["ordered_on"]))
    flags: dict[int, bool] = {}
    for customer_id, dates in grouped.items():
        if len(dates) < 3:
            flags[customer_id] = False
            continue
        hits = sum(1 for day in dates if day.day >= 25 or day.day <= 3)
        flags[customer_id] = hits / len(dates) >= 0.6
    return flags


def _public_reasons(score: LapseScore, favourite: dict | None, payday: bool) -> list[str]:
    reasons = list(score.reasons)
    if favourite:
        unit = favourite["unit"]
        qty = favourite["qty"]
        noun = unit if qty == 1 else f"{unit}s"
        reasons.append(f"Most often buys {favourite['name']} ({qty} {noun} across the window).")
    if payday:
        reasons.append(
            "Often shops around month-end payday (more than half of orders fall on the 25th–3rd)."
        )
    return reasons


def _customer_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id, c.full_name, c.phone, c.city, c.area, c.language, c.archetype,
               c.retailer_id, r.name AS retailer_name, r.city AS retailer_city,
               r.area AS retailer_area, r.category_focus
        FROM customers c
        JOIN retailers r ON r.id = c.retailer_id
        ORDER BY c.id
        """
    ).fetchall()


def score_directory(conn: sqlite3.Connection, as_of: date | None = None) -> list[dict]:
    as_of = as_of or scenario_as_of(conn)
    orders = _orders_for(conn)
    favourites = _favourites(conn)
    payday = _payday_flags(conn)
    directory: list[dict] = []
    for row in _customer_rows(conn):
        customer_id = int(row["id"])
        scored = score_orders(orders.get(customer_id, []), as_of)
        favourite = favourites.get(customer_id)
        record = _base_record(row, scored, favourite, payday.get(customer_id, False), as_of)
        directory.append(record)
    return directory


def _base_record(row, scored: LapseScore, favourite: dict | None, payday: bool, as_of: date) -> dict:
    reasons = _public_reasons(scored, favourite, payday)
    suggested_qty = 1
    if favourite and favourite["qty"] and scored.frequency:
        suggested_qty = max(1, min(3, round(favourite["qty"] / scored.frequency)))
    return {
        "id": int(row["id"]),
        "full_name": row["full_name"],
        "phone": row["phone"],
        "city": row["city"],
        "area": row["area"],
        "language": row["language"],
        "synthetic_archetype": row["archetype"],
        "retailer_id": int(row["retailer_id"]),
        "retailer_name": row["retailer_name"],
        "retailer_city": row["retailer_city"],
        "retailer_area": row["retailer_area"],
        "category_focus": row["category_focus"],
        "segment": scored.segment,
        "score": scored.score,
        "recency_days": scored.recency_days,
        "frequency": scored.frequency,
        "monetary_ngn": scored.monetary_ngn,
        "median_gap_days": scored.median_gap_days,
        "reasons": reasons,
        "favourite_product": favourite,
        "payday_shopper": payday,
        "suggested_qty": suggested_qty,
        "as_of": as_of.isoformat(),
        "synthetic": True,
    }


def list_at_risk(
    conn: sqlite3.Connection,
    *,
    as_of: date | None = None,
    retailer_id: int | None = None,
    limit: int = 50,
    include_lost: bool = False,
) -> list[dict]:
    segments = set(AT_RISK_SEGMENTS)
    if include_lost:
        segments.add("lost")
    rows = [
        row
        for row in score_directory(conn, as_of)
        if row["segment"] in segments and (retailer_id is None or row["retailer_id"] == retailer_id)
    ]
    rows.sort(key=lambda row: (-row["score"], row["id"]))
    return rows[:limit]


def get_customer(conn: sqlite3.Connection, customer_id: int, as_of: date | None = None) -> dict | None:
    for row in score_directory(conn, as_of):
        if row["id"] == customer_id:
            return row
    return None


def open_offer(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT o.id, o.product_id, o.discount_percent, o.list_price_ngn, o.offer_price_ngn,
               o.status, o.language, p.name AS product_name, p.stock_qty, p.unit
        FROM offers o
        JOIN products p ON p.id = o.product_id
        WHERE o.customer_id = ?
          AND o.status IN ('drafted', 'sent', 'declined')
          AND p.stock_qty > 0
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "offer_id": int(row["id"]),
        "product_id": int(row["product_id"]),
        "product_name": row["product_name"],
        "discount_percent": int(row["discount_percent"]),
        "list_price_ngn": int(row["list_price_ngn"]),
        "offer_price_ngn": int(row["offer_price_ngn"]),
        "status": row["status"],
        "language": row["language"],
        "stock_qty": int(row["stock_qty"]),
        "unit": row["unit"],
        "reusable": True,
    }


def agent_context(
    conn: sqlite3.Connection,
    customer_id: int,
    task: str,
    language: str | None = None,
    prior_tool_results: dict | None = None,
) -> dict | None:
    profile = get_customer(conn, customer_id)
    if profile is None:
        return None
    if language is not None and language not in {"en", "pcm"}:
        raise ValueError("language must be 'en' or 'pcm'")
    favourite = profile["favourite_product"]
    as_of = date.fromisoformat(profile["as_of"])
    spoken = language or profile["language"]
    context = {
        "task": task,
        "customer_id": profile["id"],
        "full_name": profile["full_name"],
        "language": spoken,
        "city": profile["city"],
        "area": profile["area"],
        "retailer_id": profile["retailer_id"],
        "retailer_name": profile["retailer_name"],
        "retailer_area": profile["retailer_area"],
        "category_focus": profile["category_focus"],
        "segment": profile["segment"],
        "score": profile["score"],
        "recency_days": profile["recency_days"],
        "frequency": profile["frequency"],
        "monetary_ngn": profile["monetary_ngn"],
        "reasons": profile["reasons"],
        "suggested_qty": profile["suggested_qty"],
        "favourite_product": favourite,
        "local_context": local_context(as_of),
        "today": as_of.isoformat(),
        "payday": payday_calendar(as_of),
        "as_of": profile["as_of"],
        "currency": "NGN",
    }
    if task == "reply":
        context["open_offer"] = open_offer(conn, customer_id)
        context["prior_tool_results"] = prior_tool_results or {}
        context["thread"] = (
            "Same thread as the win-back. Do not greet. Do not introduce the shop again. "
            "Do not call check_stock or get_price when prior_tool_results already has them. "
            "A declined open_offer is still the price: quote offer_price_ngn and pass its offer_id."
        )
    return context
