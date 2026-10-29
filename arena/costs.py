"""Cost accounting. Prices come from the roster file (roster.toml), never hard-coded:
provider prices change, and a wrong constant buried in code is worse than a visible
config value you have to check."""

from __future__ import annotations

from dataclasses import dataclass

from arena.types import Usage


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens. Cache prices default to the input price (an
    over-estimate), so missing config never under-counts spend."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None

    def cost(self, usage: Usage) -> float:
        cr = (
            self.cache_read_per_mtok
            if self.cache_read_per_mtok is not None
            else self.input_per_mtok
        )
        cw = (
            self.cache_write_per_mtok
            if self.cache_write_per_mtok is not None
            else self.input_per_mtok
        )
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * cr
            + usage.cache_write_tokens * cw
        ) / 1_000_000


FREE = Pricing(0.0, 0.0)


class BudgetExceeded(Exception):
    pass


class Budget:
    """In-process spend cap. Phase 2 moves this to tournaments.spent_usd in Postgres so
    the cap holds across workers; keep this class for CLI runs and tests."""

    def __init__(self, cap_usd: float | None):
        self.cap_usd = cap_usd
        self.spent_usd = 0.0

    def charge(self, usd: float) -> None:
        self.spent_usd += usd
        if self.cap_usd is not None and self.spent_usd > self.cap_usd:
            raise BudgetExceeded(f"spent ${self.spent_usd:.4f} > cap ${self.cap_usd:.2f}")
