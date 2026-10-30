"""Deterministic customer-facing copy used by the mock model and as a price guardrail.

The live Muse Spark model writes its own message. If that draft omits a naira
amount after a real tool result, the agent substitutes these templates so a
customer is never quoted a made-up price.
"""

from __future__ import annotations

import re

from convoplex.money import naira

# Payday sentences the calendar actually gives the model. A festive sentence
# stacked on top of one of these is a second hook and has to go.
_PAYDAY_CUES = (
    "month-end",
    "payday",
    "salary don land",
    "salary has landed",
    "e good time to stock up",
    "a good time to restock",
    "e good time to plan",
    "a good time to plan ahead",
)
_FESTIVE_CUES = (
    "detty",
    "christmas",
    "easter",
    "eid",
    "new yam",
    "independence",
    "democracy day",
    "children's day",
)


def _first(full_name: str) -> str:
    return full_name.split()[0]


def _code(ctx: dict) -> str:
    language = str(ctx.get("language") or "en")
    if language not in {"en", "pcm", "ig", "yo", "ha"}:
        return "en"
    return language


def _payday_note(ctx: dict) -> str:
    """The one seasonal hook the template is allowed to add.

    Festive notes stay in local_context for the model. The template does not
    append them as well, so a win-back never carries two hooks.
    """
    lines = (ctx.get("payday") or {}).get("customer_line") or {}
    language = _code(ctx)
    sentence = lines.get(language) or lines.get("en")
    if sentence:
        return f" {sentence}"
    return ""


def _is_payday(text: str) -> bool:
    low = text.lower()
    return any(cue in low for cue in _PAYDAY_CUES)


def _is_festive(text: str) -> bool:
    low = text.lower()
    return any(cue in low for cue in _FESTIVE_CUES)


def one_seasonal_hook(text: str) -> str:
    """Keep a single seasonal hook. Payday wins when a draft stacked both."""
    pieces = [piece for piece in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if piece]
    if not pieces:
        return text or ""
    if not any(_is_payday(piece) for piece in pieces) or not any(_is_festive(piece) for piece in pieces):
        return " ".join(pieces)
    kept: list[str] = []
    for piece in pieces:
        if _is_festive(piece) and not _is_payday(piece):
            continue
        if _is_festive(piece) and _is_payday(piece):
            piece = _drop_festive_clause(piece)
        if piece:
            kept.append(piece)
    return " ".join(kept).strip()


def _drop_festive_clause(sentence: str) -> str:
    parts = re.split(r"\s*(?:…|\.\.\.|—|–|;)\s*", sentence)
    kept = [part.strip() for part in parts if part.strip() and not (_is_festive(part) and not _is_payday(part))]
    if not kept:
        return sentence
    text = " ".join(part.rstrip() for part in kept)
    if sentence.rstrip().endswith((".", "!", "?")) and not text.endswith((".", "!", "?")):
        text += sentence.rstrip()[-1]
    return text


def delivery_fee_ngn(delivery: dict | None) -> int | None:
    """The rider fee from get_delivery_options, when delivery is actually offered."""
    options = (delivery or {}).get("options") or []
    rider = next((item for item in options if item.get("method") == "delivery"), None)
    if not rider or not rider.get("available") or rider.get("fee_ngn") is None:
        return None
    return int(rider["fee_ngn"])


def delivery_fee_sentence(ctx: dict, delivery: dict | None) -> str:
    fee = delivery_fee_ngn(delivery)
    if fee is None:
        return ""
    amount = naira(fee)
    code = _code(ctx)
    if code == "pcm":
        return f"Delivery fee na {amount}."
    if code == "ig":
        return f"Ụgwọ nnyefe bụ {amount}."
    if code == "yo":
        return f"Owó ìfiránṣẹ́ jẹ́ {amount}."
    if code == "ha":
        return f"Kuɗin kai shi ne {amount}."
    return f"The delivery fee is {amount}."


def _split_sentences(text: str) -> list[str]:
    return [piece for piece in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if piece]


def _fee_line_language(sentence: str) -> str | None:
    """A standalone fee announcement, not a longer sentence that mentions the fee."""
    low = sentence.strip().lower()
    if re.match(r"^(the )?delivery fee is\b", low):
        return "en"
    if re.match(r"^delivery fee na\b", low):
        return "pcm"
    if low.startswith("ụgwọ nnyefe"):
        return "ig"
    if low.startswith("owó ìfiránṣẹ́") or low.startswith("owo ifiranse"):
        return "yo"
    if low.startswith("kuɗin kai") or low.startswith("kudin kai"):
        return "ha"
    return None


def _drop_other_language_fee_lines(text: str, ctx: dict) -> str:
    """Keep fee announcements in the customer's language only."""
    spoken = _code(ctx)
    kept: list[str] = []
    seen_fee = False
    for sentence in _split_sentences(text):
        kind = _fee_line_language(sentence)
        if kind is not None and kind != spoken:
            continue
        if kind is not None:
            if seen_fee:
                continue
            seen_fee = True
        kept.append(sentence)
    return " ".join(kept).strip()


def ensure_delivery_fee(text: str, ctx: dict, delivery: dict | None) -> str:
    """Append the tool's delivery fee once, in the customer's language, when it is missing."""
    text = _drop_other_language_fee_lines(text, ctx)
    fee = delivery_fee_ngn(delivery)
    if fee is None or naira(fee) in (text or ""):
        return text
    extra = delivery_fee_sentence(ctx, delivery)
    if not extra:
        return text
    return f"{(text or '').rstrip()} {extra}".strip()


def customer_wants_delivery(user_text: str | None) -> bool:
    """True when the customer already asked for delivery, as in 'Abeg deliver am'."""
    low = (user_text or "").lower()
    if re.search(r"\b(no|don't|do not)\b.{0,20}\bdeliver", low):
        return False
    if re.search(r"\b(how much|how many|wetin be|price of)\b", low):
        return False
    return re.search(r"\bdeliver", low) is not None


def _asks_to_set_delivery(sentence: str) -> bool:
    low = sentence.lower()
    return bool(
        re.search(
            r"make i set the delivery|make we set (the )?delivery|should i set (the )?delivery|"
            r"you wan make we deliver|should we deliver",
            low,
        )
    )


def _offers_fulfillment_choice(sentence: str) -> bool:
    low = sentence.lower()
    choice = "you fit choose" in low or "you can choose" in low
    return choice and "pickup" in low and "delivery" in low


def _confirms_delivery(text: str) -> bool:
    low = text.lower()
    return any(
        phrase in low
        for phrase in ("we go deliver", "we will deliver", "we don set delivery", "delivery is set")
    )


def _delivery_option(delivery: dict | None) -> dict | None:
    options = (delivery or {}).get("options") or []
    rider = next((item for item in options if item.get("method") == "delivery"), None)
    if not rider or not rider.get("available"):
        return None
    return rider


def delivery_confirmation(ctx: dict, delivery: dict | None, *, include_fee: bool) -> str:
    rider = _delivery_option(delivery)
    if rider is None:
        return ""
    window = rider.get("window") or "1 to 2 days"
    where = rider.get("where") or "your area"
    fee = delivery_fee_ngn(delivery)
    code = _code(ctx)
    if code == "pcm":
        line = f"We go deliver am ({window} for {where})."
        if include_fee and fee is not None:
            return f"{line} Delivery fee na {naira(fee)}."
        return line
    if code == "ig":
        line = f"Anyị ga-ebuga ya ({window} na {where})."
        if include_fee and fee is not None:
            return f"{line} Ụgwọ nnyefe bụ {naira(fee)}."
        return line
    if code == "yo":
        line = f"A óò gbé e lọ ({window} ní {where})."
        if include_fee and fee is not None:
            return f"{line} Owó ìfiránṣẹ́ jẹ́ {naira(fee)}."
        return line
    if code == "ha":
        line = f"Za mu kai shi ({window} a {where})."
        if include_fee and fee is not None:
            return f"{line} Kuɗin kai shi ne {naira(fee)}."
        return line
    line = f"We will deliver it ({window} in {where})."
    if include_fee and fee is not None:
        return f"{line} The delivery fee is {naira(fee)}."
    return line


def polish_acceptance(
    text: str,
    ctx: dict,
    delivery: dict | None,
    user_text: str | None = None,
) -> str:
    """One fee line, in the customer's language, and a confirmation when they already chose delivery."""
    text = ensure_delivery_fee(text, ctx, delivery)
    if not customer_wants_delivery(user_text):
        return text
    if _delivery_option(delivery) is None:
        return text
    kept = [
        sentence
        for sentence in _split_sentences(text)
        if not _asks_to_set_delivery(sentence) and not _offers_fulfillment_choice(sentence)
    ]
    text = " ".join(kept).strip()
    if _confirms_delivery(text):
        return ensure_delivery_fee(text, ctx, delivery)
    fee = delivery_fee_ngn(delivery)
    include_fee = fee is not None and naira(fee) not in text
    extra = delivery_confirmation(ctx, delivery, include_fee=include_fee)
    if not extra:
        return text
    return f"{text} {extra}".strip() if text else extra


def delivery_sentence(ctx: dict, delivery: dict | None) -> str:
    """Pickup and delivery from get_delivery_options, in the customer's language."""
    options = (delivery or {}).get("options") or []
    pickup = next((item for item in options if item.get("method") == "pickup"), None)
    rider = next((item for item in options if item.get("method") == "delivery"), None)
    code = _code(ctx)
    pidgin = code == "pcm"
    fee = delivery_fee_ngn(delivery)
    fee_text = naira(fee) if fee is not None else None
    if pickup and pickup.get("available") and rider and rider.get("available"):
        if code == "ig":
            paid = f", ụgwọ nnyefe {fee_text}" if fee_text else ""
            return (
                f"Ị nwere ike ịhọrọ iburu ({pickup.get('window')} na {pickup.get('where')}) "
                f"ma ọ bụ nnyefe ({rider.get('window')} na {rider.get('where')}{paid})."
            )
        if code == "yo":
            paid = f", owó ìfiránṣẹ́ {fee_text}" if fee_text else ""
            return (
                f"O lè yan ìgbé ({pickup.get('window')} ní {pickup.get('where')}) "
                f"tàbí ìfiránṣẹ́ ({rider.get('window')} ní {rider.get('where')}{paid})."
            )
        if code == "ha":
            paid = f", kuɗin kai {fee_text}" if fee_text else ""
            return (
                f"Za ka iya zaɓar ɗauka ({pickup.get('window')} a {pickup.get('where')}) "
                f"ko kai ({rider.get('window')} a {rider.get('where')}{paid})."
            )
        if pidgin:
            paid = f", delivery fee na {fee_text}" if fee_text else ""
            return (
                f"You fit choose pickup ({pickup.get('window')} for {pickup.get('where')}) "
                f"or delivery ({rider.get('window')} for {rider.get('where')}{paid})."
            )
        paid = f", delivery fee {fee_text}" if fee_text else ""
        return (
            f"You can choose pickup ({pickup.get('window')} at {pickup.get('where')}) "
            f"or delivery ({rider.get('window')} in {rider.get('where')}{paid})."
        )
    if pickup and pickup.get("available") and rider and not rider.get("available"):
        if code == "ig":
            return (
                f"Iburu bụ {pickup.get('window')} na {pickup.get('where')}. "
                "Nnyefe adịghị na mpụga obodo ụlọ ahịa."
            )
        if code == "yo":
            return (
                f"Ìgbé jẹ́ {pickup.get('window')} ní {pickup.get('where')}. "
                "Ìfiránṣẹ́ kò sí ní ìta ìlú ṣọ́ọ̀bù."
            )
        if code == "ha":
            return (
                f"Ɗauka ita ce {pickup.get('window')} a {pickup.get('where')}. "
                "Kai ba ya nan a wajen garin shago."
            )
        if pidgin:
            return (
                f"Pickup na {pickup.get('window')} for {pickup.get('where')}. "
                "Delivery no dey outside the shop city."
            )
        return (
            f"Pickup is {pickup.get('window')} at {pickup.get('where')}. "
            "Delivery is not available outside the shop city."
        )
    if code == "ig":
        return "Iburu dị n'ụlọ ahịa. Gwa anyị ma ịchọrọ nnyefe."
    if code == "yo":
        return "Ìgbé wà ní ṣọ́ọ̀bù. Sọ fún wa tí o bá fẹ́ ìfiránṣẹ́."
    if code == "ha":
        return "Ɗauka tana nan a shago. Faɗa mana idan kana son kai."
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
    code = _code(ctx)
    if code == "ig":
        quiet = f"Ụbọchị {days} gafeworo kemgbe ihe ikpeazụ ị zụtara. " if days is not None else ""
        return (
            f"{first}, kedu? Ọ bụ {shop} na {area}. "
            f"{quiet}"
            f"{product} ka dị. "
            f"Enwere m ike inye gị {discount}% mbelata: {price} kama {was}."
            f"{note} "
            f"Zaghachi EE ka anyị chekwaara gị ya."
        )
    if code == "yo":
        quiet = f"Ọjọ́ {days} lọ ti o ti ra ohun kẹ́yìn. " if days is not None else ""
        return (
            f"{first}, báwo ni? {shop} ní {area} ni èyí. "
            f"{quiet}"
            f"{product} wà. "
            f"A lè fún ọ ní {discount}% ẹdinwo: {price} dípò {was}."
            f"{note} "
            f"Dáhùn BẸ́Ẹ̀NI kí a fi pamọ́ fún ọ."
        )
    if code == "ha":
        quiet = f"Kwanaki {days} sun wuce tun sayayyarka ta ƙarshe. " if days is not None else ""
        return (
            f"{first}, sannu? {shop} a {area} ne. "
            f"{quiet}"
            f"{product} yana nan. "
            f"Zan iya ba ka {discount}% ragiya: {price} maimakon {was}."
            f"{note} "
            f"Amsa EE mu ajiye maka ka."
        )
    if code == "pcm":
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
    code = _code(ctx)
    if code == "pcm":
        return (
            f"{first}, na {shop}. We wan bring you back, but {product_name} no dey for shop right now. "
            f"We go ping you once e land."
        )
    if code == "ig":
        return (
            f"{first}, ọ bụ {shop}. Anyị chọrọ ịkpọghachi gị, mana {product_name} adịghị n'ụlọ ahịa ugbu a. "
            f"Anyị ga-agwa gị mgbe ọ bịara."
        )
    if code == "yo":
        return (
            f"{first}, {shop} ni èyí. A fẹ́ kí o padà, ṣùgbọ́n {product_name} kò sí ní ṣọ́ọ̀bù báyìí. "
            f"A óò sọ fún ọ nígbà tó bá dé."
        )
    if code == "ha":
        return (
            f"{first}, {shop} ne. Muna son ka dawo, amma {product_name} babu a shago yanzu. "
            f"Za mu gaya maka idan ya iso."
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
    code = _code(ctx)
    if code == "ig":
        return (
            f"E meela, {first}. Anyị echekwala {qty} x {product} maka gị. "
            f"Mkpọkọta bụ {total}. {arrival}"
        )
    if code == "yo":
        return (
            f"Ó ti ṣe, {first}. A ti fi {qty} x {product} pamọ́ fún ọ. "
            f"Àpapọ̀ jẹ́ {total}. {arrival}"
        )
    if code == "ha":
        return (
            f"An gama, {first}. Mun ajiye {qty} x {product} maka ka. "
            f"Jimla ita ce {total}. {arrival}"
        )
    if code == "pcm":
        return (
            f"E don set, {first}. We don reserve {qty} x {product} for you. "
            f"Total na {total}. {arrival}"
        )
    return (
        f"Done, {first}. We have reserved {qty} x {product} for you. "
        f"The total is {total}. {arrival}"
    )


def offer_price_clause(ctx: dict, product_name: str | None, price_ngn: int | None) -> str:
    """The open-offer price in the customer's language, for example 'Mama Gold Rice 50kg na ₦76,014.'"""
    product = (product_name or "").strip()
    if not product or price_ngn is None:
        return ""
    price = naira(price_ngn)
    code = _code(ctx)
    if code == "pcm":
        return f"{product} na {price}."
    if code == "ig":
        return f"{product} bụ {price}."
    if code == "yo":
        return f"{product} jẹ́ {price}."
    if code == "ha":
        return f"{product} shi ne {price}."
    return f"{product} is {price}."


def confirm_hold_message(ctx: dict, product_name: str | None, price_ngn: int | None) -> str:
    """One confirm question. The open-offer price is included when we have it."""
    code = _code(ctx)
    clause = offer_price_clause(ctx, product_name, price_ngn)
    if code == "pcm":
        ask = "You wan buy am abi you no want am?"
    elif code == "ig":
        ask = "Ị chọrọ ịzụta ya ka ị chọghị?"
    elif code == "yo":
        ask = "Ṣé o fẹ́ rà á tàbí o ò fẹ́?"
    elif code == "ha":
        ask = "Kana son siya ko ba ka so ba?"
    else:
        ask = "Do you want to buy it, or not?"
    if clause:
        return f"{ask} {clause}"
    return ask


def substitute_message(ctx: dict, missing: str, product_name: str, price_ngn: int) -> str:
    """The shop does not stock `missing`. Offer the in-stock alternative at the database price.

    This is a question, not a goodbye. The Yes button is attached by the caller.
    """
    price = naira(price_ngn)
    brand = (missing or "").strip() or "That one"
    product = (product_name or "").strip() or "the one we have"
    code = _code(ctx)
    if code == "pcm":
        return (
            f"{brand} no dey for our shop o, but {product} dey, na {price} now. "
            f"Make I hold one for you?"
        )
    if code == "ig":
        return (
            f"{brand} adịghị n'ụlọ ahịa anyị, mana {product} dị, ọ bụ {price} ugbu a. "
            f"Ka m chekwaara gị otu?"
        )
    if code == "yo":
        return (
            f"{brand} kò sí ní ṣọ́ọ̀bù wa, ṣùgbọ́n {product} wà, ó jẹ́ {price} báyìí. "
            f"Kí n fi ọ̀kan pamọ́ fún ọ?"
        )
    if code == "ha":
        return (
            f"{brand} babu a shagonmu, amma {product} yana nan, shi ne {price} yanzu. "
            f"In ajiye ɗaya maka ka?"
        )
    return (
        f"We don't have {brand}. {product} is in stock at {price}. "
        f"Shall I set one aside for you?"
    )


def decline_message(ctx: dict) -> str:
    first = _first(ctx["full_name"])
    code = _code(ctx)
    if code == "pcm":
        return f"No wahala, {first}. We dey here when you ready."
    if code == "ig":
        return f"Ọ dị mma, {first}. Anyị nọ ebe a mgbe ị dị njikere."
    if code == "yo":
        return f"Kò sí wàhálà, {first}. A wà níbí nígbà tí o bá ṣetán."
    if code == "ha":
        return f"Babu matsala, {first}. Muna nan idan ka shirya."
    return f"No problem, {first}. We are here when you are ready."


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
    code = _code(ctx)
    if code == "ig":
        return (
            f"{first}, {product_name} bụ {price} ugbu a, anyị nwere {stock} n'ụlọ ahịa. "
            f"Ị chọrọ ka anyị chekwaara gị otu?"
        )
    if code == "yo":
        return (
            f"{first}, {product_name} jẹ́ {price} báyìí, a sì ní {stock} ní ṣọ́ọ̀bù. "
            f"Ṣé kí a fi ọ̀kan pamọ́ fún ọ?"
        )
    if code == "ha":
        return (
            f"{first}, {product_name} shi ne {price} yanzu, muna da {stock} a shago. "
            f"Kana son mu ajiye ɗaya?"
        )
    if code == "pcm":
        return (
            f"{first}, {product_name} na {price} now, and we get {stock} for shop. "
            f"You wan make we keep one?"
        )
    return (
        f"{first}, {product_name} is {price} right now, and we have {stock} in stock. "
        f"Would you like us to set one aside?"
    )
