import sqlite3
from collections import Counter
from datetime import date

import pytest

from convoplex.context import score_directory
from convoplex.db import SYNTHETIC_LABEL, connect
from convoplex.synthetic.generate import SCENARIO_AS_OF, WINDOW_END, WINDOW_START, generate


def test_full_synthetic_book(tmp_path):
    path = generate(tmp_path / "book.sqlite")
    conn = connect(path)
    label = conn.execute("SELECT value FROM meta WHERE key = 'label'").fetchone()["value"]
    assert label == SYNTHETIC_LABEL
    assert conn.execute("SELECT value FROM meta WHERE key = 'scenario_as_of'").fetchone()["value"] == "2026-10-27"

    customers = conn.execute("SELECT * FROM customers").fetchall()
    assert len(customers) == 500
    assert {row["city"] for row in customers} == {"Lagos", "Abuja", "Port Harcourt"}
    assert all(row["synthetic"] == 1 for row in customers)
    assert all(row["language"] in {"en", "pcm"} for row in customers)
    names = [row["full_name"] for row in customers]
    assert len(set(names)) == 500
    assert any(name.endswith("Okonkwo") or "Okonkwo" in name for name in names)
    assert any(name.endswith("Balogun") for name in names)
    for row in customers:
        assert row["phone"].startswith("+234")
        assert len(row["phone"]) == 14

    retailers = conn.execute("SELECT * FROM retailers").fetchall()
    assert len(retailers) == 6
    assert {row["city"] for row in retailers} == {"Lagos", "Abuja", "Port Harcourt"}

    products = conn.execute("SELECT * FROM products").fetchall()
    assert products
    assert all(row["price_ngn"] > 0 and row["synthetic"] == 1 for row in products)
    assert {row["category"] for row in products} <= {"provisions", "ankara", "phones", "accessories"}
    assert any(row["stock_qty"] <= 2 for row in products)
    assert any(row["stock_qty"] >= 20 for row in products)

    rice = [row["price_ngn"] for row in products if row["sku"] == "RICE-50"]
    assert len(rice) == 2
    assert len(set(rice)) == 2

    orders = conn.execute("SELECT ordered_on, total_ngn, synthetic FROM orders").fetchall()
    assert len(orders) > 1500
    dates = [date.fromisoformat(row["ordered_on"]) for row in orders]
    assert min(dates) >= WINDOW_START
    assert max(dates) <= WINDOW_END
    assert (max(dates) - min(dates)).days >= 150
    assert all(row["total_ngn"] > 0 and row["synthetic"] == 1 for row in orders)

    segments = Counter(row["segment"] for row in score_directory(conn, SCENARIO_AS_OF))
    assert sum(segments.values()) == 500
    assert segments["active"] >= 140
    assert segments["at_risk"] >= 60
    assert segments["lapsed"] >= 100
    assert segments["lost"] >= 30
    assert segments["new"] >= 40

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO customers (retailer_id, full_name, phone, city, area, language, synthetic)
            VALUES (1, 'Not Real', '+2348030001999', 'Lagos', 'Ikeja', 'en', 0)
            """
        )
    conn.close()


def test_same_seed_is_reproducible(tmp_path):
    first = generate(tmp_path / "a.sqlite", n_customers=80, seed=7)
    second = generate(tmp_path / "b.sqlite", n_customers=80, seed=7)
    assert _fingerprint(first) == _fingerprint(second)


def _fingerprint(path):
    conn = connect(path)
    customers = [
        (row["full_name"], row["phone"], row["language"], row["archetype"])
        for row in conn.execute(
            "SELECT full_name, phone, language, archetype FROM customers ORDER BY id"
        )
    ]
    total = conn.execute("SELECT COALESCE(SUM(total_ngn), 0) AS s FROM orders").fetchone()["s"]
    conn.close()
    return customers, total


