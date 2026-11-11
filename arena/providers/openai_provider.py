"""OpenAI Chat Completions adapter (function calling). One call per complete();
SDK retries disabled so RetryingProvider is the single retry layer.

OpenAI caches long prompt prefixes automatically, so `cache_system` needs no flag here;
cached tokens show up in usage.prompt_tokens_details.cached_tokens.
"""

from __future__ import annotations

import json

import httpx2 as httpx  # the SDKs ship their HTTP client as `httpx2`
import openai

from arena.providers.base import (
    Completion,
    CompletionRequest,
    ProviderError,
    retry_after_of,
)
from arena.types import Usage


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,  # injectable for tests
    ):
        self._client = openai.AsyncOpenAI(
            api_key=api_key, max_retries=0, timeout=timeout, http_client=http_client
        )

    async def complete(self, req: CompletionRequest) -> Completion:
        kwargs: dict = dict(
            model=req.model,
            max_completion_tokens=req.max_tokens,
            messages=[
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": req.tool.name,
                        "description": req.tool.description,
                        "parameters": req.tool.input_schema,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": req.tool.name}},
        )
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            raise ProviderError(str(e), retryable=True, retry_after=retry_after_of(e)) from e
        except openai.APIStatusError as e:
            raise ProviderError(
                str(e), retryable=e.status_code >= 500, retry_after=retry_after_of(e)
            ) from e
        except openai.APIConnectionError as e:  # includes timeouts
            raise ProviderError(str(e), retryable=True) from e

        msg = resp.choices[0].message
        tool_input = None
        if msg.tool_calls:
            try:
                tool_input = json.loads(msg.tool_calls[0].function.arguments)
            except (json.JSONDecodeError, AttributeError):
                tool_input = None
        raw = msg.tool_calls[0].function.arguments if msg.tool_calls else (msg.content or "")
        blocks = []
        if msg.content:
            blocks.append({"type": "text", "chars": len(msg.content), "text": msg.content})
        blocks += [
            {"type": "tool_use", "chars": len(call.function.arguments or "")}
            for call in msg.tool_calls or []
        ]

        u = resp.usage
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        usage = Usage(
            input_tokens=(u.prompt_tokens - cached) if u else 0,  # uncached input only
            output_tokens=u.completion_tokens if u else 0,
            cache_read_tokens=cached,
        )
        return Completion(
            tool_input=tool_input,
            raw_text=raw,
            usage=usage,
            stop_reason=resp.choices[0].finish_reason,
            blocks=blocks,
        )
