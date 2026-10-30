import asyncio

import pytest

from arena.costs import Budget, BudgetExceeded, Pricing
from arena.engine import InMemoryTurnStore, LeaseLost, MatchRunner, MatchSpec, TurnRecord
from arena.engine.match_runner import MatchInterrupted
from arena.games.connect4 import Connect4
from arena.players.base import Decision, TurnContext
from arena.players.bots import RandomPlayer, ScriptedPlayer
from arena.types import Usage

G = Connect4()


def spec(mid: str = "m1") -> MatchSpec:
    return MatchSpec(match_id=mid, game="connect4", seed=1, seats=["a", "b"])


async def test_random_vs_random_completes():
    store = InMemoryTurnStore()
    out = await MatchRunner(G, [RandomPlayer("a", 1), RandomPlayer("b", 2)], store).run(spec())
    assert sum(out.scores) == 1.0
    assert [t.turn_no for t in out.turns] == list(range(out.num_turns))
    assert out.forfeits == []


async def test_illegal_then_legal_gets_feedback():
    p0 = ScriptedPlayer([{"column": 9}, {"column": 0}, {"column": 0}, {"column": 0}, {"column": 0}])
    p1 = ScriptedPlayer([{"column": 1}] * 3)
    out = await MatchRunner(G, [p0, p1], InMemoryTurnStore()).run(spec())
    first = out.turns[0]
    assert first.attempts == 2 and not first.forfeited
    assert p0.seen[1].previous_error == "column 9 is out of range 0-6"
    assert out.scores == [1.0, 0.0]


async def test_forfeit_after_max_attempts():
    p0 = ScriptedPlayer([None, {"column": 99}, {"bad": 1}])
    p1 = ScriptedPlayer([])
    out = await MatchRunner(G, [p0, p1], InMemoryTurnStore(), max_attempts=3).run(spec())
    assert out.forfeits == [0] and out.scores == [0.0, 1.0] and out.placements == [2, 1]
    assert out.turns[0].attempts == 3 and out.turns[0].action is None


class CrashingStore(InMemoryTurnStore):
    """Simulates a worker losing its lease after N successful writes."""

    def __init__(self, inner: InMemoryTurnStore, crash_after: int):
        self.turns = inner.turns
        self._left = crash_after

    async def append_turn(self, match_id, record):
        if self._left == 0:
            raise LeaseLost("simulated crash")
        self._left -= 1
        await super().append_turn(match_id, record)


async def test_resume_after_crash_matches_uninterrupted_run():
    # Uninterrupted reference run
    ref = await MatchRunner(
        G, [RandomPlayer("a", 7), RandomPlayer("b", 8)], InMemoryTurnStore()
    ).run(spec())

    # Worker 1 dies after 5 turns
    shared = InMemoryTurnStore()
    with pytest.raises(LeaseLost):
        await MatchRunner(
            G, [RandomPlayer("a", 7), RandomPlayer("b", 8)], CrashingStore(shared, 5)
        ).run(spec())
    assert len(shared.turns["m1"]) == 5

    # Worker 2 resumes from the persisted log with freshly built players.
    out = await MatchRunner(G, [RandomPlayer("a", 7), RandomPlayer("b", 8)], shared).run(spec())

    assert [t.turn_no for t in out.turns] == list(range(out.num_turns))
    assert [t.action for t in out.turns] == [t.action for t in ref.turns]
    assert out.scores == ref.scores


async def test_resume_never_replays_paid_turns():
    store = InMemoryTurnStore()
    calls = 0

    class Counting(RandomPlayer):
        async def act(self, ctx):
            nonlocal calls
            calls += 1
            return await super().act(ctx)

    with pytest.raises(LeaseLost):
        await MatchRunner(G, [Counting("a", 1), Counting("b", 2)], CrashingStore(store, 4)).run(
            spec()
        )
    calls_before = calls
    out = await MatchRunner(G, [Counting("a", 1), Counting("b", 2)], store).run(spec())
    # 4 persisted turns + 1 unpersisted (crashed) turn were played in run 1;
    # run 2 plays only the remaining turns.
    assert calls_before == 5
    assert calls - calls_before == out.num_turns - 4


async def test_costs_and_budget():
    class Paying:
        def __init__(self, pid, col):
            self.player_id, self.col = pid, col

        async def act(self, ctx: TurnContext) -> Decision:
            return Decision({"column": self.col}, usage=Usage(input_tokens=1000, output_tokens=100))

    price = Pricing(input_per_mtok=1.0, output_per_mtok=5.0)  # $0.0015 per turn
    out = await MatchRunner(
        G, [Paying("a", 0), Paying("b", 1)], InMemoryTurnStore(), pricing=[price, price]
    ).run(spec())
    assert out.cost_usd == pytest.approx(0.0015 * out.num_turns)

    with pytest.raises(BudgetExceeded):
        await MatchRunner(
            G,
            [Paying("a", 0), Paying("b", 1)],
            InMemoryTurnStore(),
            pricing=[price, price],
            budget=Budget(cap_usd=0.004),
        ).run(spec("m2"))


def test_replay_rejects_corrupt_log():
    from arena.engine.match_runner import CorruptTurnLog

    bad = [TurnRecord(0, 1, {"column": 0}, False, 1, None, None, Usage(), 0.0, 0)]
    runner = MatchRunner(G, [RandomPlayer(), RandomPlayer()], InMemoryTurnStore())
    with pytest.raises(CorruptTurnLog):
        runner.replay(spec(), bad)


async def test_stop_finishes_the_turn_in_flight_then_resumes_exactly():
    stop = asyncio.Event()

    class StopsMidTurn(RandomPlayer):
        async def act(self, ctx: TurnContext) -> Decision:
            if ctx.view["move_number"] == 3:
                stop.set()  # arrives while turn 2 is being decided
            return await super().act(ctx)

    store = InMemoryTurnStore()
    players = [StopsMidTurn("a", 1), StopsMidTurn("b", 2)]
    with pytest.raises(MatchInterrupted):
        await MatchRunner(G, players, store).run(spec(), stop)
    assert [t.turn_no for t in store.turns["m1"]] == [0, 1, 2]  # turn 2 still persisted

    resumed = await MatchRunner(G, [RandomPlayer("a", 1), RandomPlayer("b", 2)], store).run(spec())
    straight = await MatchRunner(
        G, [RandomPlayer("a", 1), RandomPlayer("b", 2)], InMemoryTurnStore()
    ).run(spec())
    assert [t.action for t in resumed.turns] == [t.action for t in straight.turns]
