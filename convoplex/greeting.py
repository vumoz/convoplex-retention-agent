"""Nigerian market greetings and address terms.

'My customer' and 'Customer' are how regular buyers and sellers address each
other. costoma, kustoma, and the rest are the same word from a voice note.
They are not products.
"""

from __future__ import annotations

import re
import unicodedata

from convoplex.money import naira

_CUSTOMER_WORD = (
    r"customers?|costomas?|kustomas?|customas?|kostomers?|kostomas?|custormers?|kustomers?"
)
_CUSTOMER = re.compile(rf"\b(?:my\s+)?(?:{_CUSTOMER_WORD})\b", re.IGNORECASE)
_MY_CUSTOMER = re.compile(rf"\bmy\s+(?:{_CUSTOMER_WORD})\b", re.IGNORECASE)

# Tokens that are never a brand, including ASR spellings of "customer".
SALUTATION_TOKENS = frozenset(
    {
        "customer",
        "customers",
        "costoma",
        "costomas",
        "kustoma",
        "kustomas",
        "customa",
        "customas",
        "kostomer",
        "kostomers",
        "kostoma",
        "custormer",
        "kustomer",
        "oga",
        "madam",
        "madame",
        "aunty",
        "anti",
        "uncle",
        "bros",
        "broda",
        "sista",
        "sister",
        "person",
        "dear",
        "chairman",
        "boss",
        "papa",
        "mama",
    }
)

_GREETING = re.compile(
    r"how you dey|how far|how body|how na|"
    r"good morning|good afternoon|good evening|"
    r"\bndewo\b|\bkedu\b|"
    r"\be kaaro\b|\be kaasan\b|\bbawo\b|"
    r"\bsannu\b|\bina kwana\b|\bina wuni\b",
    re.IGNORECASE,
)
_VOCATIVE = re.compile(
    r"\b(?:oga|madam|madame|aunty|anti|uncle|bros|broda|sista|sister|"
    r"my person|my dear|chairman|boss|papa)\b|"
    r"(?:^|\s)mama(?!\s+gold\b)",
    re.IGNORECASE,
)


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()


def is_salutation_phrase(phrase: str | None) -> bool:
    """True when a lexicon candidate is an address term, not a word to learn."""
    folded = _fold(phrase or "")
    folded = re.sub(r"[^a-z\s]+", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded:
        return False
    if folded in SALUTATION_TOKENS or folded in {"my customer", "my person", "my dear"}:
        return True
    parts = folded.split()
    return all(part in SALUTATION_TOKENS or part == "my" for part in parts)


def customer_address(text: str | None) -> str | None:
    """'My customer' if they said my customer, 'Customer' if they said customer."""
    raw = text or ""
    if not _CUSTOMER.search(raw):
        return None
    if _MY_CUSTOMER.search(raw):
        return "My customer"
    return "Customer"


def has_greeting(text: str | None) -> bool:
    return bool(_GREETING.search(text or ""))


def has_market_address(text: str | None) -> bool:
    raw = text or ""
    return bool(_CUSTOMER.search(raw) or _VOCATIVE.search(raw) or has_greeting(raw))


_FILLER = frozenset(
    {
        "i",
        "im",
        "wan",
        "wont",
        "wanna",
        "buy",
        "please",
        "abeg",
        "the",
        "a",
        "am",
        "for",
        "me",
        "you",
        "to",
        "some",
        "of",
        "get",
        "and",
        "na",
        "o",
        "oo",
        "show",
        "see",
        "look",
        "like",
        "let",
        "send",
        "picture",
        "pictures",
        "pic",
        "pics",
        "pix",
        "image",
        "images",
        "photo",
        "photos",
        "foto",
        "fotos",
        "flier",
        "flyer",
        "fliers",
        "flyers",
        "wetin",
    }
)


def sale_focus(text: str | None) -> str:
    """The request left after the address and the greeting are taken off.

    'my customer, i wan buy rice' becomes 'rice'. 'My customer.' becomes ''.
    """
    raw = text or ""
    raw = _MY_CUSTOMER.sub(" ", raw)
    raw = _CUSTOMER.sub(" ", raw)
    raw = _VOCATIVE.sub(" ", raw)
    raw = _GREETING.sub(" ", raw)
    raw = re.sub(r"[^\w\s]+", " ", raw, flags=re.UNICODE)
    words = [word for word in raw.split() if word.lower() not in _FILLER and len(word) > 1]
    return " ".join(words).strip()


def reply_is_only_address(text: str | None) -> bool:
    """True when nothing is left but the address, punctuation, or a bare hello."""
    folded = _fold(text or "")
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    folded = re.sub(
        r"\b(?:my customer|customer|i dey fine o|im fine|i am fine|a di m mma|mo wa daadaa|lafiya lau)\b",
        " ",
        folded,
    )
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded == ""


def _fine(language: str) -> str:
    if language == "pcm":
        return "I dey fine o!"
    if language == "ig":
        return "A dị m mma."
    if language == "yo":
        return "Mo wa daadaa."
    if language == "ha":
        return "Lafiya lau."
    return "I'm fine."


def _offer_line(language: str, product: str, price: str, *, greeted: bool) -> str:
    if language == "pcm":
        hold = "Make I hold one bag for you?" if greeted else "Make I hold one for you?"
        now = " now" if greeted else ""
        return f"{product} dey, na {price}{now}. {hold}"
    if language == "ig":
        return f"{product} dị, ọ bụ {price}. Ka m chekwaara gị otu?"
    if language == "yo":
        return f"{product} wà, ó jẹ́ {price}. Kí n fi ọ̀kan pamọ́ fún ọ?"
    if language == "ha":
        return f"{product} yana nan, shi ne {price}. In ajiye ɗaya?"
    return f"{product} is {price}. Shall I set one aside for you?"


_PICTURE_EXTRA = frozenset(
    {"give", "send", "make", "want", "need", "abeg", "pls", "please", "one", "bag", "bags"}
)


def picture_query(text: str | None) -> str:
    """The product a picture request is about.

    'the rice' and 'picture of the rice' are this shop's Mama Gold. A named bag
    such as Cap Rice or Ofada stays specific, because a leftover word remains.
    """
    raw = text or ""
    folded = _fold(raw)
    if "rice" not in folded:
        return raw
    scrubbed = re.sub(r"[^\w\s]+", " ", folded, flags=re.UNICODE)
    leftover = [
        word
        for word in scrubbed.split()
        if word not in _FILLER
        and word not in _PICTURE_EXTRA
        and word not in SALUTATION_TOKENS
        and word != "my"
        and word != "rice"
        and len(word) > 1
    ]
    if not leftover:
        return "rice"
    return raw


def present_offer(conn, profile: dict, text: str) -> dict | None:
    """The open offer, or a new one for the product they just named.

    A named brand replaces whatever was offered before. A plain rice request
    uses this customer's win-back rice, which is Mama Gold for the demo shop.
    """
    from convoplex.context import conversation_offer
    from convoplex.tools import (
        create_offer,
        find_products,
        is_generic_rice,
        named_stocked_product,
        preferred_rice_product,
    )

    customer_id = int(profile.get("id") or profile["customer_id"])
    retailer_id = int(profile["retailer_id"])
    held = conversation_offer(conn, customer_id)
    if held and (
        int(held.get("customer_id") or 0) != customer_id
        or int(held.get("retailer_id") or 0) != retailer_id
    ):
        held = None
    named = named_stocked_product(conn, retailer_id, text)
    if named is not None:
        return _ensure_offer(conn, profile, held, named)
    if is_generic_rice(text) or is_generic_rice(sale_focus(text)):
        preferred = preferred_rice_product(conn, retailer_id, customer_id=customer_id)
        if preferred is not None:
            return _ensure_offer(conn, profile, held, preferred)
    focus = sale_focus(text)
    if held is None or (focus and not _offer_matches(conn, profile, held, focus)):
        query = focus or (profile.get("favourite_product") or {}).get("name") or ""
        if not query:
            return held
        if not find_products(conn, retailer_id, query, limit=1):
            return held
        created = create_offer(conn, customer_id=customer_id, discount_percent=10, product=query)
        if not created.get("ok"):
            return held
        held = _held_from_created(created, customer_id)
    return held


def _held_from_created(created: dict, customer_id: int) -> dict:
    return {
        "offer_id": int(created["offer_id"]),
        "product_id": int(created["product_id"]),
        "product_name": created["product_name"],
        "offer_price_ngn": int(created["offer_price_ngn"]),
        "list_price_ngn": int(created["list_price_ngn"]),
        "discount_percent": int(created["discount_percent"]),
        "stock_qty": created.get("stock_qty"),
        "retailer_id": int(created["retailer_id"]),
        "customer_id": customer_id,
        "status": created.get("status") or "drafted",
    }


def _ensure_offer(conn, profile: dict, held: dict | None, product) -> dict | None:
    """Price this product from its own offer, or from the shelf.

    A Cap Rice discount is not applied to Mama Gold. A declined offer is not
    copied into a new row. A drafted or sent offer for this same bag is reused,
    including its discount; an accepted one is reused when that is all we have.
    """
    product_id = int(product["id"])
    customer_id = int(profile.get("id") or profile["customer_id"])
    retailer_id = int(profile["retailer_id"])
    active = _offer_for_product(conn, customer_id, retailer_id, product_id, ("drafted", "sent"))
    if active is not None:
        return active
    if (
        held
        and int(held.get("product_id") or 0) == product_id
        and held.get("status") in {"drafted", "sent", "accepted"}
    ):
        return held
    reserved = _offer_for_product(conn, customer_id, retailer_id, product_id, ("accepted",))
    if reserved is not None:
        return reserved
    shelf = int(product["price_ngn"])
    return _insert_quote(
        conn,
        profile,
        product,
        list_price=shelf,
        offer_price=shelf,
        discount=0,
    )


def _offer_for_product(conn, customer_id: int, retailer_id: int, product_id: int, statuses: tuple[str, ...]):
    placeholders = ", ".join("?" for _ in statuses)
    row = conn.execute(
        f"""
        SELECT o.id, o.product_id, o.discount_percent, o.list_price_ngn, o.offer_price_ngn,
               o.status, p.name AS product_name, p.stock_qty
        FROM offers o
        JOIN products p ON p.id = o.product_id
        WHERE o.customer_id = ?
          AND o.retailer_id = ?
          AND o.product_id = ?
          AND o.status IN ({placeholders})
          AND p.stock_qty > 0
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (customer_id, retailer_id, product_id, *statuses),
    ).fetchone()
    if row is None:
        return None
    return {
        "offer_id": int(row["id"]),
        "product_id": int(row["product_id"]),
        "product_name": row["product_name"],
        "offer_price_ngn": int(row["offer_price_ngn"]),
        "list_price_ngn": int(row["list_price_ngn"]),
        "discount_percent": int(row["discount_percent"]),
        "stock_qty": int(row["stock_qty"]),
        "retailer_id": retailer_id,
        "customer_id": customer_id,
        "status": row["status"],
    }


def _insert_quote(conn, profile: dict, product, *, list_price: int, offer_price: int, discount: int) -> dict:
    from datetime import datetime, timezone

    from convoplex.db import transaction

    customer_id = int(profile.get("id") or profile["customer_id"])
    retailer_id = int(profile["retailer_id"])
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
                retailer_id,
                customer_id,
                int(product["id"]),
                int(discount),
                int(list_price),
                int(offer_price),
                None,
                str(profile.get("language") or "en"),
                created,
            ),
        )
        offer_id = int(cursor.lastrowid)
    return {
        "offer_id": offer_id,
        "product_id": int(product["id"]),
        "product_name": product["name"],
        "offer_price_ngn": int(offer_price),
        "list_price_ngn": int(list_price),
        "discount_percent": int(discount),
        "stock_qty": int(product["stock_qty"]),
        "retailer_id": retailer_id,
        "customer_id": customer_id,
        "status": "drafted",
    }


def _offer_matches(conn, profile: dict, offer: dict, focus: str) -> bool:
    from convoplex.tools import find_products, is_generic_rice, preferred_rice_product

    name = str(offer.get("product_name") or "").lower()
    # "rice" is inside "Cap Rice" too. Generic rice is the bag she buys most.
    if is_generic_rice(focus):
        preferred = preferred_rice_product(
            conn,
            int(profile.get("retailer_id") or offer.get("retailer_id") or 0),
            customer_id=int(profile.get("id") or profile.get("customer_id") or 0),
        )
        return preferred is not None and int(preferred["id"]) == int(offer.get("product_id") or 0)
    if focus.lower() in name:
        return True
    retailer_id = int(profile.get("retailer_id") or offer.get("retailer_id") or 0)
    rows = find_products(conn, retailer_id, focus, limit=1)
    return bool(rows) and int(rows[0]["id"]) == int(offer.get("product_id") or 0)


def finish_presented_offer(conn, profile: dict, offer: dict, text: str) -> None:
    """Drafted offers become the open offer the Yes button can confirm."""
    from convoplex.tools import mark_offer_sent

    if not offer or not offer.get("offer_id"):
        return
    mark_offer_sent(
        conn,
        int(offer["offer_id"]),
        text,
        language=str(profile.get("language") or "en"),
    )


def greeting_reply(ctx: dict, text: str, offer: dict | None) -> str:
    """Mirror My customer / Customer, answer a greeting, then the open offer."""
    language = str(ctx.get("language") or "en")
    if language not in {"en", "pcm", "ig", "yo", "ha"}:
        language = "en"
    address = customer_address(text)
    greeted = has_greeting(text)
    parts: list[str] = []
    bare = not sale_focus(text)
    if address and (greeted or bare):
        parts.append(f"{address}, {_fine(language)}")
    elif address:
        parts.append(address + ",")
    elif greeted or bare:
        parts.append(_fine(language))
    if offer and offer.get("product_name") and offer.get("offer_price_ngn") is not None:
        parts.append(
            _offer_line(
                language,
                str(offer["product_name"]),
                naira(int(offer["offer_price_ngn"])),
                greeted=greeted,
            )
        )
    if not parts:
        parts.append(_fine(language))
    return " ".join(parts).replace("  ", " ").strip()
