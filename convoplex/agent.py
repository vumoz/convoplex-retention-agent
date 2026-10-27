"""Retention agent loop. Tool results are executed locally and written to the action log."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from convoplex.actionlog import ActionLog
from convoplex.context import agent_context
from convoplex.llm import Completion, LLMError, ToolCall, classify_reply, split_intent
from convoplex.messages import (
    accept_message,
    decline_message,
    delivery_fee_ngn,
    one_seasonal_hook,
    polish_acceptance,
    question_message,
    winback_message,
)
from convoplex.money import naira
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


def draft_winback(
    conn,
    log: ActionLog,
    llm,
    customer_id: int,
    *,
    max_tokens: int,
    language: str | None = None,
) -> AgentResult:
    return _run(
        conn,
        log,
        llm,
        customer_id,
        task="winback",
        user_text=None,
        max_tokens=max_tokens,
        language=language,
    )


def handle_reply(
    conn,
    log: ActionLog,
    llm,
    customer_id: int,
    text: str,
    *,
    max_tokens: int,
    language: str | None = None,
) -> AgentResult:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("reply text is empty")
    return _run(
        conn,
        log,
        llm,
        customer_id,
        task="reply",
        user_text=cleaned,
        max_tokens=max_tokens,
        language=language,
    )


def _run(
    conn,
    log: ActionLog,
    llm,
    customer_id: int,
    *,
    task: str,
    user_text: str | None,
    max_tokens: int,
    language: str | None = None,
) -> AgentResult:
    prior = _prior_tool_results(log, customer_id) if task == "reply" else None
    profile = agent_context(
        conn,
        customer_id,
        task,
        language=language,
        prior_tool_results=prior,
    )
    if profile is None:
        raise LookupError(f"Unknown customer {customer_id}")
    messages = build_messages(
        conn,
        customer_id,
        task,
        user_text,
        language=language,
        prior_tool_results=prior,
    )
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
    delivery: dict | None = (prior or {}).get("get_delivery_options")
    trace: list[str] = []
    completion: Completion | None = None

    for _round in range(1, MAX_ROUNDS + 1):
        try:
            completion = llm.complete(messages, tools=TOOLS, max_tokens=max_tokens)
        except LLMError as exc:
            _log_model_calls(log, getattr(exc, "calls", None), customer_id, task)
            raise
        _log_model_calls(log, completion.calls, customer_id, task)
        calls = _delivery_before_order(completion.tool_calls, delivery, profile, customer_id)
        if calls is not completion.tool_calls:
            completion.tool_calls = calls
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
            if call.name == "get_delivery_options" and result.get("ok"):
                delivery = result
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
        if not delivery:
            delivery = _required_delivery(conn, log, customer_id, profile, trace)
        order = _fallback_place(conn, log, customer_id, profile, trace, user_text or "")
        if order and order.get("ok"):
            intent = "accept"

    if task == "reply":
        text = _strip_thread_greeting(text, profile.get("retailer_name") or "")
    if task == "reply" and intent == "decline":
        text = _without_payday(text) or decline_message(profile)

    if task == "winback" and offer and offer.get("ok") and "₦" not in text:
        text = winback_message(profile, offer)
    if task == "winback":
        text = one_seasonal_hook(text)
    if task == "reply" and intent == "question" and profile.get("open_offer"):
        offer_price = int(profile["open_offer"]["offer_price_ngn"])
        if naira(offer_price) not in text:
            held = profile["open_offer"]
            text = question_message(profile, held["product_name"], offer_price, held.get("stock_qty"))
    if task == "reply" and intent == "accept" and order and order.get("ok"):
        fee = delivery_fee_ngn(delivery)
        fee_stated = fee is None or naira(fee) in text
        if naira(int(order["total_ngn"])) not in text or not _states_arrival(text) or not fee_stated:
            text = accept_message(profile, order, delivery)
        text = polish_acceptance(text, profile, delivery, user_text)

    if task == "winback" and offer and offer.get("ok"):
        mark_offer_sent(conn, int(offer["offer_id"]), text, language=profile.get("language"))
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


def _log_model_calls(log: ActionLog, calls, customer_id: int, task: str) -> None:
    for call in calls or []:
        usage = getattr(call, "usage", None) or {}
        log.append(
            kind="model_call",
            actor="agent",
            payload={
                "customer_id": customer_id,
                "task": task,
                "attempt": getattr(call, "attempt", 1),
                "max_tokens": getattr(call, "max_tokens", None),
                "finish_reason": getattr(call, "finish_reason", None),
                "latency_ms": getattr(call, "latency_ms", None),
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                },
            },
        )


def _prior_tool_results(log: ActionLog, customer_id: int) -> dict:
    found: dict = {}
    for entry in log.read_all():
        if entry.get("kind") != "tool_result":
            continue
        payload = entry.get("payload") or {}
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        result = payload.get("result") or {}
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        name = payload.get("name")
        if name:
            found[name] = result
    return found


def _without_payday(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [sentence for sentence in sentences if "payday" not in sentence.lower()]
    return " ".join(kept).strip()


def _strip_thread_greeting(text: str, shop_name: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    shop = shop_name.lower()
    for index, sentence in enumerate(sentences):
        low = sentence.lower()
        if index <= 1 and (
            "how you dey" in low
            or "how body" in low
            or "hope you are well" in low
            or low.startswith("hello ")
            or low.startswith("hi ")
        ):
            continue
        if index <= 1 and shop and low.startswith("na ") and shop in low:
            continue
        if index <= 1 and shop and low.startswith("this is ") and shop in low:
            continue
        kept.append(sentence)
    return " ".join(kept).strip() or text


def _delivery_before_order(calls: list, delivery: dict | None, profile: dict, customer_id: int) -> list:
    """Look up pickup and delivery before place_order runs."""
    if not calls or delivery:
        return calls
    names = [call.name for call in calls]
    if "place_order" not in names:
        return calls
    ordered = list(calls)
    if "get_delivery_options" not in names:
        ordered.insert(
            0,
            ToolCall(
                id="call_required_delivery",
                name="get_delivery_options",
                arguments={"retailer_id": profile["retailer_id"], "customer_id": customer_id},
            ),
        )
    else:
        ordered.sort(key=lambda call: (call.name != "get_delivery_options", call.name == "place_order"))
    return ordered


def _states_arrival(text: str) -> bool:
    low = text.lower()
    pickup = "pickup" in low or "pick it up" in low or "pick up" in low or "collect" in low
    delivery = "delivery" in low
    return pickup and delivery


def _fallback_place(
    conn: sqlite3.Connection,
    log: ActionLog,
    customer_id: int,
    profile: dict,
    trace: list[str],
    user_text: str,
) -> dict:
    offer = profile["open_offer"]
    stock = int(offer.get("stock_qty") or 1)
    numbers = [int(number) for number in re.findall(r"\d+", user_text)]
    requested = numbers[-1] if numbers else int(profile.get("suggested_qty") or 1)
    qty = max(1, min(requested, stock))
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


def _required_delivery(conn, log: ActionLog, customer_id: int, profile: dict, trace: list[str]) -> dict | None:
    arguments = {"retailer_id": profile["retailer_id"], "customer_id": customer_id}
    trace.append("get_delivery_options")
    log.append(
        kind="tool_call",
        actor="agent",
        payload={
            "customer_id": customer_id,
            "task": "reply",
            "tool_call_id": "call_required_delivery",
            "name": "get_delivery_options",
            "arguments": arguments,
            "source": "delivery_required",
        },
    )
    result = execute_tool(conn, "get_delivery_options", arguments)
    log.append(
        kind="tool_result",
        actor="tool",
        payload={
            "customer_id": customer_id,
            "task": "reply",
            "tool_call_id": "call_required_delivery",
            "name": "get_delivery_options",
            "ok": bool(result.get("ok")),
            "result": result,
            "source": "delivery_required",
        },
    )
    return result if result.get("ok") else None
