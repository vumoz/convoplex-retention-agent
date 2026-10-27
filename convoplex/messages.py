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
    lines = (ctx.get("payday") or {}).get("customer_line") or {}
    language = "pcm" if ctx.get("language") == "pcm" else "en"
    sentence = lines.get(language)
    if sentence:
        return f" {sentence}"
    return ""


def delivery_sentence(ctx: dict, delivery: dict | None) -> str:
    """Pickup and delivery from get_delivery_options, in the customer's language."""
    options = (delivery or {}).get("options") or []
    pickup = next((item for item in options if item.get("method") == "pickup"), None)
    rider = next((item for item in options if item.get("method") == "delivery"), None)
    pidgin = ctx.get("language") == "pcm"
    if pickup and pickup.get("available") and rider and rider.get("available"):
        if pidgin:
            return (
                f"You fit choose pickup ({pickup.get('window')} for {pickup.get('where')}) "
                f"or delivery ({rider.get('window')} for {rider.get('where')})."
            )
        return (
            f"You can choose pickup ({pickup.get('window')} at {pickup.get('where')}) "
            f"or delivery ({rider.get('window')} in {rider.get('where')})."
        )
    if pickup and pickup.get("available") and rider and not rider.get("available"):
        if pidgin:
            return (
                f"Pickup na {pickup.get('window')} for {pickup.get('where')}. "
                "Delivery no dey outside the shop city."
            )
        return (
            f"Pickup is {pickup.get('window')} at {pickup.get('where')}. "
            "Delivery is not available outside the shop city."
        )
    if pidgin:
        return "Pickup dey for the shop. Tell us if you want delivery."
    return "Pickup is at the shop. Tell us if you want delivery."


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


def accept_message(ctx: dict, order: dict, delivery: dict | None = None) -> str:
    first = _first(ctx["full_name"])
    total = naira(order["total_ngn"])
    product = order["product_name"]
    qty = order["qty"]
    arrival = delivery_sentence(ctx, delivery)
    if ctx.get("language") == "pcm":
        return (
            f"E don set, {first}. We don reserve {qty} x {product} for you. "
            f"Total na {total}. {arrival}"
        )
    return (
        f"Done, {first}. We have reserved {qty} x {product} for you. "
        f"The total is {total}. {arrival}"
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
    offer = ctx.get("open_offer") or {}
    if offer.get("offer_price_ngn") is not None and (
        not offer.get("product_name") or offer.get("product_name") == product_name
    ):
        price_ngn = int(offer["offer_price_ngn"])
        product_name = offer.get("product_name") or product_name
        if offer.get("stock_qty") is not None:
            stock_qty = offer["stock_qty"]
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
