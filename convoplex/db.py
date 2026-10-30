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
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'pcm', 'ig', 'yo', 'ha')),
    language_opted_in INTEGER NOT NULL DEFAULT 0,
    language_opted_in_at TEXT,
    language_channel TEXT,
    language_hint_sent INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS action_log (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_lock (
    id INTEGER PRIMARY KEY,
    held INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS usage_counters (
    bucket TEXT PRIMARY KEY,
    hits INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_cards (
    offer_id INTEGER PRIMARY KEY,
    image BLOB NOT NULL,
    media_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lexicon (
    id INTEGER PRIMARY KEY,
    phrase TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('en', 'pcm', 'ig', 'yo', 'ha')),
    meaning TEXT NOT NULL,
    example TEXT,
    source TEXT NOT NULL CHECK (source IN ('customer', 'seed')),
    confirmations INTEGER NOT NULL DEFAULT 1,
    distinct_customers INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT,
    removed_by TEXT,
    UNIQUE (phrase, language)
);

CREATE TABLE IF NOT EXISTS lexicon_seen (
    lexicon_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    PRIMARY KEY (lexicon_id, customer_id)
);

CREATE TABLE IF NOT EXISTS lexicon_daily (
    customer_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    hits INTEGER NOT NULL,
    PRIMARY KEY (customer_id, day)
);

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

# Tables whose INSERT should return the new id on Postgres (sqlite uses lastrowid).
ID_TABLES = (
    "retailers",
    "products",
    "customers",
    "offers",
    "orders",
    "order_items",
    "lexicon",
    "purchase_states",
)


def connect(path: Path | str) -> sqlite3.Connection:
    # FastAPI may open a request connection on one worker thread and close it on another.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    from convoplex.lexicon import seed_lexicon

    seed_lexicon(conn)
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


def postgres_schema() -> str:
    """Shop DDL translated for Postgres. INTEGER PRIMARY KEY becomes an identity column."""
    lines = []
    for line in SCHEMA.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("PRAGMA"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = text.replace(
        "INTEGER PRIMARY KEY",
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
    )
    return text.replace("BLOB", "BYTEA")


def sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        if not line.strip():
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return statements


RUNTIME_TABLES = """
CREATE TABLE IF NOT EXISTS action_log (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_lock (
    id INTEGER PRIMARY KEY,
    held INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS usage_counters (
    bucket TEXT PRIMARY KEY,
    hits INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_cards (
    offer_id INTEGER PRIMARY KEY,
    image BLOB NOT NULL,
    media_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_LANGUAGE_READY: set[str] = set()


def ensure_customer_language(conn, *, postgres: bool, key: str) -> None:
    """Add language opt-in columns and widen the check. Safe to run on live Neon."""
    if key in _LANGUAGE_READY:
        return
    if postgres:
        _ensure_postgres_language(conn)
    else:
        _ensure_sqlite_language(conn)
    conn.commit()
    _LANGUAGE_READY.add(key)


def _column_names_sqlite(conn) -> set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(customers)").fetchall()}


def _ensure_sqlite_language(conn) -> None:
    if "customers" not in {
        str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }:
        return
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'customers'").fetchone()
    ddl = str(row["sql"] or "") if row else ""
    if "'ig'" not in ddl:
        _rebuild_sqlite_customers(conn)
        return
    columns = _column_names_sqlite(conn)
    if "language_opted_in" not in columns:
        conn.execute("ALTER TABLE customers ADD COLUMN language_opted_in INTEGER NOT NULL DEFAULT 0")
    if "language_opted_in_at" not in columns:
        conn.execute("ALTER TABLE customers ADD COLUMN language_opted_in_at TEXT")
    if "language_channel" not in columns:
        conn.execute("ALTER TABLE customers ADD COLUMN language_channel TEXT")
    if "language_hint_sent" not in columns:
        conn.execute("ALTER TABLE customers ADD COLUMN language_hint_sent INTEGER NOT NULL DEFAULT 0")
    _backfill_language_opt_in(conn)


def _rebuild_sqlite_customers(conn) -> None:
    """SQLite cannot widen a CHECK constraint in place."""
    conn.execute("PRAGMA foreign_keys = OFF")
    columns = _column_names_sqlite(conn)
    conn.execute(
        """
        CREATE TABLE customers_new (
            id INTEGER PRIMARY KEY,
            retailer_id INTEGER NOT NULL REFERENCES retailers(id),
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            city TEXT NOT NULL,
            area TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'pcm', 'ig', 'yo', 'ha')),
            language_opted_in INTEGER NOT NULL DEFAULT 0,
            language_opted_in_at TEXT,
            language_channel TEXT,
            language_hint_sent INTEGER NOT NULL DEFAULT 0,
            archetype TEXT NOT NULL DEFAULT 'manual',
            synthetic INTEGER NOT NULL DEFAULT 1 CHECK (synthetic = 1)
        )
        """
    )
    opted = "language_opted_in" if "language_opted_in" in columns else "0"
    opted_at = "language_opted_in_at" if "language_opted_in_at" in columns else "NULL"
    channel = "language_channel" if "language_channel" in columns else "NULL"
    hint = "language_hint_sent" if "language_hint_sent" in columns else "0"
    conn.execute(
        f"""
        INSERT INTO customers_new (
            id, retailer_id, full_name, phone, city, area, language,
            language_opted_in, language_opted_in_at, language_channel, language_hint_sent,
            archetype, synthetic
        )
        SELECT id, retailer_id, full_name, phone, city, area, language,
               {opted}, {opted_at}, {channel}, {hint}, archetype, synthetic
        FROM customers
        """
    )
    conn.execute("DROP TABLE customers")
    conn.execute("ALTER TABLE customers_new RENAME TO customers")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_retailer ON customers(retailer_id)")
    conn.execute("PRAGMA foreign_keys = ON")
    _backfill_language_opt_in(conn)


def _ensure_postgres_language(conn) -> None:
    present = {
        str(row["column_name"])
        for row in conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'customers'
            """
        ).fetchall()
    }
    if not present:
        return
    if "language_opted_in" not in present:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS language_opted_in INTEGER NOT NULL DEFAULT 0"
        )
    if "language_opted_in_at" not in present:
        conn.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS language_opted_in_at TEXT")
    if "language_channel" not in present:
        conn.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS language_channel TEXT")
    if "language_hint_sent" not in present:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS language_hint_sent INTEGER NOT NULL DEFAULT 0"
        )
    row = conn.execute(
        """
        SELECT pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conname = 'customers_language_check'
        """
    ).fetchone()
    definition = str(row["def"]) if row else ""
    if "ig" not in definition:
        conn.execute("ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_language_check")
        conn.execute(
            "ALTER TABLE customers ADD CONSTRAINT customers_language_check "
            "CHECK (language IN ('en', 'pcm', 'ig', 'yo', 'ha'))"
        )
    _backfill_language_opt_in(conn)


def _backfill_language_opt_in(conn) -> None:
    """A non-English language already on the row is an explicit opt-in."""
    conn.execute(
        """
        UPDATE customers
        SET language_opted_in = 1,
            language_channel = COALESCE(language_channel, 'seed')
        WHERE language <> 'en' AND language_opted_in = 0
        """
    )


def open_db(settings):
    """SQLite file for local and tests. Postgres when DATABASE_URL is set.

    A fresh connection is opened per call so a serverless instance never shares
    one Neon connection across concurrent requests.
    """
    if getattr(settings, "database_url", None):
        from convoplex.pg import connect_postgres

        conn = connect_postgres(settings.database_url, postgres_schema())
        ensure_customer_language(conn, postgres=True, key=settings.database_url)
        from convoplex.lexicon import ensure_lexicon

        ensure_lexicon(conn)
        from convoplex.purchase import ensure_purchase_states

        ensure_purchase_states(conn)
        return conn
    conn = connect(settings.db_path)
    conn.executescript(RUNTIME_TABLES)
    ensure_customer_language(conn, postgres=False, key=str(settings.db_path))
    from convoplex.lexicon import ensure_lexicon

    ensure_lexicon(conn)
    from convoplex.purchase import ensure_purchase_states

    ensure_purchase_states(conn)
    return conn


def ping_db(settings) -> None:
    """Raise when DATABASE_URL is set and the database does not answer."""
    conn = open_db(settings)
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        if row is None or int(row["ok"]) != 1:
            raise RuntimeError("database ping failed")
    finally:
        conn.close()


def scenario_as_of(conn: sqlite3.Connection):
    """Scenario date baked into the synthetic set, so scores do not drift with the wall clock."""
    from datetime import date

    raw = meta_get(conn, "scenario_as_of")
    if raw:
        return date.fromisoformat(raw)
    return date(2026, 10, 27)
