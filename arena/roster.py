"""Loads player definitions from roster.toml and builds Player objects.

Prices live here (per player entry) so cost tracking never depends on constants
buried in code. Check them against the provider pricing pages before real runs.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from arena.costs import FREE, Pricing
from arena.players.base import Player
from arena.players.bots import RandomPlayer, build_bot
from arena.players.llm import LLMPlayer, identity_hash
from arena.players.prompting import load_prompt
from arena.providers.base import Provider
from arena.providers.mock import RecordingProvider
from arena.providers.ratelimit import RateLimiter, TokenSource
from arena.providers.retry import RetryingProvider


@dataclass(frozen=True)
class PlayerConfig:
    id: str
    provider: str  # "anthropic" | "openai" | "random" | "bot" (model = the bot's game)
    model: str = ""
    prompt: str = ""
    temperature: float | None = 0.0
    max_tokens: int = 256
    pricing: Pricing = FREE
    seed: int = 0
    tool_choice: str = "forced"  # "auto" for models that reject forced tool use
    thinking_budget: int | None = None  # tokens of manual thinking, Anthropic only
    thinking_mode: str = "default"  # "between_tools": no up-front thinking, Anthropic only
    effort: str | None = None  # output_config.effort, Anthropic only

    def __post_init__(self) -> None:
        """Refuse here what the API would refuse later, in the middle of a tournament."""
        if self.tool_choice not in ("forced", "auto"):
            raise ValueError(f"{self.id}: tool_choice must be forced or auto")
        if self.thinking_mode not in ("default", "between_tools"):
            raise ValueError(f"{self.id}: thinking_mode must be default or between_tools")
        if self.effort not in (None, "low", "medium", "high", "xhigh", "max"):
            raise ValueError(f"{self.id}: effort must be low, medium, high, xhigh or max")
        customised = (
            self.tool_choice != "forced"
            or self.thinking_budget is not None
            or self.thinking_mode != "default"
            or self.effort is not None
        )
        if customised and self.provider != "anthropic":
            raise ValueError(
                f"{self.id}: tool_choice, thinking and effort settings are Anthropic only"
            )
        if self.thinking_mode == "between_tools":
            if self.tool_choice == "forced":
                raise ValueError(f"{self.id}: between_tools needs tool_choice = auto")
            if self.thinking_budget is not None:
                raise ValueError(f"{self.id}: between_tools can't take a thinking_budget")
            if self.effort in ("xhigh", "max"):
                raise ValueError(f"{self.id}: between_tools only works at effort high or below")
        if self.thinking_budget is None:
            return
        if self.tool_choice == "forced":
            raise ValueError(f"{self.id}: thinking_budget needs tool_choice = auto")
        if not 1024 <= self.thinking_budget < self.max_tokens:
            raise ValueError(f"{self.id}: thinking_budget must be 1024 or more, below max_tokens")


@dataclass(frozen=True)
class ProviderLimits:
    requests_per_minute: float | None = None
    max_concurrency: int | None = None


@dataclass(frozen=True)
class Roster:
    players: dict[str, PlayerConfig]
    limits: dict[str, ProviderLimits]


def load_roster(path: str | Path) -> Roster:
    data = tomllib.loads(Path(path).read_text())
    players: dict[str, PlayerConfig] = {}
    for p in data.get("players", []):
        if p.get("temperature") == "omit":  # TOML has no null; send no temperature at all
            p["temperature"] = None
        pricing = Pricing(
            input_per_mtok=p.pop("price_input_per_mtok", 0.0),
            output_per_mtok=p.pop("price_output_per_mtok", 0.0),
            cache_read_per_mtok=p.pop("price_cache_read_per_mtok", None),
            cache_write_per_mtok=p.pop("price_cache_write_per_mtok", None),
        )
        cfg = PlayerConfig(pricing=pricing, **p)
        if cfg.id in players:
            raise ValueError(f"duplicate player id {cfg.id!r} in {path}")
        players[cfg.id] = cfg
    limits = {name: ProviderLimits(**v) for name, v in data.get("providers", {}).items()}
    return Roster(players=players, limits=limits)


def config_hash(cfg: PlayerConfig) -> str:
    """Stable identity for the players table (no API key or provider object needed)."""
    if cfg.provider == "random":
        return f"random-{cfg.seed}"
    if cfg.provider == "bot":
        return f"bot-{cfg.model}-{cfg.seed}"
    return identity_hash(
        cfg.provider,
        cfg.model,
        load_prompt(cfg.prompt),
        cfg.temperature,
        cfg.max_tokens,
        cfg.tool_choice,
        cfg.thinking_budget,
        cfg.thinking_mode,
        cfg.effort,
    )


class PlayerFactory:
    """Builds players, sharing ONE rate-limited, retrying provider per vendor."""

    def __init__(
        self,
        roster: Roster,
        api_keys: dict[str, str | None],
        token_sources: dict[str, TokenSource] | None = None,
        record_dir: Path | None = None,
    ):
        self.roster = roster
        self._api_keys = api_keys
        self._token_sources = token_sources or {}
        # When set, every completion from each vendor adapter is appended to
        # record_dir/<vendor>-<timestamp>.jsonl (RecordingProvider), for test fixtures.
        self._record_dir = record_dir
        self._providers: dict[str, Provider] = {}

    def with_token_sources(self, token_sources: dict[str, TokenSource]) -> PlayerFactory:
        """Same roster and keys, with shared (e.g. Postgres) request-rate buckets."""
        return PlayerFactory(self.roster, self._api_keys, token_sources, self._record_dir)

    def with_record_dir(self, record_dir: Path) -> PlayerFactory:
        """Same roster, keys and rate sources, recording every completion to record_dir."""
        return PlayerFactory(self.roster, self._api_keys, self._token_sources, record_dir)

    def _provider(self, name: str) -> Provider:
        if name not in self._providers:
            lim = self.roster.limits.get(name, ProviderLimits())
            limiter = RateLimiter(
                lim.requests_per_minute,
                lim.max_concurrency,
                token_source=self._token_sources.get(name),
            )
            if name == "anthropic":
                from arena.providers.anthropic_provider import AnthropicProvider

                inner: Provider = AnthropicProvider(api_key=self._api_keys.get("anthropic"))
            elif name == "openai":
                from arena.providers.openai_provider import OpenAIProvider

                inner = OpenAIProvider(api_key=self._api_keys.get("openai"))
            else:
                raise ValueError(f"unknown provider {name!r}")
            if self._record_dir is not None:
                self._record_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                inner = RecordingProvider(inner, self._record_dir / f"{name}-{stamp}.jsonl")
            self._providers[name] = RetryingProvider(inner, limiter=limiter)
        return self._providers[name]

    def build(self, player_id: str) -> tuple[Player, Pricing]:
        cfg = self.roster.players.get(player_id)
        if cfg is None:
            raise ValueError(f"player {player_id!r} not in roster")
        if cfg.provider == "random":
            return RandomPlayer(cfg.id, cfg.seed), FREE
        if cfg.provider == "bot":
            return build_bot(cfg.model, cfg.id, cfg.seed), FREE
        player = LLMPlayer(
            cfg.id,
            self._provider(cfg.provider),
            cfg.model,
            load_prompt(cfg.prompt),
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            tool_choice=cfg.tool_choice,
            thinking_budget=cfg.thinking_budget,
            thinking_mode=cfg.thinking_mode,
            effort=cfg.effort,
        )
        return player, cfg.pricing
