"""Commercial facts rendered from the database, not from model prose.

Every naira amount, percent, quantity claim, and named product in the
customer-facing text has to agree with the structured payload. A mismatch
replaces the whole message with the deterministic line for that intent.
"""

from __future__ import annotations

import re

from convoplex.messages import (
    accept_message,
    confirm_hold_message,
    decline_message,
    out_of_stock_message,
    question_message,
    winback_message,
)
from convoplex.money import naira

_MONEY = re.compile(
    r"₦\s*(\d[\d,]*)|(?:NGN)\s*(\d[\d,]*)|(\d[\d,]*)\s*(?:NGN|naira)\b",
    re.IGNORECASE,
)
_WORD_AMOUNTS = {
    "one": 1,
    "a": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twenty": 20,
    "fifty": 50,
    "hundred": 100,
}
_WORD_NAIRA = re.compile(
    r"\b(one|a|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty|hundred)\s+naira\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_QTY = re.compile(r"\b(\d+)\s*x\b", re.IGNORECASE)
_FREE_DELIVERY = re.compile(
    r"\bfree delivery\b|\bdelivery is free\b|\bno delivery fee\b|\bdelivery fee na free\b",
    re.IGNORECASE,
)


def commercial_payload(
    profile: dict,
    *,
    offer: dict | None = None,
    order: dict | None = None,
    delivery: dict | None = None,
    other_product_names: list[str] | None = None,
) -> dict | None:
    """Facts the customer is allowed to hear. Prices come from the offer or the order row."""
    held = offer or {}
    if not held and not (order and order.get("ok")):
        return None
    product = (order or {}).get("product_name") or held.get("product_name") or held.get("name")
    qty = (order or {}).get("qty") or held.get("qty") or profile.get("suggested_qty") or 1
    unit = (order or {}).get("unit_price_ngn")
    if unit is None:
        unit = held.get("offer_price_ngn")
    list_price = held.get("list_price_ngn")
    discount = held.get("discount_percent")
    total = (order or {}).get("total_ngn")
    if total is None and unit is not None:
        total = int(unit) * int(qty)
    fee = None
    for option in (delivery or {}).get("options") or []:
        if option.get("method") == "delivery" and option.get("fee_ngn") is not None:
            fee = int(option["fee_ngn"])
    names = [name for name in (other_product_names or []) if name and name != product]
    return {
        "product_name": product,
        "qty": int(qty) if qty is not None else None,
        "unit_price_ngn": int(unit) if unit is not None else None,
        "list_price_ngn": int(list_price) if list_price is not None else None,
        "discount_percent": int(discount) if discount is not None else None,
        "total_ngn": int(total) if total is not None else None,
        "delivery_fee_ngn": fee,
        "currency": "NGN",
        "offer_id": held.get("offer_id") or (order or {}).get("offer_id"),
        "in_stock": None if held.get("stock_qty") is None else int(held.get("stock_qty") or 0) > 0,
        "other_product_names": names,
    }


def money_amounts(text: str) -> list[int]:
    found: list[int] = []
    for match in _MONEY.finditer(text or ""):
        raw = next(group for group in match.groups() if group)
        found.append(int(raw.replace(",", "")))
    for match in _WORD_NAIRA.finditer(text or ""):
        found.append(_WORD_AMOUNTS[match.group(1).lower()])
    return found


def claims_mismatch(text: str, payload: dict) -> bool:
    if payload.get("in_stock") is False and (text or "").strip():
        return True
    allowed = {
        amount
        for amount in (
            payload.get("unit_price_ngn"),
            payload.get("list_price_ngn"),
            payload.get("total_ngn"),
            payload.get("delivery_fee_ngn"),
            0,
        )
        if amount is not None
    }
    unit = payload.get("unit_price_ngn")
    qty = payload.get("qty")
    if unit is not None and qty is not None:
        allowed.add(int(unit) * int(qty))
    for amount in money_amounts(text):
        if amount not in allowed:
            return True
    discount = payload.get("discount_percent")
    for match in _PERCENT.finditer(text or ""):
        percent = float(match.group(1))
        if discount is None or percent != float(discount):
            return True
    if qty is not None:
        for match in _QTY.finditer(text or ""):
            if int(match.group(1)) != int(qty):
                return True
    fee = payload.get("delivery_fee_ngn")
    if fee is not None and fee > 0 and _FREE_DELIVERY.search(text or ""):
        return True
    product = (payload.get("product_name") or "").strip().lower()
    lowered = (text or "").lower()
    for name in payload.get("other_product_names") or []:
        label = str(name).strip().lower()
        if label and label != product and label in lowered:
            return True
    return False


def deterministic_message(
    intent: str | None,
    profile: dict,
    payload: dict,
    *,
    order: dict | None = None,
    delivery: dict | None = None,
    confirm: bool = False,
) -> str:
    if payload.get("in_stock") is False:
        return out_of_stock_message(profile, payload.get("product_name") or "that item")
    if intent == "decline":
        return decline_message(profile)
    if intent == "accept" and order and order.get("ok"):
        return accept_message(profile, order, delivery)
    if confirm:
        return confirm_hold_message(profile, payload.get("product_name"), payload.get("unit_price_ngn"))
    if payload.get("unit_price_ngn") is not None and intent == "question":
        return question_message(
            profile,
            payload.get("product_name") or "",
            payload.get("unit_price_ngn"),
            None,
        )
    offer = {
        "offer_price_ngn": payload.get("unit_price_ngn") or 0,
        "list_price_ngn": payload.get("list_price_ngn") or payload.get("unit_price_ngn") or 0,
        "product_name": payload.get("product_name") or "",
        "discount_percent": payload.get("discount_percent") or 0,
    }
    if offer["offer_price_ngn"]:
        return winback_message(profile, offer)
    return decline_message(profile)


def reconcile_text(
    text: str,
    intent: str | None,
    profile: dict,
    payload: dict | None,
    *,
    order: dict | None = None,
    delivery: dict | None = None,
    confirm: bool = False,
) -> str:
    if not payload:
        return text
    if not claims_mismatch(text or "", payload):
        return text
    fallback = deterministic_message(
        intent, profile, payload, order=order, delivery=delivery, confirm=confirm
    )
    if payload.get("unit_price_ngn") is not None and naira(int(payload["unit_price_ngn"])) not in fallback:
        if intent != "decline" and payload.get("in_stock") is not False:
            clause = naira(int(payload["unit_price_ngn"]))
            fallback = f"{fallback} {clause}".strip()
    return fallback
