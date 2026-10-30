"""Immutable scope for one turn. Tools cannot switch customer or retailer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    retailer_id: int
    customer_id: int
    conversation_id: int
    inbound_event_id: str | None = None

    @staticmethod
    def for_customer(profile: dict, *, inbound_event_id: str | None = None) -> ExecutionContext:
        customer_id = int(profile.get("id") or profile["customer_id"])
        return ExecutionContext(
            retailer_id=int(profile["retailer_id"]),
            customer_id=customer_id,
            conversation_id=customer_id,
            inbound_event_id=inbound_event_id,
        )


_NEEDS_CUSTOMER = {
    "create_offer",
    "place_order",
    "set_language",
    "send_offer_card",
    "learn_phrase",
    "get_delivery_options",
    "get_price",
    "ask_phrase",
}
_NEEDS_RETAILER = {"check_stock", "get_price", "get_delivery_options"}


def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def scope_arguments(name: str, arguments: dict, context: ExecutionContext) -> tuple[dict | None, dict | None]:
    """Return (arguments, None) or (None, denial).

    A supplied customer_id or retailer_id that disagrees with the context is a
    denial. A missing id is filled from the context so the tool cannot guess.
    """
    scoped = dict(arguments)
    checks = (("customer_id", context.customer_id), ("retailer_id", context.retailer_id))
    for field, bound in checks:
        if field not in scoped or scoped[field] is None or scoped[field] == "":
            continue
        supplied = _as_int(scoped[field])
        if supplied is None or supplied != int(bound):
            return None, {
                "ok": False,
                "denied": True,
                "error": "This action is outside the current customer.",
                "reason": "identity_mismatch",
                "field": field,
            }
    if name in _NEEDS_CUSTOMER:
        scoped["customer_id"] = context.customer_id
    if name in _NEEDS_RETAILER and name != "ask_phrase":
        scoped["retailer_id"] = context.retailer_id
    return scoped, None


def audit_payload(context: ExecutionContext, *, tool: str, reason: str) -> dict:
    """Safe audit line: ids of the bound customer only, no phone and no token."""
    return {
        "event": "denied",
        "tool": tool,
        "reason": reason,
        "customer_id": context.customer_id,
        "retailer_id": context.retailer_id,
        "conversation_id": context.conversation_id,
    }
