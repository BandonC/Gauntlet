"""Runs one match turn by turn, persisting every turn before moving on.

Resumability: a match's state is never stored, only its turn log. `run()` loads the
log, rebuilds state by replaying it through the (deterministic) game engine, and
continues from there. So a worker can die at any point and another worker picks up
exactly where it stopped, without re-paying for turns already played.

Fencing: `TurnStore.append_turn` must refuse writes from a worker that no longer holds
the match lease (Postgres version: a conditional INSERT; see arena/store/turns.py).
When it refuses, it raises LeaseLost and this runner stops immediately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from arena.costs import FREE, Budget, Pricing
from arena.games.base import Game, State
from arena.players.base import Player, TurnContext
from arena.types import JSON, Action, Usage


class LeaseLost(Exception):
    """Another worker owns this match now. Stop without writing anything else."""


class CorruptTurnLog(Exception):
    pass


class MatchTooLong(Exception):
    pass


class MatchInterrupted(Exception):
    """A stop was requested between turns. Every played turn is persisted, so the
    match resumes from its log like any other."""


@dataclass(frozen=True)
class MatchSpec:
    match_id: str
    game: str
    seed: int
    seats: list[str]  # player ids in seat order
    options: JSON | None = None  # matches.options, passed to Game.initial_state


@dataclass(frozen=True)
class TurnRecord:
    turn_no: int
    seat: int
    action: Action | None  # None when the seat forfeited
    forfeited: bool
    attempts: int  # 1 = first try was legal
    raw: str | None
    error: str | None  # last rejection reason, if any attempt was rejected
    usage: Usage
    cost_usd: float
    latency_ms: int
    truncated: bool = False  # the engine shortened this action (Game.normalize_action)
    rejections: list[JSON] = field(default_factory=list)  # {attempt, action, reason}, in order
    stop_reasons: list[str | None] = field(default_factory=list)  # one per attempt, in order


@dataclass(frozen=True)
class MatchOutcome:
    match_id: str
    scores: list[float]
    placements: list[int]  # per seat, 1 is best (Game.final_placements)
    num_turns: int
    forfeits: list[int]
    usage_by_seat: list[Usage]
    cost_usd: float
    turns: list[TurnRecord] = field(repr=False)


class TurnStore(Protocol):
    async def load_turns(self, match_id: str) -> list[TurnRecord]: ...

    async def append_turn(self, match_id: str, record: TurnRecord) -> None:
        """Persist one turn. Raise LeaseLost if this writer may no longer write
        (lease expired / reassigned) or if turn_no already exists."""
        ...


class InMemoryTurnStore:
    """TurnStore for tests and CLI runs."""

    def __init__(self) -> None:
        self.turns: dict[str, list[TurnRecord]] = {}

    async def load_turns(self, match_id: str) -> list[TurnRecord]:
        return list(self.turns.get(match_id, []))

    async def append_turn(self, match_id: str, record: TurnRecord) -> None:
        log = self.turns.setdefault(match_id, [])
        if record.turn_no != len(log):
            raise LeaseLost(f"turn {record.turn_no} conflicts with log length {len(log)}")
        log.append(record)


def iter_states(game: Game, spec: MatchSpec, turns: Sequence[TurnRecord]) -> Iterator[State]:
    """Yield the initial state, then the state after each logged turn, re-validating
    every action. Used for resuming matches and for building replays."""
    state = game.initial_state(len(spec.seats), spec.seed, spec.options)
    yield state
    for i, t in enumerate(turns):
        if t.turn_no != i:
            raise CorruptTurnLog(f"{spec.match_id}: expected turn {i}, got {t.turn_no}")
        if game.is_terminal(state) or game.current_player(state) != t.seat:
            raise CorruptTurnLog(f"{spec.match_id}: turn {i} played out of order")
        if t.forfeited:
            state = game.forfeit(state, t.seat)
        else:
            if t.action is None:
                raise CorruptTurnLog(f"{spec.match_id}: turn {i} has no action")
            err = game.validate_action(state, t.seat, t.action)
            if err:
                raise CorruptTurnLog(f"{spec.match_id}: turn {i} is illegal on replay: {err}")
            state = game.apply(state, t.seat, t.action)
        yield state


OnTurn = Callable[[TurnRecord, State], Awaitable[None] | None]


class MatchRunner:
    def __init__(
        self,
        game: Game,
        players: Sequence[Player],
        store: TurnStore,
        *,
        pricing: Sequence[Pricing] | None = None,  # per seat
        max_attempts: int = 3,
        max_turns: int = 1000,
        budget: Budget | None = None,
        on_turn: OnTurn | None = None,
    ):
        if not game.min_players <= len(players) <= game.max_players:
            raise ValueError(f"{game.name} takes {game.min_players}-{game.max_players} players")
        self.game = game
        self.players = list(players)
        self.store = store
        self.pricing = list(pricing) if pricing else [FREE] * len(players)
        self.max_attempts = max_attempts
        self.max_turns = max_turns
        self.budget = budget
        self.on_turn = on_turn

    def replay(self, spec: MatchSpec, turns: Sequence[TurnRecord]) -> State:
        *_, final = iter_states(self.game, spec, turns)
        return final

    async def run(self, spec: MatchSpec, stop: asyncio.Event | None = None) -> MatchOutcome:
        """Play to the end. If `stop` is set, the turn in flight still finishes and is
        persisted, then MatchInterrupted is raised before the next one starts."""
        turns = await self.store.load_turns(spec.match_id)
        state = self.replay(spec, turns)

        while not self.game.is_terminal(state):
            turn_no = len(turns)
            if stop is not None and stop.is_set():
                raise MatchInterrupted(f"{spec.match_id} stopped before turn {turn_no}")
            if turn_no >= self.max_turns:
                raise MatchTooLong(f"{spec.match_id} exceeded {self.max_turns} turns")
            seat = self.game.current_player(state)
            record, next_state = await self._play_turn(state, seat, turn_no)
            # Persist first; only advance in-memory state once the write is accepted.
            await self.store.append_turn(spec.match_id, record)
            turns.append(record)
            state = next_state
            if self.budget:
                self.budget.charge(record.cost_usd)
            if self.on_turn:
                maybe = self.on_turn(record, state)
                if maybe is not None:
                    await maybe

        return self._outcome(spec, state, turns)

    async def _play_turn(self, state: State, seat: int, turn_no: int) -> tuple[TurnRecord, State]:
        player = self.players[seat]
        view = self.game.player_view(state, seat)
        usage = Usage()
        latency = 0
        error: str | None = None
        rejections: list[JSON] = []
        stop_reasons: list[str | None] = []
        raw: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            ctx = TurnContext(
                game_name=self.game.name,
                seat=seat,
                rules=self.game.rules_text(),
                view=view,
                view_text=self.game.render_view(view),
                action_spec=self.game.action_spec(state, seat),
                legal_actions_hint=self.game.legal_actions_hint(state, seat),
                attempt=attempt,
                previous_error=error,
            )
            decision = await player.act(ctx)
            usage += decision.usage
            latency += decision.latency_ms
            raw = decision.raw
            stop_reasons.append(decision.stop_reason)
            if decision.action is None:
                error = "no move was returned; call the tool with a legal move"
            else:
                error = self.game.validate_action(state, seat, decision.action)
            if error is None:
                last_rejection = rejections[-1]["reason"] if rejections else None
                action = self.game.normalize_action(state, seat, decision.action)
                record = self._record(
                    turn_no,
                    seat,
                    action,
                    False,
                    attempt,
                    raw,
                    last_rejection,
                    usage,
                    latency,
                    truncated=action != decision.action,
                    rejections=rejections,
                    stop_reasons=stop_reasons,
                )
                return record, self.game.apply(state, seat, action)
            rejections.append({"attempt": attempt, "action": decision.action, "reason": error})

        # Every attempt was invalid: forfeit. (Infra errors never reach here; they raise.)
        record = self._record(
            turn_no,
            seat,
            None,
            True,
            self.max_attempts,
            raw,
            error,
            usage,
            latency,
            rejections=rejections,
            stop_reasons=stop_reasons,
        )
        return record, self.game.forfeit(state, seat)

    def _record(
        self,
        turn_no: int,
        seat: int,
        action: Action | None,
        forfeited: bool,
        attempts: int,
        raw: str | None,
        error: str | None,
        usage: Usage,
        latency_ms: int,
        truncated: bool = False,
        rejections: list[JSON] | None = None,
        stop_reasons: list[str | None] | None = None,
    ) -> TurnRecord:
        return TurnRecord(
            turn_no=turn_no,
            seat=seat,
            action=action,
            forfeited=forfeited,
            attempts=attempts,
            raw=raw,
            error=error,
            usage=usage,
            cost_usd=self.pricing[seat].cost(usage),
            latency_ms=latency_ms,
            truncated=truncated,
            rejections=rejections or [],
            stop_reasons=stop_reasons or [],
        )

    def _outcome(self, spec: MatchSpec, state: State, turns: list[TurnRecord]) -> MatchOutcome:
        usage_by_seat = [Usage() for _ in spec.seats]
        for t in turns:
            usage_by_seat[t.seat] += t.usage
        forfeits = [t.seat for t in turns if t.forfeited]
        return MatchOutcome(
            match_id=spec.match_id,
            scores=self.game.scores(state),
            placements=self.game.final_placements(state, forfeits),
            num_turns=len(turns),
            forfeits=forfeits,
            usage_by_seat=usage_by_seat,
            cost_usd=sum(t.cost_usd for t in turns),
            turns=turns,
        )
