"""The live client must match the Meta Model API notes, without calling the network."""

import json

import httpx

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback
from convoplex.config import MIN_TOOL_TOKENS, resolve_llm_mode
from convoplex.llm import LLMError, LiveMuse, effective_max_tokens
from convoplex.tools import TOOLS


def test_mode_resolution(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    assert resolve_llm_mode("auto") == "mock"
    monkeypatch.setenv("META_API_KEY", "present")
    assert resolve_llm_mode("auto") == "live"
    assert resolve_llm_mode("mock", api_key="present") == "mock"
    assert resolve_llm_mode("live", api_key=None) == "live"


def test_tool_round_trip_matches_documented_shape(shop, log_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_abc",
                                        "type": "function",
                                        "function": {
                                            "name": "get_price",
                                            "arguments": json.dumps(
                                                {"product": "Mama Gold Rice 50kg", "retailer_id": 1}
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"completion_tokens_details": {"reasoning_tokens": 12}},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Hello Chinedu, the bag is ₦78,000."},
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = LiveMuse(
        api_key="sk-test-secret-value",
        base_url="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        client=client,
    )
    result = draft_winback(shop, ActionLog(log_path), llm, 1, max_tokens=300)
    assert result.message == "Hello Chinedu, the bag is ₦78,000."
    assert seen[0].url == "https://api.meta.ai/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer sk-test-secret-value"
    body = json.loads(seen[0].content)
    assert set(body) == {"model", "messages", "max_tokens", "tools"}
    assert body["model"] == "muse-spark-1.3"
    assert body["max_tokens"] >= MIN_TOOL_TOKENS
    assert body["max_tokens"] == MIN_TOOL_TOKENS
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == [
        "check_stock",
        "get_price",
        "create_offer",
        "place_order",
        "get_delivery_options",
        "set_language",
        "ask_phrase",
        "send_offer_card",
        "learn_phrase",
    ]
    assert body["tools"] == TOOLS
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"

    follow = json.loads(seen[1].content)
    tool_messages = [message for message in follow["messages"] if message["role"] == "tool"]
    assert tool_messages[0]["tool_call_id"] == "call_abc"
    assert "78000" in tool_messages[0]["content"]
    assert "sk-test-secret-value" not in log_path.read_text(encoding="utf-8")


def test_length_finish_retries_once_then_raises():
    sent: list[int] = []

    def length(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length", "message": {"role": "assistant", "content": None}}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": sent[-1],
                    "total_tokens": 10 + sent[-1],
                    "completion_tokens_details": {"reasoning_tokens": sent[-1]},
                },
            },
        )

    llm = LiveMuse(
        api_key="sk-test-secret-value",
        base_url="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        client=httpx.Client(transport=httpx.MockTransport(length)),
    )
    try:
        llm.complete([{"role": "user", "content": "hi"}], tools=TOOLS, max_tokens=5)
    except LLMError as exc:
        assert "max_tokens" in str(exc)
        assert "sk-test-secret-value" not in str(exc)
        assert sent == [MIN_TOOL_TOKENS, MIN_TOOL_TOKENS * 2]
        assert [call.max_tokens for call in exc.calls] == sent
        assert exc.calls[1].usage["reasoning_tokens"] == MIN_TOOL_TOKENS * 2
    else:
        raise AssertionError("expected LLMError")


def test_length_retry_keeps_the_second_completion():
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_tokens"])
        if len(sent) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "length", "message": {"role": "assistant", "content": None}}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Abeg no wahala."},
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 8, "total_tokens": 12},
            },
        )

    llm = LiveMuse(
        api_key="sk-test-secret-value",
        base_url="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    done = llm.complete([{"role": "user", "content": "hi"}], tools=TOOLS, max_tokens=5)
    assert done.content == "Abeg no wahala."
    assert sent == [MIN_TOOL_TOKENS, MIN_TOOL_TOKENS * 2]
    assert len(done.calls) == 2
    assert done.calls[1].usage["total_tokens"] == 12
    assert done.calls[1].latency_ms >= 0

    def denied(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, text="unauthorized")

    llm = LiveMuse(
        api_key="sk-test-secret-value",
        base_url="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        client=httpx.Client(transport=httpx.MockTransport(denied)),
    )
    try:
        llm.complete([{"role": "user", "content": "hi"}], tools=None, max_tokens=300)
    except LLMError as exc:
        assert "401" in str(exc)
        assert "sk-test-secret-value" not in str(exc)
    else:
        raise AssertionError("expected LLMError")


def test_reply_length_falls_back_without_doubling_the_budget():
    """A short WhatsApp reply must not spend a second call at 5000 tokens."""
    sent: list[int] = []

    def length(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["max_tokens"])
        assert body["reasoning_effort"] == "minimal"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length", "message": {"role": "assistant", "content": None}}
                ],
                "usage": {"completion_tokens_details": {"reasoning_tokens": body["max_tokens"]}},
            },
        )

    llm = LiveMuse(
        api_key="sk-test-secret-value",
        base_url="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        client=httpx.Client(transport=httpx.MockTransport(length)),
    )
    done = llm.complete(
        [{"role": "user", "content": "hi"}],
        tools=TOOLS,
        max_tokens=5,
        reasoning_effort="minimal",
        retry_on_length=False,
    )
    assert sent == [MIN_TOOL_TOKENS]
    assert done.finish_reason == "length"
    assert done.content is None
    assert len(done.calls) == 1

    def with_text(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "Zan yi amfani da Hausa."},
                    }
                ]
            },
        )

    llm = LiveMuse(
        api_key="sk-test-secret-value",
        base_url="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        client=httpx.Client(transport=httpx.MockTransport(with_text)),
    )
    kept = llm.complete(
        [{"role": "user", "content": "hi"}],
        tools=TOOLS,
        max_tokens=5,
        retry_on_length=True,
    )
    assert kept.content == "Zan yi amfani da Hausa."
    assert sent == [MIN_TOOL_TOKENS, MIN_TOOL_TOKENS]


def test_max_tokens_floor():
    assert MIN_TOOL_TOKENS == 2500
    assert effective_max_tokens(300, TOOLS) == 2500
    assert effective_max_tokens(1500, TOOLS) == 2500
    assert effective_max_tokens(3000, TOOLS) == 3000
    assert effective_max_tokens(300, None) == 300


def test_live_client_refuses_to_call_without_a_key():
    llm = LiveMuse(api_key=None, base_url="https://api.meta.ai/v1", model="muse-spark-1.3")
    try:
        llm.complete([{"role": "user", "content": "hi"}], tools=None, max_tokens=300)
    except LLMError as exc:
        assert "META_API_KEY" in str(exc)
    else:
        raise AssertionError("expected LLMError")
