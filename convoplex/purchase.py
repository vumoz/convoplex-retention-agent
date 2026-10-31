"""Purchase states for one customer, one offer, one quantity.

States are offer_presented, clarification_required, confirmed, declined, and
expired. place_order will not write an order unless a confirmed row matches
the product, quantity, offer, and customer and has not expired.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from convoplex.db import transaction

STATES = (
    "offer_presented",
    "clarification_required",
    "confirmed",
    "declined",
    "expired",
)
OPEN_STATES = ("offer_presented", "clarification_required", "confirmed")
CONFIRM_HOURS = 24

ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS purchase_states (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    retailer_id INTEGER NOT NULL,
    offer_id INTEGER,
    product_id INTEGER,
    qty INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'offer_presented', 'clarification_required', 'confirmed', 'declined', 'expired'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchase_customer ON purchase_states(customer_id, state);
"""


def ensure_purchase_states(conn) -> None:
    for statement in ENSURE_SQL.strip().split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _expires() -> str:
    moment = datetime.now(timezone.utc) + timedelta(hours=CONFIRM_HOURS)
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def requested_qty(user_text: str | None, profile: dict, offer: dict) -> int:
    """The quantity the fallback will send to place_order. Confirmation uses the same number."""
    stock = int(offer.get("stock_qty") or 1)
    numbers = [int(number) for number in re.findall(r"\d+", user_text or "")]
    # A bare yes is one bag. suggested_qty is a restock hint, not consent for 2 or 3.
    requested = numbers[-1] if numbers else 1
    del profile
    return max(1, min(requested, stock if stock > 0 else 1))


def expire_elapsed(conn, customer_id: int) -> None:
    now = _now()
    conn.execute(
        """
        UPDATE purchase_states
        SET state = 'expired', updated_at = ?
        WHERE customer_id = ?
          AND state = 'confirmed'
          AND expires_at IS NOT NULL
          AND expires_at < ?
        """,
        (now, customer_id, now),
    )


def expire_other_offers(conn, customer_id: int, keep_offer_id: int | None) -> None:
    """A new or replaced offer retires older confirmations so they cannot execute."""
    now = _now()
    if keep_offer_id is None:
        conn.execute(
            """
            UPDATE purchase_states
            SET state = 'expired', updated_at = ?
            WHERE customer_id = ? AND state IN ('offer_presented', 'clarification_required', 'confirmed')
            """,
            (now, customer_id),
        )
        return
    conn.execute(
        """
        UPDATE purchase_states
        SET state = 'expired', updated_at = ?
        WHERE customer_id = ?
          AND state IN ('offer_presented', 'clarification_required', 'confirmed')
          AND (offer_id IS NULL OR offer_id != ?)
        """,
        (now, customer_id, int(keep_offer_id)),
    )


def _insert(
    conn,
    *,
    customer_id: int,
    retailer_id: int,
    offer_id: int | None,
    product_id: int | None,
    qty: int,
    state: str,
    expires_at: str | None,
) -> int:
    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO purchase_states (
            customer_id, retailer_id, offer_id, product_id, qty, state,
            created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (customer_id, retailer_id, offer_id, product_id, qty, state, now, now, expires_at),
    )
    return int(cursor.lastrowid)


def note_presented(conn, offer: dict, *, qty: int = 1) -> None:
    customer_id = int(offer["customer_id"])
    offer_id = int(offer["offer_id"])
    with transaction(conn):
        expire_other_offers(conn, customer_id, offer_id)
        _insert(
            conn,
            customer_id=customer_id,
            retailer_id=int(offer["retailer_id"]),
            offer_id=offer_id,
            product_id=int(offer["product_id"]),
            qty=qty,
            state="offer_presented",
            expires_at=None,
        )


def _profile_customer(profile: dict) -> int:
    return int(profile.get("id") or profile["customer_id"])


def note_clarification(conn, profile: dict, offer: dict) -> bool:
    """Record clarification_required. Returns True when this offer was already waiting on a yes."""
    customer_id = _profile_customer(profile)
    offer_id = int(offer["offer_id"])
    with transaction(conn):
        existing = conn.execute(
            """
            SELECT state FROM purchase_states
            WHERE customer_id = ? AND offer_id = ? AND state = 'clarification_required'
            ORDER BY id DESC LIMIT 1
            """,
            (customer_id, offer_id),
        ).fetchone()
        already = existing is not None
        if not already:
            _insert(
                conn,
                customer_id=customer_id,
                retailer_id=int(profile["retailer_id"]),
                offer_id=offer_id,
                product_id=int(offer["product_id"]),
                qty=requested_qty(None, profile, offer),
                state="clarification_required",
                expires_at=None,
            )
    return already


def note_declined(conn, profile: dict, offer: dict) -> None:
    customer_id = _profile_customer(profile)
    offer_id = int(offer["offer_id"])
    with transaction(conn):
        now = _now()
        conn.execute(
            """
            UPDATE purchase_states
            SET state = 'expired', updated_at = ?
            WHERE customer_id = ? AND offer_id = ?
              AND state IN ('offer_presented', 'clarification_required', 'confirmed')
            """,
            (now, customer_id, offer_id),
        )
        _insert(
            conn,
            customer_id=customer_id,
            retailer_id=int(profile["retailer_id"]),
            offer_id=offer_id,
            product_id=int(offer["product_id"]),
            qty=requested_qty(None, profile, offer),
            state="declined",
            expires_at=None,
        )


def confirm_purchase(
    conn,
    *,
    customer_id: int,
    retailer_id: int,
    product_id: int,
    qty: int,
    offer_id: int | None,
) -> dict:
    """Trusted code only. The model has no tool that writes this row."""
    with transaction(conn):
        expire_elapsed(conn, customer_id)
        state_id = _insert(
            conn,
            customer_id=int(customer_id),
            retailer_id=int(retailer_id),
            offer_id=int(offer_id) if offer_id is not None else None,
            product_id=int(product_id),
            qty=int(qty),
            state="confirmed",
            expires_at=_expires(),
        )
    return {"ok": True, "id": state_id, "state": "confirmed"}


def find_confirmation(
    conn,
    *,
    customer_id: int,
    product_id: int,
    qty: int,
    offer_id: int | None,
) -> sqlite3.Row | None:
    expire_elapsed(conn, int(customer_id))
    if offer_id is None:
        offer_clause = "offer_id IS NULL"
        params: tuple = (int(customer_id), int(product_id), int(qty))
    else:
        offer_clause = "offer_id = ?"
        params = (int(customer_id), int(product_id), int(qty), int(offer_id))
    return conn.execute(
        f"""
        SELECT * FROM purchase_states
        WHERE customer_id = ? AND product_id = ? AND qty = ? AND state = 'confirmed'
          AND {offer_clause}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def consume_confirmation(conn, state_id: int) -> None:
    conn.execute(
        "UPDATE purchase_states SET state = 'expired', updated_at = ? WHERE id = ? AND state = 'confirmed'",
        (_now(), int(state_id)),
    )
