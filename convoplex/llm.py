"""Muse Spark client.

Live mode posts the OpenAI-compatible chat shape documented for api.meta.ai.
Mock mode is deterministic and is what tests and CI run when META_API_KEY is unset.
Both return the same tool-call structure, so the agent loop does not care which is active.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import httpx

from convoplex.config import MIN_TOOL_TOKENS
from convoplex.messages import (
    accept_message,
    decline_message,
    out_of_stock_message,
    question_message,
    winback_message,
)
from convoplex.prompts import parse_context

EXPLICIT_ACCEPT = (
    "yes",
    "yeah",
    "yep",
    "no wahala",
    "send am",
    "i'll take",
    "ill take",
    "i want",
    "i go buy",
    "please send",
    "add am",
    "go ahead",
    "e dey ok",
)
SOFT_ACCEPT = ("ok", "okay", "alright", "confirm", "deal")
DECLINE = (
    "no thanks",
    "no thank you",
    "not now",
    "i no want",
    "leave am",
    "maybe later",
    "maybe next",
    "not interested",
    "i no need",
    "do not",
    "don't",
    "cancel",
)
# "wan" is "want" in Broken English. A voice note often hears it as "won't".
_AMBIGUOUS_BUY = re.compile(
    r"\b(?:won'?t|wont|wan|wanna)\b(?:\s+\w+){0,4}\s+\b(?:buy|take|get)\b"
    r"|\b(?:buy|take|get)\b(?:\s+\w+){0,4}\s+\b(?:won'?t|wont|wan|wanna)\b",
    re.IGNORECASE,
)


class LLMError(RuntimeError):
    def __init__(self, message: str, *, calls: list | None = None):
        super().__init__(message)
        self.calls = list(calls or [])


def purchase_is_ambiguous(text: str | None) -> bool:
    """True when won't/wan/wanna sits next to buy, take, or get.

    In Broken English, "wan" means want. Voice transcription often writes "won't".
    That pair must not cancel an offer on its own.
    """
    folded = (text or "").replace("’", "'").replace("‘", "'")
    return bool(_AMBIGUOUS_BUY.search(folded))


def classify_reply(text: str) -> str:
    """Keyword intent for the mock, and a fallback when a live draft omits INTENT."""
    lowered = text.lower().strip()
    if purchase_is_ambiguous(lowered):
        return "question"
    if any(_contains(lowered, phrase) for phrase in EXPLICIT_ACCEPT):
        return "accept"
    if re.search(r"\bdeliver", lowered) and not re.search(
        r"\b(how much|how many|wetin be|price of)\b", lowered
    ):
        return "accept"
    if any(_contains(lowered, phrase) for phrase in DECLINE) or lowered in {"no", "nope", "nah"}:
        return "decline"
    if any(_contains(lowered, phrase) for phrase in SOFT_ACCEPT):
        return "accept"
    return "question"


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def split_intent(text: str) -> tuple[str, str | None]:
    lines = text.rstrip().splitlines()
    if not lines:
        return "", None
    last = lines[-1].strip()
    if last.lower().startswith("intent:"):
        intent = last.split(":", 1)[1].strip().lower()
        if intent in {"accept", "decline", "question"}:
            return "\n".join(lines[:-1]).strip(), intent
    return text.strip(), None


def effective_max_tokens(requested: int, tools: list | None) -> int:
    if tools and requested < MIN_TOOL_TOKENS:
        return MIN_TOOL_TOKENS
    return requested


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelCall:
    """One chat completion attempt, including a length retry."""

    latency_ms: int
    usage: dict
    finish_reason: str
    max_tokens: int
    attempt: int


def empty_usage() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


def _as_int_token(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_from_body(body: dict) -> dict:
    raw = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        return empty_usage()
    details = raw.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    prompt = _as_int_token(raw.get("prompt_tokens"))
    completion = _as_int_token(raw.get("completion_tokens"))
    total = _as_int_token(raw.get("total_tokens")) or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": _as_int_token(details.get("reasoning_tokens")),
        "total_tokens": total,
    }


@dataclass
class Completion:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    calls: list[ModelCall] = field(default_factory=list)

    def assistant_message(self) -> dict:
        message: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("tool arguments must be a JSON object")
    return data


def _message_text(message: dict) -> str | None:
    content = message.get("content")
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "".join(parts) or None
    return str(content)


def tool_results(messages: list[dict]) -> dict[str, dict]:
    """Latest result payload for each tool name already in the transcript."""
    id_to_name: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            id_to_name[str(call.get("id"))] = function.get("name") or ""
    found: dict[str, dict] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        name = id_to_name.get(str(message.get("tool_call_id")))
        if not name:
            continue
        try:
            found[name] = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            found[name] = {"ok": False, "error": "unreadable tool result"}
    return found


def _customer_text(messages: list[dict]) -> str:
    """The customer's words, not the short follow-up note the agent appends."""
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and not content.startswith("The language is saved."):
            parts.append(content)
    return "\n".join(parts).strip() or _last_user_text(messages)


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _first(full_name: str) -> str:
    return full_name.split()[0]


class LiveMuse:
    """POST /v1/chat/completions on the Meta Model API."""

    mode = "live"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client or httpx.Client(timeout=120.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None,
        max_tokens: int,
        reasoning_effort: str | None = None,
        retry_on_length: bool = True,
    ) -> Completion:
        if not self.api_key:
            raise LLMError(
                "META_API_KEY is not set. Export it in your shell, or run with CONVOPLEX_LLM=mock."
            )
        budget = effective_max_tokens(max_tokens, tools)
        attempts: list[ModelCall] = []
        for attempt in (1, 2):
            try:
                completion, record = self._once(
                    messages,
                    tools=tools,
                    max_tokens=budget,
                    attempt=attempt,
                    reasoning_effort=reasoning_effort,
                )
            except LLMError as exc:
                exc.calls = list(attempts) + list(exc.calls or [])
                raise
            attempts.append(record)
            completion.calls = list(attempts)
            if completion.finish_reason != "length":
                return completion
            # A usable tool call or any visible text is enough. Doubling the
            # budget only spends more reasoning tokens on a short reply.
            if completion.tool_calls or (completion.content and completion.content.strip()):
                return completion
            if not retry_on_length:
                return completion
            if attempt == 2:
                raise LLMError(
                    "Muse Spark stopped because max_tokens was used up by reasoning "
                    f"(tried {attempts[0].max_tokens}, then {budget}).",
                    calls=attempts,
                )
            budget *= 2
        raise LLMError("Muse Spark did not return a completion.", calls=attempts)

    def _once(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None,
        max_tokens: int,
        attempt: int,
        reasoning_effort: str | None = None,
    ) -> tuple[Completion, ModelCall]:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        # muse-spark-1.3 always reasons. Chat Completions takes top-level
        # reasoning_effort. "none" is rejected with HTTP 400. "minimal" is the
        # shortest effort the docs allow.
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if tools:
            payload["tools"] = tools
        started = time.perf_counter()
        response = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise LLMError(
                f"Meta Model API returned HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Meta Model API returned an unexpected chat payload.") from exc
        finish = choice.get("finish_reason") or "stop"
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            try:
                arguments = _parse_arguments(function.get("arguments"))
            except (ValueError, json.JSONDecodeError):
                arguments = {"_invalid_json": function.get("arguments")}
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{len(calls) + 1}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        content = _message_text(message)
        record = ModelCall(
            latency_ms=latency_ms,
            usage=usage_from_body(body),
            finish_reason=finish,
            max_tokens=max_tokens,
            attempt=attempt,
        )
        return Completion(content=content, tool_calls=calls, finish_reason=finish, calls=[record]), record


class MockMuse:
    """Scripted tool use. Same message shape as the live API, no network."""

    mode = "mock"

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None,
        max_tokens: int,
        reasoning_effort: str | None = None,
        retry_on_length: bool = True,
    ) -> Completion:
        context = parse_context(messages)
        results = tool_results(messages)
        if context.get("task") == "reply":
            completion = self._reply(messages, context, results)
        else:
            completion = self._winback(context, results)
        if not completion.calls:
            completion.calls = [
                ModelCall(
                    latency_ms=0,
                    usage=empty_usage(),
                    finish_reason=completion.finish_reason,
                    max_tokens=effective_max_tokens(max_tokens, tools),
                    attempt=1,
                )
            ]
        return completion

    def _winback(self, context: dict, results: dict[str, dict]) -> Completion:
        favourite = context.get("favourite_product") or {}
        product_name = favourite.get("name") or "rice"
        if "check_stock" not in results and "get_price" not in results:
            return _calls(
                results,
                [
                    ("check_stock", {"product": product_name, "retailer_id": context["retailer_id"]}),
                    ("get_price", {"product": product_name, "retailer_id": context["retailer_id"]}),
                ],
            )
        if "create_offer" not in results:
            if not _favourite_in_stock(context, results):
                price = results.get("get_price") or {}
                name = price.get("name") or product_name
                return Completion(content=out_of_stock_message(context, name), finish_reason="stop")
            price = results.get("get_price") or {}
            product_id = price.get("product_id") or favourite.get("product_id")
            return _calls(
                results,
                [
                    (
                        "create_offer",
                        {
                            "customer_id": context["customer_id"],
                            "product_id": product_id,
                            "discount_percent": 10,
                        },
                    )
                ],
            )
        offer = results.get("create_offer") or {}
        if not offer.get("ok"):
            return Completion(content=out_of_stock_message(context, product_name), finish_reason="stop")
        return Completion(content=winback_message(context, offer), finish_reason="stop")

    def _reply(self, messages: list[dict], context: dict, results: dict[str, dict]) -> Completion:
        from convoplex.languages import CONFIRM, requested_language

        user_text = _customer_text(messages)
        if "set_language" in results and results["set_language"].get("ok"):
            from convoplex.languages import message_asks_something_else

            code = results["set_language"]["language"]
            confirmation = results["set_language"].get("confirmation") or CONFIRM[code]
            if message_asks_something_else(user_text):
                spoken = {**context, "language": code}
                answer = question_message(spoken, "rice", 78000, 12)
                return Completion(
                    content=f"{confirmation} {answer}\nINTENT: question",
                    finish_reason="stop",
                )
            return Completion(content=f"{confirmation}\nINTENT: question", finish_reason="stop")
        requested = requested_language(user_text)
        if requested and requested != context.get("language"):
            return _calls(
                results,
                [("set_language", {"customer_id": context["customer_id"], "language": requested})],
            )
        intent = classify_reply(user_text)
        offer = context.get("open_offer")
        user_text = _last_user_text(messages)
        if intent == "accept" and offer and _known(context, results, "get_delivery_options") is None:
            return _calls(
                results,
                [
                    (
                        "get_delivery_options",
                        {
                            "retailer_id": context["retailer_id"],
                            "customer_id": context["customer_id"],
                        },
                    )
                ],
            )
        if intent == "accept" and offer and "place_order" not in results:
            stock = int(offer.get("stock_qty") or 1)
            qty = _qty_from_text(user_text, context, stock)
            return _calls(
                results,
                [
                    (
                        "place_order",
                        {
                            "customer_id": context["customer_id"],
                            "product_id": offer["product_id"],
                            "qty": qty,
                            "offer_id": offer["offer_id"],
                        },
                    )
                ],
            )
        if intent == "accept" and offer and "place_order" in results:
            order = results.get("place_order") or {}
            delivery = _known(context, results, "get_delivery_options")
            if order.get("ok"):
                text = accept_message(context, order, delivery)
            else:
                text = f"We could not place that order: {order.get('error', 'please try again.')}"
            return Completion(content=f"{text}\nINTENT: accept", finish_reason="stop")
        if intent == "accept" and not offer:
            code = context.get("language") or "en"
            if code == "pcm":
                text = (
                    f"{_first(context['full_name'])}, we never get open offer to confirm yet. "
                    "Tell us wetin you want, make we check the shop."
                )
            elif code == "ig":
                text = (
                    f"{_first(context['full_name'])}, onweghị onyinye mepere emepe iji kwenye ugbu a. "
                    "Gwa anyị ihe ị chọrọ, ka anyị lelee ụlọ ahịa."
                )
            elif code == "yo":
                text = (
                    f"{_first(context['full_name'])}, kò sí ìfilọ̀ ṣíṣí tí a lè jẹ́rìí sí báyìí. "
                    "Sọ ohun tí o fẹ́, kí a wo ṣọ́ọ̀bù."
                )
            elif code == "ha":
                text = (
                    f"{_first(context['full_name'])}, babu tayin da ke buɗe da za mu tabbatar yanzu. "
                    "Faɗa mana abin da kake so, mu duba shago."
                )
            else:
                text = (
                    f"{_first(context['full_name'])}, there is no open offer to confirm yet. "
                    "Tell us what you would like and we will check the shelf."
                )
            return Completion(content=f"{text}\nINTENT: question", finish_reason="stop")
        if intent == "decline":
            return Completion(content=f"{decline_message(context)}\nINTENT: decline", finish_reason="stop")
        price = _known(context, results, "get_price")
        stock = _known(context, results, "check_stock")
        if price is None and stock is None and not offer:
            product_name = (context.get("favourite_product") or {}).get("name") or "rice"
            return _calls(
                results,
                [
                    (
                        "get_price",
                        {
                            "product": product_name,
                            "retailer_id": context["retailer_id"],
                            "customer_id": context["customer_id"],
                        },
                    ),
                    ("check_stock", {"product": product_name, "retailer_id": context["retailer_id"]}),
                ],
            )
        price = price or {}
        stock = stock or {}
        product_name = price.get("name") or (offer or {}).get("product_name") or "that item"
        quoted = price.get("quote_price_ngn", price.get("price_ngn"))
        if offer and offer.get("offer_price_ngn") is not None:
            quoted = offer["offer_price_ngn"]
            product_name = offer.get("product_name") or product_name
        stock_qty = price.get("stock_qty")
        if offer and offer.get("stock_qty") is not None:
            stock_qty = offer["stock_qty"]
        if stock.get("matches"):
            stock_qty = stock["matches"][0].get("stock_qty", stock_qty)
        text = question_message(context, product_name, quoted, stock_qty)
        return Completion(content=f"{text}\nINTENT: question", finish_reason="stop")


def _known(context: dict, results: dict[str, dict], name: str) -> dict | None:
    if name in results:
        return results[name]
    prior = context.get("prior_tool_results") or {}
    found = prior.get(name)
    return found if isinstance(found, dict) else None


def _qty_from_text(text: str, context: dict, stock: int) -> int:
    numbers = [int(number) for number in re.findall(r"\d+", text)]
    requested = numbers[-1] if numbers else int(context.get("suggested_qty") or 1)
    if stock > 0:
        return max(1, min(requested, stock))
    return max(1, requested)


def _favourite_in_stock(context: dict, results: dict[str, dict]) -> bool:
    price = results.get("get_price") or {}
    if not price.get("ok"):
        return False
    favourite_id = (context.get("favourite_product") or {}).get("product_id")
    stock = results.get("check_stock") or {}
    matches = stock.get("matches") or []
    if matches:
        chosen = next((item for item in matches if item.get("product_id") == favourite_id), matches[0])
        return bool(chosen.get("in_stock"))
    return bool(price.get("in_stock"))


def _calls(results: dict[str, dict], calls: list[tuple[str, dict]]) -> Completion:
    start = len(results)
    tool_calls = [
        ToolCall(id=f"call_mock_{start + offset + 1}", name=name, arguments=arguments)
        for offset, (name, arguments) in enumerate(calls)
    ]
    return Completion(content=None, tool_calls=tool_calls, finish_reason="tool_calls")
