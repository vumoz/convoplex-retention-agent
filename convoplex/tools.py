"""Shop tools backed by SQLite. Prices always come from the database, never from the model."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from convoplex.db import scenario_as_of, transaction
from convoplex.execution import ExecutionContext, audit_payload, scope_arguments
from convoplex.languages import CONFIRM, clean_language, language_label, save_language
from convoplex.lexicon import learn_phrase as store_phrase
from convoplex.purchase import (
    consume_confirmation,
    expire_other_offers,
    find_confirmation,
    note_presented,
)

MIN_DISCOUNT = 5
MAX_DISCOUNT = 15
MAX_QTY = 10

_DELIVERY_FEE_NGN = {
    "provisions": 2500,
    "ankara": 2000,
    "phones": 3500,
}

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
                "and mark the win-back accepted. A declined offer is still valid: if you omit "
                "offer_id, the customer's latest drafted, sent, or declined offer for this product "
                "is attached and its price is charged. Quantity is capped at shelf stock and at "
                f"{MAX_QTY} units per order. Stock is decremented by the capped quantity."
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
    {
        "type": "function",
        "function": {
            "name": "get_delivery_options",
            "description": (
                "Synthetic delivery options for this shop. Pickup is same day at the shop with no fee. "
                "Same-city delivery is a 1 to 2 day window, not a named weekday. "
                "Delivery outside the shop's city is unavailable. "
                "Never promise a date this tool did not return."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "retailer_id": {"type": "integer"},
                    "customer_id": {"type": "integer"},
                },
                "required": ["retailer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_language",
            "description": (
                "Switch the customer's saved language. Call this before any other tool when they ask "
                "to speak English, Broken English, Igbo, Yoruba, or Hausa, in any wording and in any "
                "language, including a misheard name. After it returns, write only in the new language "
                "and start with the confirmation. Do not keep replying in the previous language."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                    "language": {
                        "type": "string",
                        "enum": ["en", "pcm", "ig", "yo", "ha"],
                        "description": "en English, pcm Broken English, ig Igbo, yo Yoruba, ha Hausa",
                    },
                },
                "required": ["customer_id", "language"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_phrase",
            "description": (
                "Ask about one unknown word only when it blocks you from knowing what the "
                "customer wants. If the message is already clear, do not call this: infer and "
                "carry on. At most one call per turn and two in the conversation. "
                "Do not decline or cancel an offer. If the tool says already_asked, do not ask again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "language": {"type": "string", "description": "en, pcm, ig, yo, or ha"},
                    "customer_id": {"type": "integer"},
                },
                "required": ["phrase"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_offer_card",
            "description": (
                "Send the promo card image for the customer's open offer. Call this when they "
                "ask to see it: picture, pic, image, photo, foto, flier, show me, or make I see. "
                "Never say you cannot send an image. The price on the card is the database price."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                    "offer_id": {"type": "integer"},
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_phrase",
            "description": (
                "Silently save a word the customer explained in this message, then keep selling "
                "in the same reply. If you only inferred the word, wait until their next reply "
                "confirms it before calling this. Do not mention learning, a glossary, or saving "
                "words. Do not save words already listed in LEXICON_DATA."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "language": {"type": "string"},
                    "meaning": {"type": "string"},
                    "example": {"type": "string"},
                    "customer_id": {"type": "integer"},
                },
                "required": ["phrase", "language", "meaning", "customer_id"],
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
    if is_generic_rice(query):
        # "rice" matches every rice. Cap Rice must not win just because C comes first.
        scored.sort(
            key=lambda item: (
                item[0],
                0 if "mama gold" in str(item[1]["name"]).lower() else 1,
                item[1]["name"],
            )
        )
    else:
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


def get_price(
    conn: sqlite3.Connection,
    product: str,
    retailer_id: int,
    customer_id: int | None = None,
    **_extra,
) -> dict:
    retailer_id = _as_int(retailer_id, "retailer_id")
    if customer_id is not None:
        customer_id = _as_int(customer_id, "customer_id")
    matches = find_products(conn, retailer_id, product)
    if not matches:
        return {"ok": False, "error": f"No product matching {product!r} at retailer {retailer_id}."}
    best = _product_payload(matches[0])
    payload = {"ok": True, "retailer_id": retailer_id, **best, "quote_price_ngn": best["price_ngn"]}
    if len(matches) > 1 and _rank(product, matches[0]["name"], matches[0]["sku"]) != 0:
        payload["note"] = "Several products matched. The first match is returned."
        payload["matches"] = [_product_payload(row) for row in matches]
    if customer_id is not None:
        offer = _latest_valid_offer(conn, customer_id, int(best["product_id"]))
        if offer is not None:
            payload["offer_id"] = int(offer["id"])
            payload["offer_price_ngn"] = int(offer["offer_price_ngn"])
            payload["list_price_ngn"] = int(offer["list_price_ngn"])
            payload["quote_price_ngn"] = int(offer["offer_price_ngn"])
            payload["offer_status"] = offer["status"]
            payload["note"] = (
                "quote_price_ngn is the win-back price. Use it in the customer message. "
                "price_ngn is the list price."
            )
    return payload


def _customer(conn: sqlite3.Connection, customer_id: int):
    return conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


_RICE_NOISE = frozenset(
    {
        "i", "im", "wan", "wont", "wanna", "want", "need", "buy", "please", "abeg",
        "the", "a", "am", "for", "me", "you", "to", "some", "of", "get", "and", "na",
        "o", "oo", "show", "see", "look", "like", "let", "send", "give", "make",
        "picture", "pictures", "pic", "pics", "pix", "image", "images", "photo",
        "photos", "foto", "fotos", "flier", "flyer", "fliers", "flyers", "wetin",
        "how", "much", "now", "last", "price", "about", "what", "be", "is", "it",
        "this", "that", "bag", "bags", "one", "dey", "hold", "keep", "any", "just",
        "still", "again", "your", "our", "shop", "my", "customer", "custom",
        "costoma", "kustoma", "customa", "kostomer", "oga", "madam", "pls",
        "do", "have", "can", "we", "with", "from", "when", "them", "abeg",
        "so", "but", "na", "price", "abi",
    }
)
_SIZE = re.compile(r"^\d+(?:kg|g|l|ml|mg)?$")
_NOT_BRAND = frozenset(
    {
        "rice", "bag", "bags", "pack", "carton", "tin", "piece", "bucket", "keg",
        "oil", "bottle", "set", "yard", "yards",
    }
)


def _tokens(text: str | None) -> list[str]:
    """Words with apostrophes removed, so won't is one token and not 'won' + 't'."""
    raw = (text or "").lower()
    for mark in ("'", "’", "‘", "`"):
        raw = raw.replace(mark, "")
    return re.findall(r"[a-z0-9]+", raw)


def is_generic_rice(query: str | None) -> bool:
    """True when they asked for rice and did not name a brand.

    'I wan buy rice' and 'how much be the rice' count. 'Cap Rice' and
    'Mama Gold' do not.
    """
    words = _tokens(query)
    if "rice" not in words:
        return False
    return all(word in _RICE_NOISE or word == "rice" for word in words)


def _brand_tokens(name: str) -> list[str]:
    tokens = []
    for token in _tokens(name):
        if token in _NOT_BRAND or token in _RICE_NOISE or _SIZE.match(token):
            continue
        tokens.append(token)
    return tokens


def named_stocked_product(conn, retailer_id: int, text: str | None):
    """The in-stock product whose brand words appear in the message.

    'mama gold' matches Mama Gold Rice. 'cap rice' matches Cap Rice.
    'caprice' matches neither, and the word 'rice' on its own matches nothing.
    """
    words = set(_tokens(text))
    if not words:
        return None
    rows = conn.execute(
        "SELECT * FROM products WHERE retailer_id = ? AND stock_qty > 0 ORDER BY name",
        (int(retailer_id),),
    ).fetchall()
    best = None
    best_key = None
    folded = " ".join(_tokens(text))
    for row in rows:
        tokens = _brand_tokens(row["name"])
        if not tokens or any(token not in words for token in tokens):
            continue
        position = max(folded.find(token) for token in tokens)
        key = (len(tokens), position)
        if best_key is None or key > best_key:
            best = row
            best_key = key
    return best


def preferred_rice_product(conn, retailer_id: int, favourite: dict | None = None, customer_id: int | None = None):
    """Most-bought in-stock rice for this customer, else the shop's best seller.

    The stored win-back offer is not a vote, and the name sort is not a vote.
    `favourite` is ignored; sales of rice at this shop decide.
    """
    del favourite
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.price_ngn, p.stock_qty,
               COALESCE(SUM(CASE WHEN o.customer_id = ? AND o.status = 'completed' THEN oi.qty ELSE 0 END), 0) AS mine,
               COALESCE(SUM(CASE WHEN o.status = 'completed' THEN oi.qty ELSE 0 END), 0) AS shop_qty
        FROM products p
        LEFT JOIN order_items oi ON oi.product_id = p.id
        LEFT JOIN orders o ON o.id = oi.order_id AND o.retailer_id = p.retailer_id
        WHERE p.retailer_id = ? AND p.stock_qty > 0
        GROUP BY p.id, p.name, p.price_ngn, p.stock_qty
        """,
        (int(customer_id or 0), int(retailer_id)),
    ).fetchall()
    rice = [row for row in rows if "rice" in str(row["name"]).lower()]
    if not rice:
        return None
    rice.sort(
        key=lambda row: (
            -int(row["mine"] or 0),
            -int(row["shop_qty"] or 0),
            -int(row["stock_qty"] or 0),
            int(row["id"]),
        )
    )
    return rice[0]


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
    retailer_id = int(customer["retailer_id"])
    matches = find_products(conn, retailer_id, product, limit=8)
    if not matches:
        return None, {"ok": False, "error": f"No product matching {product!r} for this customer's shop."}
    if is_generic_rice(product):
        preferred = preferred_rice_product(conn, retailer_id, customer_id=int(customer["id"]))
        if preferred is not None:
            full = conn.execute("SELECT * FROM products WHERE id = ?", (int(preferred["id"]),)).fetchone()
            if full is not None:
                return full, None
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
    expire_other_offers(conn, customer_id, offer_id)
    conn.commit()
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


def _same_price_reservation(conn, customer_id: int, product_id: int, offer_id: int, unit_price: int, product):
    """The latest completed order for this offer at this price, or None.

    A second yes confirms that reservation. A different price is not reused,
    so a shelf-price order cannot answer a discounted yes.
    """
    row = conn.execute(
        """
        SELECT o.id, o.total_ngn, o.offer_id, o.ordered_on, o.channel, oi.qty, oi.unit_price_ngn
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.customer_id = ?
          AND o.status = 'completed'
          AND o.offer_id = ?
          AND oi.product_id = ?
          AND oi.unit_price_ngn = ?
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (int(customer_id), int(offer_id), int(product_id), int(unit_price)),
    ).fetchone()
    if row is None:
        return None
    qty = int(row["qty"])
    return {
        "ok": True,
        "order_id": int(row["id"]),
        "customer_id": int(customer_id),
        "product_id": int(product_id),
        "product_name": product["name"],
        "requested_qty": qty,
        "qty": qty,
        "capped": False,
        "max_per_order": MAX_QTY,
        "unit": product["unit"],
        "unit_price_ngn": int(row["unit_price_ngn"]),
        "total_ngn": int(row["total_ngn"]),
        "offer_id": int(offer_id),
        "ordered_on": row["ordered_on"],
        "channel": row["channel"],
        "currency": "NGN",
        "already_reserved": True,
    }


def _latest_valid_offer(conn: sqlite3.Connection, customer_id: int, product_id: int | None = None):
    sql = """
        SELECT * FROM offers
        WHERE customer_id = ? AND status IN ('drafted', 'sent', 'declined')
    """
    params: list = [customer_id]
    if product_id is not None:
        sql += " AND product_id = ?"
        params.append(product_id)
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _reject(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


def _product_by_id(conn, product_id: int):
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def _resolve_named_product(conn, customer, product_id, product: str | None):
    """Product first, inside this retailer. Ambiguous names return candidates and write nothing."""
    retailer_id = int(customer["retailer_id"])
    named = (product or "").strip()
    if product_id is not None:
        row = _product_by_id(conn, product_id)
        if row is None:
            return None, _reject(f"Unknown product_id {product_id}.")
        if int(row["retailer_id"]) != retailer_id:
            return None, _reject("That product belongs to a different retailer.")
        if named and _rank(named, row["name"], row["sku"]) is None:
            return None, _reject("The product name does not match that product id.")
        return row, None
    if not named:
        return None, None
    matches = find_products(conn, retailer_id, named, limit=5)
    if not matches:
        return None, _reject(f"No product matching {named!r} for this customer's shop.")
    exact = [row for row in matches if _rank(named, row["name"], row["sku"]) == 0]
    if len(exact) == 1:
        return exact[0], None
    if len(matches) > 1:
        return None, _reject(
            "Several products match. Ask which one.",
            candidates=[_product_payload(row) for row in matches],
        )
    return matches[0], None


def place_order(
    conn: sqlite3.Connection,
    customer_id: int,
    qty: int,
    product_id: int | None = None,
    product: str | None = None,
    offer_id: int | None = None,
    add_bag: bool = False,
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
    if qty < 1:
        return {"ok": False, "error": "qty must be at least 1."}
    requested_qty = qty
    customer = _customer(conn, customer_id)
    if customer is None:
        return {"ok": False, "error": f"Unknown customer_id {customer_id}."}
    retailer_id = int(customer["retailer_id"])

    row, error = _resolve_named_product(conn, customer, product_id, product)
    if error:
        return error

    offer = None
    if offer_id is not None:
        offer = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        if offer is None:
            return {"ok": False, "error": f"Unknown offer_id {offer_id}."}
        if int(offer["customer_id"]) != customer_id:
            return {"ok": False, "error": "Offer belongs to a different customer."}
        if int(offer["retailer_id"]) != retailer_id:
            return {"ok": False, "error": "Offer belongs to a different retailer."}
        # accepted stays usable. The first Yes marks the win-back accepted, and a
        # later Yes in the demo has to reserve that same discounted bag again.
        if offer["status"] not in ("drafted", "sent", "declined", "accepted"):
            return {"ok": False, "error": f"Offer is {offer['status']} and cannot be used."}
        if row is None:
            row = _product_by_id(conn, int(offer["product_id"]))
            if row is None or int(row["retailer_id"]) != retailer_id:
                return {"ok": False, "error": "That product belongs to a different retailer."}
        elif int(offer["product_id"]) != int(row["id"]):
            return {"ok": False, "error": "That offer is for a different product."}
    elif row is not None:
        attached = _latest_valid_offer(conn, customer_id, int(row["id"]))
        if attached is not None:
            offer = attached
            offer_id = int(attached["id"])
    else:
        return {"ok": False, "error": "Pass product_id, product, or offer_id."}

    confirmation = find_confirmation(
        conn,
        customer_id=customer_id,
        product_id=int(row["id"]),
        qty=requested_qty,
        offer_id=offer_id,
    )
    if confirmation is None:
        return {
            "ok": False,
            "error": "No matching purchase confirmation for this customer, product, quantity, and offer.",
            "confirmed": False,
        }

    stock = int(row["stock_qty"])
    if stock < 1:
        return {
            "ok": False,
            "error": f"Only {stock} {row['unit']} of {row['name']} in stock.",
            "stock_qty": stock,
            "requested_qty": requested_qty,
        }
    qty = min(requested_qty, stock, MAX_QTY)
    if offer is not None:
        unit_price = int(offer["offer_price_ngn"])
    else:
        unit_price = int(row["price_ngn"])
    if offer_id is not None and not add_bag:
        held = _same_price_reservation(conn, customer_id, int(row["id"]), offer_id, unit_price, row)
        if held is not None:
            with transaction(conn):
                consume_confirmation(conn, int(confirmation["id"]))
            return held
    total = unit_price * qty
    ordered_on = scenario_as_of(conn).isoformat()
    with transaction(conn):
        consume_confirmation(conn, int(confirmation["id"]))
        cursor = conn.execute(
            """
            INSERT INTO orders (
                retailer_id, customer_id, ordered_on, status, total_ngn, channel, offer_id
            ) VALUES (?, ?, ?, 'completed', ?, 'whatsapp_sim', ?)
            """,
            (retailer_id, customer_id, ordered_on, total, offer_id),
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
        "requested_qty": requested_qty,
        "qty": qty,
        "capped": qty != requested_qty,
        "max_per_order": MAX_QTY,
        "unit": row["unit"],
        "unit_price_ngn": unit_price,
        "total_ngn": total,
        "offer_id": offer_id,
        "ordered_on": ordered_on,
        "channel": "whatsapp_sim",
        "currency": "NGN",
    }


def get_delivery_options(
    conn: sqlite3.Connection,
    retailer_id: int,
    customer_id: int | None = None,
    **_extra,
) -> dict:
    try:
        retailer_id = _as_int(retailer_id, "retailer_id")
        if customer_id is not None:
            customer_id = _as_int(customer_id, "customer_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    retailer = conn.execute("SELECT * FROM retailers WHERE id = ?", (retailer_id,)).fetchone()
    if retailer is None:
        return {"ok": False, "error": f"Unknown retailer_id {retailer_id}."}
    customer = None
    if customer_id is not None:
        customer = _customer(conn, customer_id)
        if customer is None:
            return {"ok": False, "error": f"Unknown customer_id {customer_id}."}
        if int(customer["retailer_id"]) != retailer_id:
            return {"ok": False, "error": "Customer belongs to a different retailer."}
    same_city = customer is None or customer["city"].strip().lower() == retailer["city"].strip().lower()
    fee = _DELIVERY_FEE_NGN.get(retailer["category_focus"], 2500)
    pickup = {
        "method": "pickup",
        "available": True,
        "window": "same day",
        "fee_ngn": 0,
        "where": f"{retailer['name']}, {retailer['area']}",
    }
    if same_city:
        delivery = {
            "method": "delivery",
            "available": True,
            "window": "1 to 2 days",
            "fee_ngn": fee,
            "where": retailer["city"],
            "note": "A window only. Do not name a weekday or promise a rider for a specific day.",
        }
    else:
        delivery = {
            "method": "delivery",
            "available": False,
            "window": None,
            "fee_ngn": None,
            "where": customer["city"] if customer else None,
            "note": "No delivery outside the shop city in this synthetic catalogue.",
        }
    return {
        "ok": True,
        "synthetic": True,
        "retailer_id": retailer_id,
        "customer_id": customer_id,
        "options": [pickup, delivery],
    }


def send_offer_card(
    conn: sqlite3.Connection,
    customer_id: int,
    offer_id: int | None = None,
    **_extra,
) -> dict:
    """Mark the open offer's promo card to be sent. The channel delivers the image."""
    from convoplex.context import open_offer

    try:
        customer_id = _as_int(customer_id, "customer_id")
        if offer_id is not None:
            offer_id = _as_int(offer_id, "offer_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    offer = open_offer(conn, customer_id)
    if offer is None:
        return {"ok": False, "error": "No open offer to show."}
    if offer_id is not None and int(offer_id) != int(offer["offer_id"]):
        return {"ok": False, "error": "That offer is not the current one."}
    return {
        "ok": True,
        "send_image": True,
        "offer_id": int(offer["offer_id"]),
        "product_name": offer["product_name"],
        "offer_price_ngn": int(offer["offer_price_ngn"]),
        "currency": "NGN",
        "instruction": (
            "The promo card image will be sent. Do not say you cannot send an image. "
            "The caption uses this product and offer_price_ngn."
        ),
    }


def set_language_tool(conn: sqlite3.Connection, customer_id: int, language: str) -> dict:
    """Save a language the customer asked for and hand back the confirmation."""
    code = clean_language(language)
    save_language(conn, int(customer_id), code, channel="whatsapp")
    return {
        "ok": True,
        "language": code,
        "label": language_label(code),
        "confirmation": CONFIRM[code],
        "instruction": (
            f"Write the rest of this turn only in {language_label(code)}. "
            "Start with the confirmation. Do not continue in the previous language."
        ),
    }


def execute_tool(
    conn: sqlite3.Connection,
    name: str,
    arguments: dict,
    context: ExecutionContext | None = None,
    log=None,
) -> dict:
    functions = {
        "check_stock": check_stock,
        "get_price": get_price,
        "create_offer": create_offer,
        "place_order": place_order,
        "get_delivery_options": get_delivery_options,
        "set_language": set_language_tool,
        "send_offer_card": send_offer_card,
        "learn_phrase": store_phrase,
    }
    fn = functions.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "Tool arguments must be a JSON object."}
    if context is not None:
        arguments, denial = scope_arguments(name, arguments, context)
        if denial:
            if log is not None:
                log.append(
                    kind="audit",
                    actor="tool",
                    payload=audit_payload(context, tool=name, reason=str(denial.get("reason") or "denied")),
                )
            return denial
    try:
        return fn(conn, **arguments)
    except TypeError as exc:
        return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def mark_offer_sent(
    conn: sqlite3.Connection,
    offer_id: int,
    message: str,
    language: str | None = None,
) -> None:
    with transaction(conn):
        if language in {"en", "pcm"}:
            conn.execute(
                """
                UPDATE offers
                SET message = ?, language = ?, status = 'sent'
                WHERE id = ? AND status IN ('drafted', 'sent')
                """,
                (message, language, offer_id),
            )
        else:
            conn.execute(
                """
                UPDATE offers
                SET message = ?, status = 'sent'
                WHERE id = ? AND status IN ('drafted', 'sent')
                """,
                (message, offer_id),
            )
        row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if row is not None and row["status"] == "sent":
        note_presented(
            conn,
            {
                "customer_id": int(row["customer_id"]),
                "retailer_id": int(row["retailer_id"]),
                "offer_id": int(row["id"]),
                "product_id": int(row["product_id"]),
            },
        )


def mark_offer_declined(conn: sqlite3.Connection, offer_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE offers SET status = 'declined' WHERE id = ? AND status = 'sent'",
            (offer_id,),
        )
