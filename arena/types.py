"""Small shared types used across games, players and providers.

Core modules (games, engine, players, ratings) use stdlib dataclasses only, so they stay
importable and testable without third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeGuard

JSON = dict[str, Any]
Action = JSON  # every action is a JSON-serializable dict, e.g. {"column": 3}


@dataclass(frozen=True)
class ActionSpec:
    """The tool an LLM must call to make a move. `input_schema` is JSON Schema."""

    name: str
    description: str
    input_schema: JSON


@dataclass(frozen=True)
class Usage:
    """Token counts for one or more calls. `input_tokens` is UNCACHED input only;
    cached reads/writes are counted separately (providers normalise to this).
    `thinking_tokens` is the part of `output_tokens` spent on reasoning: a breakdown, already
    billed as output, so it must never be added to a cost."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            thinking_tokens=self.thinking_tokens + other.thinking_tokens,
        )


def is_int(x: object) -> TypeGuard[int]:
    """An int that is not a bool (JSON true/false must not pass as 0/1)."""
    return isinstance(x, int) and not isinstance(x, bool)
