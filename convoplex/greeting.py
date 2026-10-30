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


def greeting_reply(ctx: dict, text: str, offer: dict | None) -> str:
    """Mirror My customer / Customer, answer a greeting, then the open offer."""
    language = str(ctx.get("language") or "en")
    if language not in {"en", "pcm", "ig", "yo", "ha"}:
        language = "en"
    address = customer_address(text)
    greeted = has_greeting(text)
    parts: list[str] = []
    if address and greeted:
        parts.append(f"{address}, {_fine(language)}")
    elif address:
        parts.append(address + ",")
    elif greeted:
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
