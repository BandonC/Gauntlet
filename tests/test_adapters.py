"""Vendor adapters against canned HTTP responses (no network, no tokens).
If a vendor changes its response shape, these are the tests that should break."""

import json
from dataclasses import replace

import httpx2 as httpx
import pytest

from arena.providers.anthropic_provider import AnthropicProvider
from arena.providers.base import CompletionRequest, ProviderError
from arena.providers.openai_provider import OpenAIProvider
from arena.types import ActionSpec

TOOL = ActionSpec("drop_piece", "Drop a piece", {"type": "object", "properties": {}})
REQ = CompletionRequest(model="m", system="sys", user="board", tool=TOOL)


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_anthropic_tool_use_parsed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "drop_piece", "input": {"column": 3}}
                ],
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 15,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    out = await AnthropicProvider(api_key="k", http_client=client(handler)).complete(REQ)
    assert out.tool_input == {"column": 3}
    assert (out.usage.input_tokens, out.usage.cache_read_tokens) == (120, 900)
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": "drop_piece"}
    assert seen["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}


def anthropic_reply(content, **usage):
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "m",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 300, **usage},
        },
    )


TOOL_USE = {"type": "tool_use", "id": "t", "name": "drop_piece", "input": {"column": 3}}
THINKING = {"type": "thinking", "thinking": "summary", "signature": "sig"}


async def send(req, content, **usage):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return anthropic_reply(content, **usage)

    out = await AnthropicProvider(api_key="k", http_client=client(handler)).complete(req)
    return seen, out


async def test_anthropic_sends_no_thinking_setting_by_default():
    seen, _ = await send(REQ, [TOOL_USE])
    assert "thinking" not in seen


async def test_anthropic_auto_allows_one_tool_call_and_forces_none():
    seen, out = await send(replace(REQ, tool_choice="auto"), [TOOL_USE])
    assert seen["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert out.tool_input == {"column": 3}


async def test_anthropic_auto_reply_in_text_is_no_action_not_an_error():
    _, out = await send(replace(REQ, tool_choice="auto"), [{"type": "text", "text": "column 3"}])
    assert out.tool_input is None and out.raw_text == "column 3"


async def test_anthropic_thinking_budget_is_sent():
    req = replace(REQ, tool_choice="auto", thinking_budget=1024, max_tokens=2048)
    seen, _ = await send(req, [TOOL_USE])
    assert seen["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert seen["max_tokens"] == 2048


async def test_anthropic_thinking_tokens_are_recorded_inside_output_tokens():
    details = {"thinking_tokens": 280}
    _, out = await send(REQ, [THINKING, TOOL_USE], output_tokens_details=details)
    assert (out.usage.output_tokens, out.usage.thinking_tokens) == (300, 280)
    assert out.tool_input == {"column": 3} and out.raw_text == '{"column": 3}'


async def test_anthropic_reply_without_a_breakdown_records_no_thinking_tokens():
    _, out = await send(REQ, [TOOL_USE])
    assert out.usage.thinking_tokens == 0


async def test_anthropic_reply_of_only_thinking_is_no_action():
    _, out = await send(replace(REQ, tool_choice="auto"), [THINKING])
    assert out.tool_input is None and out.raw_text == ""


async def test_anthropic_between_tools_is_sent_and_nothing_else_about_thinking():
    seen, _ = await send(
        replace(REQ, tool_choice="auto", thinking_mode="between_tools"), [TOOL_USE]
    )
    assert seen["thinking"] == {"type": "between_tools"} and "output_config" not in seen


async def test_anthropic_effort_is_sent_in_output_config():
    seen, _ = await send(replace(REQ, effort="low"), [TOOL_USE])
    assert seen["output_config"] == {"effort": "low"} and "thinking" not in seen


@pytest.mark.parametrize("reason", ["tool_use", "max_tokens", "end_turn", "refusal"])
async def test_anthropic_stop_reason_is_kept_as_the_api_said_it(reason):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        reply = anthropic_reply([{"type": "text", "text": "x"}])
        body = json.loads(reply.content) | {"stop_reason": reason}
        seen["sent"] = request
        return httpx.Response(200, json=body)

    out = await AnthropicProvider(api_key="k", http_client=client(handler)).complete(REQ)
    assert out.stop_reason == reason


async def test_anthropic_429_is_retryable_with_retry_after():
    def handler(request):
        return httpx.Response(
            429,
            headers={"retry-after": "7"},
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
        )

    with pytest.raises(ProviderError) as ei:
        await AnthropicProvider(api_key="k", http_client=client(handler)).complete(REQ)
    assert ei.value.retryable and ei.value.retry_after == 7.0


async def test_anthropic_400_not_retryable():
    def handler(request):
        return httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}
        )

    with pytest.raises(ProviderError) as ei:
        await AnthropicProvider(api_key="k", http_client=client(handler)).complete(REQ)
    assert not ei.value.retryable


async def test_openai_function_call_parsed():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {
                                        "name": "drop_piece",
                                        "arguments": '{"column": 5}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 12,
                    "total_tokens": 1012,
                    "prompt_tokens_details": {"cached_tokens": 768},
                },
            },
        )

    out = await OpenAIProvider(api_key="k", http_client=client(handler)).complete(REQ)
    assert out.tool_input == {"column": 5}
    assert (out.usage.input_tokens, out.usage.cache_read_tokens) == (232, 768)


async def test_openai_garbage_arguments_become_no_action():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {"name": "drop_piece", "arguments": "{col"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    out = await OpenAIProvider(api_key="k", http_client=client(handler)).complete(REQ)
    assert out.tool_input is None and out.raw_text == "{col"
