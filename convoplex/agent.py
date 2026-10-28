"""Retention agent loop. Tool results are executed locally and written to the action log."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from convoplex.actionlog import ActionLog
from convoplex.context import agent_context
from convoplex.languages import CONFIRM, guard_reply, message_asks_something_else, message_is_language_only
from convoplex.lexicon import clarify_question, hold_line, normalize_phrase
from convoplex.integrations.whatsapp import (
    CONFIRM_NO_ID,
    CONFIRM_NO_TITLE,
    CONFIRM_YES_ID,
    CONFIRM_YES_TITLE,
)
from convoplex.llm import (
    Completion,
    LLMError,
    ToolCall,
    classify_reply,
    explicit_refusal,
    purchase_is_ambiguous,
    split_intent,
)
from convoplex.messages import (
    accept_message,
    confirm_hold_message,
    decline_message,
    offer_price_clause,
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
    confirm_offer_id: int | None = None


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
    from_voice: bool = False,
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
        from_voice=from_voice,
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
    from_voice: bool = False,
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
    switched_to: str | None = None
    language_only = task == "reply" and message_is_language_only(user_text)
    followups = 0
    reply_kwargs: dict = {}
    if task == "reply":
        # muse-spark-1.3 always reasons. "minimal" is the shortest effort the
        # chat API accepts ("none" is HTTP 400). A length finish must not
        # double the budget: that retry spent most of the live language switch.
        reply_kwargs = {"reasoning_effort": "minimal", "retry_on_length": False}
    decision = _reply_decision(user_text, profile, log, customer_id, from_voice) if task == "reply" else None
    clarify_question_text = None
    blocked_repeat_ask = False

    for _round in (() if decision else range(1, MAX_ROUNDS + 1)):
        try:
            completion = llm.complete(messages, tools=TOOLS, max_tokens=max_tokens, **reply_kwargs)
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
            if call.name == "ask_phrase":
                result = _resolve_ask(log, customer_id, call.arguments, profile)
                if result.get("ask") and clarify_question_text is None:
                    clarify_question_text = result.get("question")
                elif result.get("already_asked"):
                    blocked_repeat_ask = True
            else:
                result = execute_tool(conn, call.name, call.arguments)
            if call.name == "learn_phrase" and result.get("ok"):
                log.append(
                    kind="lexicon",
                    actor="agent",
                    payload={
                        "action": "learn",
                        "customer_id": customer_id,
                        "id": result.get("id"),
                        "phrase": result.get("phrase"),
                        "language": result.get("language"),
                        "meaning": result.get("meaning"),
                        "source": result.get("source"),
                        "confirmations": result.get("confirmations"),
                        "created": result.get("created"),
                    },
                )
            if call.name == "set_language" and result.get("ok"):
                switched_to = str(result["language"])
                profile["language"] = switched_to
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
        if clarify_question_text or blocked_repeat_ask:
            break
        if switched_to and task == "reply":
            only_set = all(name == "set_language" for name in trace)
            extra = message_asks_something_else(user_text)
            if language_only or (only_set and not extra):
                break
            if followups >= 1:
                break
            followups += 1
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The language is saved. Answer the rest of the customer's message "
                        "in that language, in one or two short sentences. Start with the "
                        "confirmation from set_language. Do not call set_language again."
                    ),
                }
            )
            continue
    else:
        if decision is None:
            raise LLMError(
                "Muse Spark kept calling tools without a final message. Stopped after 6 rounds."
            )

    raw = completion.content if completion and completion.content else ""
    text, intent = split_intent(raw)

    if task == "reply" and order and order.get("ok"):
        intent = "accept"
    elif task == "reply" and intent is None and user_text:
        intent = classify_reply(user_text)

    if clarify_question_text:
        intent = "question"
    elif blocked_repeat_ask:
        intent = "question"
    if decision == "accept":
        intent = "accept"
    elif decision == "decline":
        intent = "decline"
    elif decision == "confirm":
        intent = "question"

    if (
        task == "reply"
        and intent == "accept"
        and order is None
        and profile.get("open_offer")
        and not clarify_question_text
    ):
        if not delivery:
            delivery = _required_delivery(conn, log, customer_id, profile, trace)
        order = _fallback_place(conn, log, customer_id, profile, trace, user_text or "")
        if order and order.get("ok"):
            intent = "accept"

    if task == "reply":
        text = _strip_thread_greeting(text, profile.get("retailer_name") or "")
    if task == "reply" and intent == "decline":
        text = _without_payday(text) or decline_message(profile)
    confirm_offer_id = None
    if decision == "confirm":
        intent = "question"
        order = None
        held = profile.get("open_offer") or {}
        text = confirm_hold_message(profile, held.get("product_name"), held.get("offer_price_ngn"))
        if held.get("offer_id") is not None:
            confirm_offer_id = int(held["offer_id"])

    if task == "winback" and offer and offer.get("ok") and "₦" not in text:
        text = winback_message(profile, offer)
    if task == "winback":
        text = one_seasonal_hook(text)
    if task == "reply" and intent == "question" and profile.get("open_offer") and not clarify_question_text and not blocked_repeat_ask:
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
        text = _ensure_offer_price(text, profile)

    if switched_to:
        profile["language"] = switched_to
        confirmation = CONFIRM[switched_to]
        extra = message_asks_something_else(user_text)
        if language_only or not extra or not (text or "").strip():
            text = confirmation
        elif confirmation not in text:
            text = f"{confirmation}\n\n{text.strip()}"
    if clarify_question_text:
        text = clarify_question_text
        intent = "question"
    elif blocked_repeat_ask:
        text = hold_line(profile.get("language"))
        intent = "question"
    text = guard_reply(text, profile.get("language"))
    if task == "winback" and offer and offer.get("ok"):
        mark_offer_sent(conn, int(offer["offer_id"]), text, language=profile.get("language"))
        offer = {**offer, "status": "sent", "message": text}
    if task == "reply" and intent == "decline" and profile.get("open_offer") and not clarify_question_text:
        mark_offer_declined(conn, int(profile["open_offer"]["offer_id"]))

    payload = {
        "customer_id": customer_id,
        "task": task,
        "direction": "out",
        "content": text,
        "intent": intent,
    }
    if confirm_offer_id is not None:
        payload["confirm_offer_id"] = confirm_offer_id
    log.append(
        kind="message",
        actor="agent",
        payload=payload,
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
        confirm_offer_id=confirm_offer_id,
    )


def _log_model_calls(log: ActionLog, calls, customer_id: int, task: str) -> None:
    for call in calls or []:
        usage = getattr(call, "usage", None) or {}
        payload = {
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
        }
        if task == "reply":
            payload["reasoning_effort"] = "minimal"
            payload["retry_on_length"] = False
        log.append(
            kind="model_call",
            actor="agent",
            payload=payload,
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


def _asked_before(log: ActionLog, customer_id: int, phrase: str) -> bool:
    norm = normalize_phrase(phrase)
    for entry in log.read_all():
        if entry.get("kind") != "tool_result":
            continue
        payload = entry.get("payload") or {}
        if payload.get("name") != "ask_phrase":
            continue
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        result = payload.get("result") or {}
        if result.get("ask") and normalize_phrase(result.get("phrase")) == norm:
            return True
    return False


def _resolve_ask(log: ActionLog, customer_id: int, arguments: dict, profile: dict) -> dict:
    arguments = arguments if isinstance(arguments, dict) else {}
    phrase = normalize_phrase(arguments.get("phrase"))
    language = arguments.get("language") or profile.get("language") or "en"
    if not phrase:
        return {"ok": False, "error": "phrase is empty", "ask": False, "already_asked": False, "phrase": ""}
    if _asked_before(log, customer_id, phrase):
        return {
            "ok": True,
            "ask": False,
            "already_asked": True,
            "phrase": phrase,
            "language": language,
            "question": None,
        }
    return {
        "ok": True,
        "ask": True,
        "already_asked": False,
        "phrase": phrase,
        "language": language,
        "question": clarify_question(phrase, language),
    }


def _reply_decision(user_text: str | None, profile: dict, log: ActionLog, customer_id: int, from_voice: bool) -> str | None:
    """accept, decline, confirm, or None to let the model decide.

    A Broken English voice note that says "won't buy" is "wan buy" when an
    offer is open. Other customers are asked once, then a repeat accepts.
    """
    if not (user_text or "").strip():
        return None
    choice = _button_choice(user_text)
    if choice == "yes":
        return "accept"
    if choice == "no":
        return "decline"
    if explicit_refusal(user_text):
        return "decline"
    offer = profile.get("open_offer") or {}
    if _pcm_voice_wants(user_text, from_voice, profile.get("language"), offer):
        return "accept"
    offer_id = offer.get("offer_id")
    if purchase_is_ambiguous(user_text) and offer_id is not None:
        if _confirm_already_asked(log, customer_id, int(offer_id)):
            return "accept"
        return "confirm"
    return None


def _button_choice(text: str | None) -> str | None:
    folded = (text or "").strip().lower()
    if folded in {CONFIRM_YES_ID, CONFIRM_YES_TITLE.lower()}:
        return "yes"
    if folded in {CONFIRM_NO_ID, CONFIRM_NO_TITLE.lower()}:
        return "no"
    return None


def _pcm_voice_wants(text: str, from_voice: bool, language: str | None, offer: dict) -> bool:
    """Transcriber writes Pidgin 'wan buy' as 'won't buy'. That is a yes."""
    if not from_voice or language != "pcm" or not offer:
        return False
    if explicit_refusal(text):
        return False
    folded = (text or "").replace("’", "'").replace("‘", "'")
    return bool(
        re.search(
            r"\b(?:won'?t|wont|wan|wanna)\b(?:\s+\w+){0,4}\s+\b(?:buy|take|get)\b",
            folded,
            re.IGNORECASE,
        )
    )


def _confirm_already_asked(log: ActionLog, customer_id: int, offer_id: int) -> bool:
    last = None
    for entry in log.read_all():
        if entry.get("kind") != "message" or entry.get("actor") != "agent":
            continue
        payload = entry.get("payload") or {}
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        if payload.get("direction") == "out":
            last = payload
    return bool(last and int(last.get("confirm_offer_id") or 0) == int(offer_id))


def _ensure_offer_price(text: str, profile: dict) -> str:
    held = profile.get("open_offer") or {}
    clause = offer_price_clause(profile, held.get("product_name"), held.get("offer_price_ngn"))
    if clause and clause not in (text or ""):
        return f"{(text or '').rstrip()} {clause}".strip()
    return text


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
