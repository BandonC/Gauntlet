"""Anthropic Messages API adapter. One call per complete(); the SDK's own retries are
disabled so RetryingProvider is the single retry layer."""

from __future__ import annotations

import json

import anthropic
import httpx2 as httpx  # the SDKs ship their HTTP client as `httpx2`

from arena.providers.base import Completion, CompletionRequest, ProviderError
from arena.types import Usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,  # injectable for tests
    ):
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=0, timeout=timeout, http_client=http_client
        )

    async def complete(self, req: CompletionRequest) -> Completion:
        system_block: dict = {"type": "text", "text": req.system}
        if req.cache_system:
            # Caching only kicks in above a model-specific minimum prompt length;
            # below it this is a harmless no-op. Check cache_read_tokens in results.
            system_block["cache_control"] = {"type": "ephemeral"}

        kwargs: dict = dict(
            model=req.model,
            max_tokens=req.max_tokens,
            system=[system_block],
            messages=[{"role": "user", "content": req.user}],
            tools=[
                {
                    "name": req.tool.name,
                    "description": req.tool.description,
                    "input_schema": req.tool.input_schema,
                }
            ],
            tool_choice={"type": "tool", "name": req.tool.name},
        )
        # req.temperature is intentionally not sent: the current Anthropic SDK's
        # messages.create() has no sampling parameters. Keep temperature in player
        # config anyway (it's part of identity for providers that accept it).

        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise ProviderError(str(e), retryable=True, retry_after=_retry_after(e)) from e
        except anthropic.APIStatusError as e:
            retryable = e.status_code >= 500 or e.status_code == 529
            raise ProviderError(str(e), retryable=retryable, retry_after=_retry_after(e)) from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise ProviderError(str(e), retryable=True) from e

        tool_input = next((b.input for b in resp.content if b.type == "tool_use"), None)
        raw = (
            json.dumps(tool_input)
            if tool_input is not None
            else "".join(getattr(b, "text", "") for b in resp.content)
        )
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return Completion(tool_input=tool_input, raw_text=raw, usage=usage)


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        value = e.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, ValueError):
        return None
