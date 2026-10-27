"""Shared shop fixture. Forces mock mode so a stray key cannot hit the network."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from convoplex.db import connect, init_db

AS_OF = date(2026, 10, 27)


@pytest.fixture(autouse=True)
def _force_mock(monkeypatch):
    monkeypatch.setenv("CONVOPLEX_LLM", "mock")
    monkeypatch.delenv("META_API_KEY", raising=False)


@pytest.fixture
def shop(tmp_path: Path):
    db_path = tmp_path / "shop.sqlite"
    conn = init_db(db_path)
    conn.execute("INSERT INTO meta (key, value) VALUES ('scenario_as_of', ?)", (AS_OF.isoformat(),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('dataset', 'synthetic')")
    conn.execute(
        """
        INSERT INTO retailers (name, city, area, category_focus, phone)
        VALUES ('Mama Nkechi Provisions', 'Lagos', 'Ikeja', 'provisions', '+2348000001001')
        """
    )
    conn.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (1, 'RICE-50', 'Mama Gold Rice 50kg', 'provisions', 'bag', 78000, 20)
        """
    )
    conn.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (1, 'SPAG', 'Golden Penny Spaghetti 500g', 'provisions', 'pack', 1100, 0)
        """
    )
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language, archetype)
        VALUES (1, 'Chinedu Okonkwo', '+2348030001101', 'Lagos', 'Ikeja', 'pcm', 'lapsed')
        """
    )
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language, archetype)
        VALUES (1, 'Adaeze Balogun', '+2348030001102', 'Lagos', 'Yaba', 'en', 'lapsed')
        """
    )
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language, archetype)
        VALUES (1, 'Tunde Adeyemi', '+2348030001103', 'Lagos', 'Maryland', 'en', 'active')
        """
    )
    lapsed_days = (
        "2026-05-01",
        "2026-05-19",
        "2026-06-06",
        "2026-06-24",
        "2026-07-12",
        "2026-07-30",
    )
    for customer_id in (1, 2):
        for ordered_on in lapsed_days:
            _order(conn, customer_id, ordered_on, 78000)
        conn.execute(
            """
            INSERT INTO orders (retailer_id, customer_id, ordered_on, status, total_ngn, channel)
            VALUES (1, ?, '2026-06-01', 'cancelled', 78000, 'walk-in')
            """,
            (customer_id,),
        )
    for ordered_on in ("2026-08-01", "2026-08-21", "2026-09-10", "2026-09-30", "2026-10-20"):
        _order(conn, 3, ordered_on, 15000)
    conn.commit()
    yield conn
    conn.close()


def _order(conn, customer_id: int, ordered_on: str, total: int) -> None:
    cursor = conn.execute(
        """
        INSERT INTO orders (retailer_id, customer_id, ordered_on, status, total_ngn, channel)
        VALUES (1, ?, ?, 'completed', ?, 'walk-in')
        """,
        (customer_id, ordered_on, total),
    )
    conn.execute(
        """
        INSERT INTO order_items (order_id, product_id, qty, unit_price_ngn, line_total_ngn)
        VALUES (?, 1, 1, ?, ?)
        """,
        (int(cursor.lastrowid), total, total),
    )


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "actions.jsonl"
