"""Shop tools backed by SQLite. Prices always come from the database, never from the model."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from convoplex.db import scenario_as_of, transaction

MIN_DISCOUNT = 5
MAX_DISCOUNT = 15
MAX_QTY = 20

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_stock",
            "description": "Check current shelf quantity for a product at a retailer. Prices are NGN.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {"type": "string", "description": "Product name or SKU"},
                    "retailer_id": {"type": "integer", "description": "Retailer id from the customer context"},
                },
                "required": ["product", "retailer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_price",
            "description": "Get the current price in Nigerian naira (NGN) for a product at a retailer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {"type": "string", "description": "Product name or SKU"},
                    "retailer_id": {"type": "integer"},
                },
                "required": ["product", "retailer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_offer",
            "description": (
                "Create a win-back offer. Discount must be between 5 and 15 percent. "
                "The offer price is calculated from the database price."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                    "product_id": {"type": "integer"},
                    "product": {"type": "string", "description": "Product name, if product_id is unknown"},
                    "discount_percent": {"type": "integer"},
                },
                "required": ["customer_id", "discount_percent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": (
                "Place an order for the customer. Pass offer_id to charge the offer price "
                "and mark the win-back accepted. Stock is decremented."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                    "product_id": {"type": "integer"},
                    "product": {"type": "string"},
                    "qty": {"type": "integer"},
                    "offer_id": {"type": "integer"},
                },
                "required": ["customer_id", "qty"],
            },
        },
    },
]


def _as_int(value, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{field} must be an integer")


def _rank(query: str, name: str, sku: str) -> int | None:
    needle = query.strip().lower()
    if not needle:
        return None
    label = name.lower()
    code = sku.lower()
    if needle == label or needle == code:
        return 0
    if label.startswith(needle) or code.startswith(needle):
        return 1
    if needle in label or needle in code:
        return 2
    tokens = [token for token in needle.split() if len(token) > 2]
    if tokens and all(token in label for token in tokens):
        return 3
    return None


def find_products(conn: sqlite3.Connection, retailer_id: int, query: str, limit: int = 5) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM products WHERE retailer_id = ? ORDER BY name",
        (retailer_id,),
    ).fetchall()
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        rank = _rank(query, row["name"], row["sku"])
        if rank is None:
            continue
        scored.append((rank, row))
    scored.sort(key=lambda item: (item[0], item[1]["name"]))
    return [row for _, row in scored[:limit]]


def _product_payload(row: sqlite3.Row) -> dict:
    stock = int(row["stock_qty"])
    return {
        "product_id": int(row["id"]),
        "sku": row["sku"],
        "name": row["name"],
        "category": row["category"],
        "unit": row["unit"],
        "price_ngn": int(row["price_ngn"]),
        "stock_qty": stock,
        "in_stock": stock > 0,
        "currency": "NGN",
    }


def check_stock(conn: sqlite3.Connection, product: str, retailer_id: int, **_extra) -> dict:
    retailer_id = _as_int(retailer_id, "retailer_id")
    matches = [_product_payload(row) for row in find_products(conn, retailer_id, product)]
    if not matches:
        return {"ok": False, "error": f"No product matching {product!r} at retailer {retailer_id}."}
    return {"ok": True, "retailer_id": retailer_id, "matches": matches}


def get_price(conn: sqlite3.Connection, product: str, retailer_id: int, **_extra) -> dict:
    retailer_id = _as_int(retailer_id, "retailer_id")
    matches = find_products(conn, retailer_id, product)
    if not matches:
        return {"ok": False, "error": f"No product matching {product!r} at retailer {retailer_id}."}
    best = _product_payload(matches[0])
    payload = {"ok": True, "retailer_id": retailer_id, **best}
    if len(matches) > 1 and _rank(product, matches[0]["name"], matches[0]["sku"]) != 0:
        payload["note"] = "Several products matched. The first match is returned."
        payload["matches"] = [_product_payload(row) for row in matches]
    return payload


def _customer(conn: sqlite3.Connection, customer_id: int):
    return conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def _resolve_product(conn, customer, product_id, product):
    if product_id is not None:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            return None, {"ok": False, "error": f"Unknown product_id {product_id}."}
        if int(row["retailer_id"]) != int(customer["retailer_id"]):
            return None, {"ok": False, "error": "That product belongs to a different retailer."}
        return row, None
    if not product:
        return None, {"ok": False, "error": "Pass product_id or product."}
    matches = find_products(conn, int(customer["retailer_id"]), product, limit=1)
    if not matches:
        return None, {"ok": False, "error": f"No product matching {product!r} for this customer's shop."}
    return matches[0], None


def create_offer(
    conn: sqlite3.Connection,
    customer_id: int,
    discount_percent: int,
    product_id: int | None = None,
    product: str | None = None,
    message: str | None = None,
    **_extra,
) -> dict:
    try:
        customer_id = _as_int(customer_id, "customer_id")
        discount_percent = _as_int(discount_percent, "discount_percent")
        if product_id is not None:
            product_id = _as_int(product_id, "product_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if discount_percent < MIN_DISCOUNT or discount_percent > MAX_DISCOUNT:
        return {
            "ok": False,
            "error": f"discount_percent must be between {MIN_DISCOUNT} and {MAX_DISCOUNT}.",
        }
    customer = _customer(conn, customer_id)
    if customer is None:
        return {"ok": False, "error": f"Unknown customer_id {customer_id}."}
    row, error = _resolve_product(conn, customer, product_id, product)
    if error:
        return error
    if int(row["stock_qty"]) <= 0:
        return {"ok": False, "error": f"{row['name']} is out of stock.", "product_name": row["name"]}
    list_price = int(row["price_ngn"])
    offer_price = list_price * (100 - discount_percent) // 100
    if offer_price <= 0:
        return {"ok": False, "error": "Offer price rounded to zero."}
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO offers (
                retailer_id, customer_id, product_id, discount_percent,
                list_price_ngn, offer_price_ngn, message, language, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'drafted', ?)
            """,
            (
                int(customer["retailer_id"]),
                customer_id,
                int(row["id"]),
                discount_percent,
                list_price,
                offer_price,
                message,
                customer["language"],
                created,
            ),
        )
        offer_id = int(cursor.lastrowid)
    return {
        "ok": True,
        "offer_id": offer_id,
        "customer_id": customer_id,
        "retailer_id": int(customer["retailer_id"]),
        "product_id": int(row["id"]),
        "product_name": row["name"],
        "unit": row["unit"],
        "discount_percent": discount_percent,
        "list_price_ngn": list_price,
        "offer_price_ngn": offer_price,
        "stock_qty": int(row["stock_qty"]),
        "currency": "NGN",
        "status": "drafted",
    }


def place_order(
    conn: sqlite3.Connection,
    customer_id: int,
    qty: int,
    product_id: int | None = None,
    product: str | None = None,
    offer_id: int | None = None,
    **_extra,
) -> dict:
    try:
        customer_id = _as_int(customer_id, "customer_id")
        qty = _as_int(qty, "qty")
        if product_id is not None:
            product_id = _as_int(product_id, "product_id")
        if offer_id is not None:
            offer_id = _as_int(offer_id, "offer_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if qty < 1 or qty > MAX_QTY:
        return {"ok": False, "error": f"qty must be between 1 and {MAX_QTY}."}
    customer = _customer(conn, customer_id)
    if customer is None:
        return {"ok": False, "error": f"Unknown customer_id {customer_id}."}

    with transaction(conn):
        offer = None
        if offer_id is not None:
            offer = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
            if offer is None:
                return {"ok": False, "error": f"Unknown offer_id {offer_id}."}
            if int(offer["customer_id"]) != customer_id:
                return {"ok": False, "error": "Offer belongs to a different customer."}
            if offer["status"] not in ("drafted", "sent"):
                return {"ok": False, "error": f"Offer is {offer['status']} and cannot be used."}
            product_id = int(offer["product_id"])
        row, error = _resolve_product(conn, customer, product_id, product)
        if error:
            return error
        stock = int(row["stock_qty"])
        if stock < qty:
            return {
                "ok": False,
                "error": f"Only {stock} {row['unit']} of {row['name']} in stock.",
                "stock_qty": stock,
            }
        if offer is not None:
            unit_price = int(offer["offer_price_ngn"])
        else:
            unit_price = int(row["price_ngn"])
        total = unit_price * qty
        ordered_on = scenario_as_of(conn).isoformat()
        cursor = conn.execute(
            """
            INSERT INTO orders (
                retailer_id, customer_id, ordered_on, status, total_ngn, channel, offer_id
            ) VALUES (?, ?, ?, 'completed', ?, 'whatsapp_sim', ?)
            """,
            (int(customer["retailer_id"]), customer_id, ordered_on, total, offer_id),
        )
        order_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO order_items (order_id, product_id, qty, unit_price_ngn, line_total_ngn)
            VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, int(row["id"]), qty, unit_price, total),
        )
        conn.execute(
            "UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
            (qty, int(row["id"])),
        )
        if offer is not None:
            conn.execute("UPDATE offers SET status = 'sent' WHERE id = ? AND status = 'drafted'", (offer_id,))
            conn.execute("UPDATE offers SET status = 'accepted' WHERE id = ?", (offer_id,))
    return {
        "ok": True,
        "order_id": order_id,
        "customer_id": customer_id,
        "product_id": int(row["id"]),
        "product_name": row["name"],
        "qty": qty,
        "unit": row["unit"],
        "unit_price_ngn": unit_price,
        "total_ngn": total,
        "offer_id": offer_id,
        "ordered_on": ordered_on,
        "channel": "whatsapp_sim",
        "currency": "NGN",
    }


def execute_tool(conn: sqlite3.Connection, name: str, arguments: dict) -> dict:
    functions = {
        "check_stock": check_stock,
        "get_price": get_price,
        "create_offer": create_offer,
        "place_order": place_order,
    }
    fn = functions.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "Tool arguments must be a JSON object."}
    try:
        return fn(conn, **arguments)
    except TypeError as exc:
        return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def mark_offer_sent(conn: sqlite3.Connection, offer_id: int, message: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            UPDATE offers
            SET message = ?, status = 'sent'
            WHERE id = ? AND status IN ('drafted', 'sent')
            """,
            (message, offer_id),
        )


def mark_offer_declined(conn: sqlite3.Connection, offer_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE offers SET status = 'declined' WHERE id = ? AND status = 'sent'",
            (offer_id,),
        )
