"""Retailer-facing totals. Win-back rate is accepted offers divided by offers sent."""

from __future__ import annotations

import sqlite3

from convoplex.context import list_at_risk
from convoplex.db import scenario_as_of


def snapshot(conn: sqlite3.Connection) -> dict:
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
    return {
        "offers_sent": sent,
        "offers_accepted": accepted,
        "offers_declined": declined,
        "win_back_rate": (accepted / sent) if sent else 0.0,
        "naira_recovered": int(recovered["naira"] or 0),
        "at_risk_customers": len(at_risk),
        "currency": "NGN",
        "synthetic": True,
    }
