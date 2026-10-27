"""Build a reproducible synthetic shop database.

The scenario date is 27 Oct 2026, the opening day of the hackathon. Orders cover
27 Apr 2026 through 26 Oct 2026. Every row is labelled synthetic.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from convoplex.db import SYNTHETIC_LABEL, init_db, transaction
from convoplex.synthetic.catalog import (
    AREAS,
    CATALOGUES,
    FIRST_NAMES,
    PHONE_PREFIXES,
    RETAILERS,
    SURNAMES,
    adjusted_price,
)

SCENARIO_AS_OF = date(2026, 10, 27)
WINDOW_START = date(2026, 4, 27)
WINDOW_END = date(2026, 10, 26)
DEFAULT_SEED = 20261027
DEFAULT_CUSTOMERS = 500

# Weights sum to 50 so 500 customers divide evenly: 150, 40, 80, 140, 40, 50.
ARCHETYPE_WEIGHTS = (
    ("active", 15),
    ("payday", 4),
    ("at_risk", 8),
    ("lapsed", 14),
    ("lost", 4),
    ("new", 5),
)


def archetype_plan(n: int) -> list[str]:
    total = sum(weight for _, weight in ARCHETYPE_WEIGHTS)
    counts = [n * weight // total for _, weight in ARCHETYPE_WEIGHTS]
    remainder = n - sum(counts)
    for index in range(remainder):
        counts[index % len(counts)] += 1
    plan: list[str] = []
    for (name, _), count in zip(ARCHETYPE_WEIGHTS, counts):
        plan.extend([name] * count)
    return plan


def generate(
    path: Path | str,
    *,
    n_customers: int = DEFAULT_CUSTOMERS,
    seed: int = DEFAULT_SEED,
) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = init_db(path)
    rng = random.Random(seed)
    plan = archetype_plan(n_customers)
    rng.shuffle(plan)

    with transaction(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('dataset', 'synthetic')")
        conn.execute("INSERT INTO meta (key, value) VALUES ('label', ?)", (SYNTHETIC_LABEL,))
        conn.execute("INSERT INTO meta (key, value) VALUES ('seed', ?)", (str(seed),))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('scenario_as_of', ?)",
            (SCENARIO_AS_OF.isoformat(),),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('window_start', ?)",
            (WINDOW_START.isoformat(),),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('window_end', ?)",
            (WINDOW_END.isoformat(),),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('customer_count', ?)",
            (str(n_customers),),
        )

        retailer_ids: list[int] = []
        catalogues: dict[int, list[dict]] = {}
        cores: dict[int, list[dict]] = {}
        for slot, spec in enumerate(RETAILERS):
            cursor = conn.execute(
                """
                INSERT INTO retailers (name, city, area, category_focus, phone)
                VALUES (?, ?, ?, ?, ?)
                """,
                (spec["name"], spec["city"], spec["area"], spec["category_focus"], spec["phone"]),
            )
            retailer_id = int(cursor.lastrowid)
            retailer_ids.append(retailer_id)
            products = []
            for index, item in enumerate(CATALOGUES[spec["category_focus"]]):
                sku, name, category, unit, base, core = item
                price = adjusted_price(base, slot)
                if index == len(CATALOGUES[spec["category_focus"]]) - 1:
                    stock = slot % 3
                else:
                    stock = 20 + (slot * 5 + index * 3) % 30
                row = conn.execute(
                    """
                    INSERT INTO products (
                        retailer_id, sku, name, category, unit, price_ngn, stock_qty
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (retailer_id, sku, name, category, unit, price, stock),
                )
                product = {
                    "id": int(row.lastrowid),
                    "sku": sku,
                    "name": name,
                    "category": category,
                    "unit": unit,
                    "price_ngn": price,
                    "core": core,
                }
                products.append(product)
            catalogues[retailer_id] = products
            cores[retailer_id] = [product for product in products if product["core"]]

        used_names: set[str] = set()
        used_phones: set[str] = set()
        for index, archetype in enumerate(plan):
            retailer_id = retailer_ids[index % len(retailer_ids)]
            retailer = RETAILERS[index % len(RETAILERS)]
            full_name = _unique_name(rng, used_names)
            phone = _unique_phone(rng, used_phones)
            area = rng.choice(AREAS[retailer["city"]])
            language = "pcm" if rng.random() < 0.45 else "en"
            customer = conn.execute(
                """
                INSERT INTO customers (
                    retailer_id, full_name, phone, city, area, language, archetype
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (retailer_id, full_name, phone, retailer["city"], area, language, archetype),
            )
            customer_id = int(customer.lastrowid)
            favourite = rng.choice(cores[retailer_id])
            for ordered_on in _order_dates(rng, archetype):
                alt = rng.choice(catalogues[retailer_id])
                roll = rng.random()
                product = favourite if roll < 0.70 else alt
                extra = rng.choice(catalogues[retailer_id])
                include_extra = rng.random() < 0.35 and extra["sku"] != product["sku"]
                qty_roll = rng.randint(1, 3)
                items = [(product, _qty(product, qty_roll))]
                if include_extra:
                    extra_roll = rng.randint(1, 3)
                    items.append((extra, _qty(extra, extra_roll)))
                total = sum(item["price_ngn"] * qty for item, qty in items)
                order = conn.execute(
                    """
                    INSERT INTO orders (
                        retailer_id, customer_id, ordered_on, status, total_ngn, channel
                    ) VALUES (?, ?, ?, 'completed', ?, 'walk-in')
                    """,
                    (retailer_id, customer_id, ordered_on.isoformat(), total),
                )
                order_id = int(order.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO order_items (
                        order_id, product_id, qty, unit_price_ngn, line_total_ngn
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (order_id, item["id"], qty, item["price_ngn"], item["price_ngn"] * qty)
                        for item, qty in items
                    ],
                )
    conn.close()
    return path


def _qty(product: dict, roll: int) -> int:
    if product["category"] == "phones" and product["price_ngn"] >= 50_000:
        return 1
    if product["category"] == "ankara":
        return 1 if roll == 3 else roll
    return roll


def _unique_name(rng: random.Random, used: set[str]) -> str:
    for _ in range(1000):
        first = rng.choice(FIRST_NAMES)
        if rng.random() < 0.12:
            middle = rng.choice(FIRST_NAMES)
            if middle != first:
                first = f"{first} {middle}"
        surname = rng.choice(SURNAMES)
        name = f"{first} {surname}"
        if name not in used:
            used.add(name)
            return name
    raise RuntimeError("exhausted unique synthetic names")


def _unique_phone(rng: random.Random, used: set[str]) -> str:
    for _ in range(1000):
        prefix = rng.choice(PHONE_PREFIXES)
        rest = "".join(str(rng.randint(0, 9)) for _ in range(7))
        phone = f"+234{prefix}{rest}"
        if phone not in used:
            used.add(phone)
            return phone
    raise RuntimeError("exhausted unique synthetic phones")


def _order_dates(rng: random.Random, archetype: str) -> list[date]:
    if archetype == "payday":
        return _payday_dates()
    if archetype == "new":
        return [SCENARIO_AS_OF - timedelta(days=rng.randint(2, 25))]
    if archetype == "lost":
        recency = rng.randint(156, 170)
        last = SCENARIO_AS_OF - timedelta(days=recency)
        first = WINDOW_START + timedelta(days=rng.randint(0, 4))
        if first >= last:
            first = last - timedelta(days=10)
        return [first, last]
    if archetype == "lapsed":
        gap = rng.randint(16, 20)
        recency = rng.randint(52, 78)
        last = SCENARIO_AS_OF - timedelta(days=recency)
        first = WINDOW_START + timedelta(days=rng.randint(0, 6))
        return _spaced(rng, first, last, gap, gap)
    if archetype == "at_risk":
        gap = rng.randint(20, 22)
        recency = rng.randint(30, 36)
        last = SCENARIO_AS_OF - timedelta(days=recency)
        first = WINDOW_START + timedelta(days=rng.randint(0, 6))
        return _spaced(rng, first, last, gap, gap)
    # active
    first = WINDOW_START + timedelta(days=rng.randint(0, 12))
    last = WINDOW_END - timedelta(days=rng.randint(0, 9))
    return _spaced(rng, first, last, 14, 24)


def _payday_dates() -> list[date]:
    dates: list[date] = []
    year, month = WINDOW_START.year, WINDOW_START.month
    while True:
        day = date(year, month, 28)
        if WINDOW_START <= day <= WINDOW_END:
            dates.append(day)
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        if date(year, month, 1) > WINDOW_END:
            break
    return dates


def _spaced(rng: random.Random, first: date, last: date, gap_low: int, gap_high: int) -> list[date]:
    if last <= first:
        return [last]
    dates = [first]
    cursor = first
    while True:
        step = rng.randint(gap_low, gap_high)
        nxt = cursor + timedelta(days=step)
        if nxt >= last:
            break
        dates.append(nxt)
        cursor = nxt
    if dates[-1] != last:
        if (last - dates[-1]).days < max(1, gap_low // 2) and len(dates) > 1:
            dates[-1] = last
        else:
            dates.append(last)
    return dates
