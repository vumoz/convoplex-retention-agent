"""Retailer-facing totals. Win-back rate is accepted offers divided by offers sent.

An offer that was declined and later accepted is counted only as accepted.
offers_declined is the number of offers that are still declined.
"""

from __future__ import annotations

import sqlite3

from convoplex.context import list_at_risk
from convoplex.db import scenario_as_of


def usage_totals(entries: list[dict] | None) -> dict:
    prompt = completion = reasoning = total = latency = calls = 0
    for entry in entries or []:
        if entry.get("kind") != "model_call":
            continue
        payload = entry.get("payload") or {}
        usage = payload.get("usage") or {}
        calls += 1
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        reasoning += int(usage.get("reasoning_tokens") or 0)
        total += int(usage.get("total_tokens") or 0)
        latency += int(payload.get("latency_ms") or 0)
    return {
        "model_calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "model_latency_ms": latency,
    }


def snapshot(conn: sqlite3.Connection, actions: list[dict] | None = None) -> dict:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status IN ('sent', 'accepted', 'declined') THEN 1 ELSE 0 END) AS sent,
          SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted,
          SUM(CASE WHEN status = 'declined' THEN 1 ELSE 0 END) AS declined
        FROM offers
        """
    ).fetchone()
    sent = int(row["sent"] or 0)
    accepted = int(row["accepted"] or 0)
    declined = int(row["declined"] or 0)
    recovered = conn.execute(
        """
        SELECT COALESCE(SUM(total_ngn), 0) AS naira
        FROM orders
        WHERE offer_id IS NOT NULL AND status = 'completed'
        """
    ).fetchone()
    at_risk = list_at_risk(conn, as_of=scenario_as_of(conn), limit=10_000)
    by_language = [
        {
            "language": row["language"],
            "customers": int(row["customers"] or 0),
            "offers_sent": int(row["offers_sent"] or 0),
        }
        for row in conn.execute(
            """
            SELECT c.language AS language,
                   COUNT(DISTINCT c.id) AS customers,
                   SUM(CASE WHEN o.status IN ('sent', 'accepted', 'declined') THEN 1 ELSE 0 END) AS offers_sent
            FROM customers c
            LEFT JOIN offers o ON o.customer_id = c.id
            GROUP BY c.language
            ORDER BY c.language
            """
        ).fetchall()
    ]
    return {
        "offers_sent": sent,
        "offers_accepted": accepted,
        "offers_declined": declined,
        "win_back_rate": (accepted / sent) if sent else 0.0,
        "naira_recovered": int(recovered["naira"] or 0),
        "at_risk_customers": len(at_risk),
        "currency": "NGN",
        "by_language": by_language,
        "synthetic": True,
        **usage_totals(actions),
    }
