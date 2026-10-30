"""An unstocked brand is a product question, not a refusal.

The shop says the name is not on the shelf and offers the closest in-stock
item from the database, usually the open offer. Prices come from that row.
"""

from __future__ import annotations

import re
import unicodedata

from convoplex.greeting import is_salutation_phrase
from convoplex.languages import message_is_language_only, requested_language
from convoplex.lexicon import normalize_phrase
from convoplex.tools import find_products

_DEFINITION = re.compile(
    r"\b(?:wetin be|wetin mean|what does|what is|what do you mean|you mean)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)

# Function words and sale chatter. A brand token is whatever is left.
_STOP = {
    "i",
    "im",
    "you",
    "we",
    "una",
    "they",
    "get",
    "got",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "any",
    "the",
    "and",
    "for",
    "our",
    "shop",
    "please",
    "abeg",
    "biko",
    "dey",
    "wan",
    "wont",
    "won't",
    "wanna",
    "will",
    "buy",
    "talk",
    "speak",
    "switch",
    "change",
    "language",
    "hausa",
    "igbo",
    "yoruba",
    "english",
    "broken",
    "naija",
    "ibo",
    "how",
    "much",
    "last",
    "price",
    "now",
    "wetin",
    "be",
    "what",
    "this",
    "that",
    "with",
    "from",
    "your",
    "are",
    "was",
    "not",
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "ehen",
    "oya",
    "send",
    "take",
    "deliver",
    "delivery",
    "pickup",
    "today",
    "one",
    "bag",
    "make",
    "hold",
    "want",
    "need",
    "sell",
    "stock",
    "carry",
    "about",
    "nwere",
    "jowo",
    "don",
    "die",
    "still",
    "naira",
    "off",
    "just",
    "like",
    "can",
    "pls",
    "again",
    "mean",
    "means",
    "meaning",
    "small",
    "extra",
    "little",
    "word",
}

_AVAILABILITY = re.compile(
    r"\b(?:have|get|got|stock|sell|nwere|carry)\b",
    re.IGNORECASE,
)


def _fold(token: str) -> str:
    text = unicodedata.normalize("NFKD", token)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip("'")


def _lexicon_phrases(conn) -> set[str]:
    rows = conn.execute(
        "SELECT phrase FROM lexicon WHERE removed_at IS NULL"
    ).fetchall()
    return {normalize_phrase(row["phrase"]) for row in rows}


def _content_words(text: str, lexicon: set[str]) -> list[str]:
    kept: list[str] = []
    for raw in _WORD.findall(text or ""):
        folded = _fold(raw)
        if len(folded) < 3 or folded in _STOP or is_salutation_phrase(folded):
            continue
        if folded in lexicon or normalize_phrase(raw) in lexicon:
            continue
        kept.append(raw)
    return kept


def _token_in_stock(conn, retailer_id: int, token: str) -> bool:
    needle = _fold(token)
    if len(needle) < 3:
        return False
    for row in find_products(conn, retailer_id, token, limit=5):
        if int(row["stock_qty"]) <= 0:
            continue
        name = _fold(row["name"])
        sku = _fold(row["sku"])
        if needle in name or needle == sku:
            return True
    return False


def _looks_like_brand(raw: str, missing: list[str]) -> bool:
    """A product name, not a slang word the lexicon is meant to ask about.

    Caprice, Royal Stallion, and 'do you have caprice' count. 'jara again' does not.
    """
    if not missing:
        return False
    if _AVAILABILITY.search(raw):
        return True
    if any(word[:1].isupper() for word in missing):
        return True
    if len(missing) >= 2:
        return True
    folded = _fold(missing[0])
    others = [
        word
        for word in _WORD.findall(raw)
        if _fold(word) not in _STOP and _fold(word) != folded and len(_fold(word)) >= 3
    ]
    return not others and len(folded) >= 6


def unstocked_request(conn, text: str | None, profile: dict) -> dict | None:
    """Return the missing name and the in-stock alternative, or None.

    A definition question and a name we already stock stay on the normal path.
    An unknown or out-of-stock brand does not close the sale.
    """
    raw = (text or "").strip()
    if not raw or _DEFINITION.search(raw) or message_is_language_only(raw) or requested_language(raw):
        return None
    words = _content_words(raw, _lexicon_phrases(conn))
    if not words:
        return None
    retailer_id = int(profile["retailer_id"])
    missing_words = [word for word in words if not _token_in_stock(conn, retailer_id, word)]
    if not _looks_like_brand(raw, missing_words):
        return None
    offer = profile.get("open_offer") or {}
    if offer.get("product_name") and offer.get("offer_price_ngn") is not None:
        return {
            "missing": " ".join(missing_words),
            "product_name": offer["product_name"],
            "price_ngn": int(offer["offer_price_ngn"]),
            "offer_id": offer.get("offer_id"),
        }
    favourite = profile.get("favourite_product") or {}
    if favourite.get("name") and int(favourite.get("stock_qty") or 0) > 0 and favourite.get("price_ngn"):
        return {
            "missing": " ".join(missing_words),
            "product_name": favourite["name"],
            "price_ngn": int(favourite["price_ngn"]),
            "offer_id": None,
        }
    return None
