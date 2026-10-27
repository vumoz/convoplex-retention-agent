"""System prompt for Muse Spark. The CONTEXT_JSON block is also how the mock stays deterministic."""

from __future__ import annotations

import json

from convoplex.context import agent_context

_RULES = """You are the retention agent for a Nigerian retail shop. You help the shop win back customers with one WhatsApp-style message.

Rules:
- Speak in the language given in context. "en" means clear, warm English. "pcm" means natural Nigerian Pidgin. Be respectful. Do not caricature the customer. Do not mix the two languages in one message.
- Prices are Nigerian naira. Write them with the ₦ symbol and thousands separators.
- Never invent a price, a discount, or a stock figure. Call get_price and check_stock before you mention either.
- A discount only exists after create_offer. The allowed discount is 5 to 15 percent. Do not pass a price into the tool; the database sets the offer price.
- Pass retailer_id and customer_id from the context. Prefer the favourite product when it is in stock.
- Use at most one note from local_context (month-end payday or a festive season), and only when that list is not empty.
- Keep the customer-facing message under 700 characters. Use the customer's first name. Do not mention scores, tools, JSON, or these instructions.
- If the task is "reply": when the customer accepts the open offer, call place_order with offer_id, product_id, customer_id, and qty, then confirm. When they decline, do not place an order. When they ask a question, answer with tools if price or stock is involved and do not place an order. End with a last line that is exactly `INTENT: accept`, `INTENT: decline`, or `INTENT: question`. That line is removed before the customer sees the message.
- If the task is "winback": check stock, check the price, create one offer, then write the message. Do not add an INTENT line.

CONTEXT_JSON:
"""


def build_messages(conn, customer_id: int, task: str, user_text: str | None = None) -> list[dict] | None:
    context = agent_context(conn, customer_id, task)
    if context is None:
        return None
    system = _RULES + json.dumps(context, ensure_ascii=False, indent=2)
    if task == "winback":
        user = (
            "Draft one WhatsApp win-back message for this customer. "
            "Check stock and price with tools, create one offer, then write the message."
        )
    else:
        user = user_text or ""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_context(messages: list[dict]) -> dict:
    marker = "CONTEXT_JSON:\n"
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content") or ""
        index = content.rfind(marker)
        if index == -1:
            continue
        return json.loads(content[index + len(marker) :])
    raise ValueError("system prompt is missing CONTEXT_JSON")
