"""Deterministic customer-facing copy used by the mock model and as a price guardrail.

The live Muse Spark model writes its own message. If that draft omits a naira
amount after a real tool result, the agent substitutes these templates so a
customer is never quoted a made-up price.
"""

from __future__ import annotations

from convoplex.money import naira


def _first(full_name: str) -> str:
    return full_name.split()[0]


def _payday_note(ctx: dict) -> str:
    status = (ctx.get("payday") or {}).get("status")
    pidgin = ctx.get("language") == "pcm"
    if status == "passed":
        if pidgin:
            return " Month-end salary don enter for many people, so this one go help."
        return " Month-end salary has landed for many people, so this is a good week to restock."
    if status == "open":
        if pidgin:
            return " Month-end payday dey near, salary never land yet."
        return " Month-end payday is close, but salary has not necessarily landed yet."
    if status == "upcoming":
        if pidgin:
            return " Payday still dey come from the 25th. E never enter yet."
        return " Payday is still upcoming, from the 25th. Salary has not entered yet."
    blob = " ".join(ctx.get("local_context") or []).lower()
    if blob:
        if pidgin:
            return " Festive season dey come, so e good make you arrange am early."
        return " Festive season is coming up, so this is a good time to sort it."
    return ""


def winback_message(ctx: dict, offer: dict) -> str:
    first = _first(ctx["full_name"])
    price = naira(offer["offer_price_ngn"])
    was = naira(offer["list_price_ngn"])
    product = offer["product_name"]
    days = ctx.get("recency_days")
    shop = ctx["retailer_name"]
    area = ctx.get("retailer_area") or ctx["area"]
    discount = offer["discount_percent"]
    note = _payday_note(ctx)
    if ctx.get("language") == "pcm":
        quiet = f"You never buy from us for {days} days. " if days is not None else ""
        return (
            f"{first}, how you dey? Na {shop} for {area}. "
            f"{quiet}"
            f"{product} still dey. "
            f"I fit give you {discount}% off: {price} instead of {was}."
            f"{note} "
            f"Reply YES make we keep am for you."
        )
    quiet = f"It's been {days} days since your last order. " if days is not None else ""
    return (
        f"Hello {first}, this is {shop} in {area}. "
        f"{quiet}"
        f"{product} is in stock. "
        f"We can offer {discount}% off: {price} instead of {was}."
        f"{note} "
        f"Reply YES and we will set it aside for you."
    )


def out_of_stock_message(ctx: dict, product_name: str) -> str:
    first = _first(ctx["full_name"])
    shop = ctx["retailer_name"]
    if ctx.get("language") == "pcm":
        return (
            f"{first}, na {shop}. We wan bring you back, but {product_name} no dey for shop right now. "
            f"We go ping you once e land."
        )
    return (
        f"Hello {first}, this is {shop}. We wanted to welcome you back, but {product_name} is out of stock right now. "
        f"We will message you when it arrives."
    )


def accept_message(ctx: dict, order: dict) -> str:
    first = _first(ctx["full_name"])
    total = naira(order["total_ngn"])
    product = order["product_name"]
    qty = order["qty"]
    shop = ctx["retailer_name"]
    area = ctx.get("retailer_area") or ctx["area"]
    if ctx.get("language") == "pcm":
        return (
            f"E don set, {first}. We don reserve {qty} x {product} for you. "
            f"Total na {total}. Come collect am at {shop}, {area}, or tell us if you want delivery."
        )
    return (
        f"Done, {first}. We have reserved {qty} x {product} for you. "
        f"The total is {total}. Pick it up at {shop}, {area}, or tell us if you want delivery."
    )


def decline_message(ctx: dict) -> str:
    first = _first(ctx["full_name"])
    area = ctx.get("retailer_area") or ctx["area"]
    if ctx.get("language") == "pcm":
        return f"No wahala, {first}. Whenever you ready, we dey for {area}. Thank you."
    return (
        f"No problem, {first}. We will be here in {area} whenever you are ready. Thank you."
    )


def question_message(ctx: dict, product_name: str, price_ngn: int | None, stock_qty: int | None) -> str:
    first = _first(ctx["full_name"])
    price = naira(price_ngn) if price_ngn is not None else "the shelf price"
    stock = stock_qty if stock_qty is not None else "some"
    if ctx.get("language") == "pcm":
        return (
            f"{first}, {product_name} na {price} now, and we get {stock} for shop. "
            f"You wan make we keep one?"
        )
    return (
        f"{first}, {product_name} is {price} right now, and we have {stock} in stock. "
        f"Would you like us to set one aside?"
    )
