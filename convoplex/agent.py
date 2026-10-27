"""Retention agent loop. Tool results are executed locally and written to the action log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from convoplex.actionlog import ActionLog
from convoplex.context import agent_context
from convoplex.llm import Completion, LLMError, classify_reply, split_intent
from convoplex.messages import accept_message, winback_message
from convoplex.prompts import build_messages
from convoplex.tools import TOOLS, execute_tool, mark_offer_declined, mark_offer_sent

MAX_ROUNDS = 6


@dataclass
class AgentResult:
    task: str
    customer_id: int
    message: str
    intent: str | None
    offer: dict | None
    order: dict | None
    language: str = "en"
    tool_trace: list[str] = field(default_factory=list)
    llm_mode: str = "mock"


def draft_winback(conn, log: ActionLog, llm, customer_id: int, *, max_tokens: int) -> AgentResult:
    return _run(conn, log, llm, customer_id, task="winback", user_text=None, max_tokens=max_tokens)


def handle_reply(
    conn,
    log: ActionLog,
    llm,
    customer_id: int,
    text: str,
    *,
    max_tokens: int,
) -> AgentResult:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("reply text is empty")
    return _run(conn, log, llm, customer_id, task="reply", user_text=cleaned, max_tokens=max_tokens)


def _run(conn, log: ActionLog, llm, customer_id: int, *, task: str, user_text: str | None, max_tokens: int) -> AgentResult:
    profile = agent_context(conn, customer_id, task)
    if profile is None:
        raise LookupError(f"Unknown customer {customer_id}")
    messages = build_messages(conn, customer_id, task, user_text)
    if messages is None:
        raise LookupError(f"Unknown customer {customer_id}")

    if task == "reply":
        log.append(
            kind="message",
            actor="customer",
            payload={"customer_id": customer_id, "task": task, "content": user_text},
        )
    else:
        log.append(
            kind="message",
            actor="system",
            payload={"customer_id": customer_id, "task": task, "content": "draft win-back"},
        )

    offer: dict | None = None
    order: dict | None = None
    trace: list[str] = []
    completion: Completion | None = None

    for _round in range(1, MAX_ROUNDS + 1):
        completion = llm.complete(messages, tools=TOOLS, max_tokens=max_tokens)
        messages.append(completion.assistant_message())
        if completion.content and not completion.tool_calls:
            break
        if not completion.tool_calls:
            break
        for call in completion.tool_calls:
            trace.append(call.name)
            log.append(
                kind="tool_call",
                actor="agent",
                payload={
                    "customer_id": customer_id,
                    "task": task,
                    "tool_call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            )
            result = execute_tool(conn, call.name, call.arguments)
            if call.name == "create_offer" and result.get("ok"):
                offer = result
            if call.name == "place_order" and result.get("ok"):
                order = result
            log.append(
                kind="tool_result",
                actor="tool",
                payload={
                    "customer_id": customer_id,
                    "task": task,
                    "tool_call_id": call.id,
                    "name": call.name,
                    "ok": bool(result.get("ok")),
                    "result": result,
                },
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    else:
        raise LLMError(
            "Muse Spark kept calling tools without a final message. Stopped after 6 rounds."
        )

    raw = completion.content if completion and completion.content else ""
    text, intent = split_intent(raw)

    if task == "reply" and order and order.get("ok"):
        intent = "accept"
    elif task == "reply" and intent is None and user_text:
        intent = classify_reply(user_text)

    if task == "reply" and intent == "accept" and order is None and profile.get("open_offer"):
        order = _fallback_place(conn, log, customer_id, profile, trace)
        if order and order.get("ok"):
            intent = "accept"

    if task == "winback" and offer and offer.get("ok") and "₦" not in text:
        text = winback_message(profile, offer)
    if task == "reply" and intent == "accept" and order and order.get("ok") and "₦" not in text:
        text = accept_message(profile, order)

    if task == "winback" and offer and offer.get("ok"):
        mark_offer_sent(conn, int(offer["offer_id"]), text)
        offer = {**offer, "status": "sent", "message": text}
    if task == "reply" and intent == "decline" and profile.get("open_offer"):
        mark_offer_declined(conn, int(profile["open_offer"]["offer_id"]))

    log.append(
        kind="message",
        actor="agent",
        payload={
            "customer_id": customer_id,
            "task": task,
            "direction": "out",
            "content": text,
            "intent": intent,
        },
    )
    return AgentResult(
        task=task,
        customer_id=customer_id,
        message=text,
        intent=intent,
        offer=offer,
        order=order,
        language=profile.get("language") or "en",
        tool_trace=trace,
        llm_mode=getattr(llm, "mode", "mock"),
    )


def _fallback_place(conn: sqlite3.Connection, log: ActionLog, customer_id: int, profile: dict, trace: list[str]) -> dict:
    offer = profile["open_offer"]
    stock = int(offer.get("stock_qty") or 1)
    qty = max(1, min(int(profile.get("suggested_qty") or 1), stock, 3))
    arguments = {
        "customer_id": customer_id,
        "product_id": offer["product_id"],
        "qty": qty,
        "offer_id": offer["offer_id"],
    }
    trace.append("place_order")
    log.append(
        kind="tool_call",
        actor="agent",
        payload={
            "customer_id": customer_id,
            "task": "reply",
            "tool_call_id": "call_fallback_place_order",
            "name": "place_order",
            "arguments": arguments,
            "source": "intent_fallback",
        },
    )
    result = execute_tool(conn, "place_order", arguments)
    log.append(
        kind="tool_result",
        actor="tool",
        payload={
            "customer_id": customer_id,
            "task": "reply",
            "tool_call_id": "call_fallback_place_order",
            "name": "place_order",
            "ok": bool(result.get("ok")),
            "result": result,
            "source": "intent_fallback",
        },
    )
    return result
