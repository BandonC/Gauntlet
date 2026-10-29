"""LLM-backed player: renders the versioned prompt, makes one provider call, returns
the tool input as the action. Validation and retry-with-feedback happen in the match
runner, not here."""

from __future__ import annotations

import hashlib
import json
import time

from arena.players.base import Decision, TurnContext
from arena.players.prompting import PromptTemplate
from arena.providers.base import CompletionRequest, Provider


def identity_hash(
    provider: str,
    model: str,
    prompt: PromptTemplate,
    temperature: float | None,
    max_tokens: int,
) -> str:
    cfg = {
        "provider": provider,
        "model": model,
        "prompt": prompt.key,
        "prompt_hash": prompt.content_hash,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


class LLMPlayer:
    def __init__(
        self,
        player_id: str,
        provider: Provider,
        model: str,
        prompt: PromptTemplate,
        *,
        temperature: float | None = 0.0,
        max_tokens: int = 256,
    ):
        self.player_id = player_id
        self.provider = provider
        self.model = model
        self.prompt = prompt
        self.temperature = temperature
        self.max_tokens = max_tokens

    @property
    def config_hash(self) -> str:
        """Identity of this player's behaviour. Same hash = comparable ratings."""
        return identity_hash(
            self.provider.name, self.model, self.prompt, self.temperature, self.max_tokens
        )

    async def act(self, ctx: TurnContext) -> Decision:
        req = CompletionRequest(
            model=self.model,
            system=self.prompt.render_system(ctx),
            user=self.prompt.render_turn(ctx),
            tool=ctx.action_spec,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        start = time.perf_counter()
        completion = await self.provider.complete(req)  # ProviderError propagates
        latency_ms = int((time.perf_counter() - start) * 1000)
        return Decision(
            action=completion.tool_input,
            raw=completion.raw_text,
            usage=completion.usage,
            latency_ms=latency_ms,
        )
