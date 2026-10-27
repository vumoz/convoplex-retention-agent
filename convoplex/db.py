"""SQLite access. Every business row is synthetic and the schema rejects anything else."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SYNTHETIC_LABEL = (
    "SYNTHETIC DATA — fictional Nigerian retailers, customers, and orders "
    "generated for the Convoplex hackathon demo. Not real people or transactions."
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retailers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    area TEXT NOT NULL,
    category_focus TEXT NOT NULL,
    phone TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1 CHECK (synthetic = 1)
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL,
    area TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('en', 'pcm')),
    archetype TEXT NOT NULL DEFAULT 'manual',
    synthetic INTEGER NOT NULL DEFAULT 1 CHECK (synthetic = 1)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    price_ngn INTEGER NOT NULL CHECK (price_ngn > 0),
    stock_qty INTEGER NOT NULL CHECK (stock_qty >= 0),
    synthetic INTEGER NOT NULL DEFAULT 1 CHECK (synthetic = 1),
    UNIQUE (retailer_id, sku)
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    discount_percent INTEGER NOT NULL,
    list_price_ngn INTEGER NOT NULL,
    offer_price_ngn INTEGER NOT NULL,
    message TEXT,
    language TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('drafted', 'sent', 'accepted', 'declined')),
    created_at TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1 CHECK (synthetic = 1)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'cancelled')),
    total_ngn INTEGER NOT NULL CHECK (total_ngn >= 0),
    channel TEXT NOT NULL,
    offer_id INTEGER REFERENCES offers(id),
    synthetic INTEGER NOT NULL DEFAULT 1 CHECK (synthetic = 1)
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_price_ngn INTEGER NOT NULL CHECK (unit_price_ngn >= 0),
    line_total_ngn INTEGER NOT NULL CHECK (line_total_ngn >= 0)
);

CREATE INDEX IF NOT EXISTS idx_customers_retailer ON customers(retailer_id);
CREATE INDEX IF NOT EXISTS idx_products_retailer ON products(retailer_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_ordered_on ON orders(ordered_on);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_offers_customer ON offers(customer_id);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back if the body raises. Business errors return a dict instead."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["value"])


def scenario_as_of(conn: sqlite3.Connection):
    """Scenario date baked into the synthetic set, so scores do not drift with the wall clock."""
    from datetime import date

    raw = meta_get(conn, "scenario_as_of")
    if raw:
        return date.fromisoformat(raw)
    return date(2026, 10, 27)
