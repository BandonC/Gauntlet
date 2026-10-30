"""Command line entry point.

arena play connect4 --p1 random --p2 haiku-5.5/c4-v1     one match, in memory, printed
arena migrate                                            apply SQL migrations
arena tournament --game connect4 --players a,b,c --games-per-pair 10 --budget 5
arena worker --concurrency 4                             run matches from the queue
arena leaderboard --game connect4
arena serve                                              API + UI on :8000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from arena.config import settings


def _factory(roster_path: str | None):
    from arena.roster import PlayerFactory, Roster, load_roster

    s = settings()
    path = roster_path or s.arena_roster_path
    try:
        roster = load_roster(path)
    except FileNotFoundError:
        roster = Roster(players={}, limits={})
    keys = {"anthropic": s.anthropic_api_key, "openai": s.openai_api_key}
    return PlayerFactory(roster, keys), path


def _build_player(factory, spec: str):
    """'random' or 'random:SEED' are built in; anything else is a roster id."""
    from arena.costs import FREE
    from arena.players.bots import RandomPlayer

    if spec == "random" or spec.startswith("random:"):
        seed = int(spec.split(":", 1)[1]) if ":" in spec else 0
        return RandomPlayer(spec, seed), FREE
    return factory.build(spec)


async def _play(args: argparse.Namespace) -> None:
    from arena.costs import Budget
    from arena.engine import InMemoryTurnStore, MatchRunner, MatchSpec
    from arena.games.registry import get_game

    game = get_game(args.game)
    factory, _ = _factory(args.roster)
    built = [_build_player(factory, s) for s in (args.p1, args.p2)]

    def show(record, state):
        if args.quiet:
            return
        who = (args.p1, args.p2)[record.seat]
        move = "FORFEIT" if record.forfeited else record.action
        extra = f"  (attempts={record.attempts})" if record.attempts > 1 else ""
        print(f"\nturn {record.turn_no}: {who} -> {move}{extra}  ${record.cost_usd:.5f}")
        print(game.render_view(game.player_view(state, 0)).split("\n\n")[1])

    runner = MatchRunner(
        game,
        [p for p, _ in built],
        InMemoryTurnStore(),
        pricing=[pr for _, pr in built],
        budget=Budget(args.budget),
        on_turn=show,
    )
    out = await runner.run(MatchSpec("cli", args.game, args.seed, [args.p1, args.p2]))
    print(f"\nscores: {dict(zip((args.p1, args.p2), out.scores, strict=True))}")
    print(f"turns: {out.num_turns}  forfeits: {out.forfeits}  cost: ${out.cost_usd:.5f}")
    for name, u in zip((args.p1, args.p2), out.usage_by_seat, strict=True):
        print(
            f"  {name}: in={u.input_tokens} out={u.output_tokens} cache_read={u.cache_read_tokens}"
        )


async def _migrate(args: argparse.Namespace) -> None:
    from arena.store.migrate import migrate

    names = await migrate(settings().database_url)
    print("applied:", ", ".join(names) if names else "nothing (up to date)")


async def _tournament(args: argparse.Namespace) -> None:
    from arena.store.db import create_pool
    from arena.store.tournaments import create_round_robin, create_swiss, resume_tournament

    if args.tournament_cmd == "resume":
        pool = await create_pool(settings().database_url)
        try:
            n = await resume_tournament(pool, args.tournament_id)
        except ValueError as e:
            sys.exit(f"arena tournament resume: {e}")
        finally:
            await pool.close()
        print(f"tournament {args.tournament_id}: running, {n} failed matches re-queued")
        return
    if not args.game or not args.players:
        sys.exit("arena tournament: --game and --players are required")

    factory, path = _factory(args.roster)
    ids = [p.strip() for p in args.players.split(",") if p.strip()]
    missing = [i for i in ids if i not in factory.roster.players]
    if missing:
        sys.exit(f"not in {path}: {missing}")
    cfgs = [factory.roster.players[i] for i in ids]
    pool = await create_pool(settings().database_url)
    try:
        if args.format == "swiss":
            tid, n = await create_swiss(
                pool, args.game, cfgs, args.budget, rounds=args.rounds, seed=args.seed
            )
            print(f"tournament {tid}: swiss, round 1 queued ({n} matches)")
        else:
            tid, n = await create_round_robin(
                pool, args.game, cfgs, args.games_per_pair, args.budget, seed=args.seed
            )
            print(f"tournament {tid}: {n} matches queued")
    finally:
        await pool.close()


async def _worker(args: argparse.Namespace) -> None:
    from arena.store.db import create_pool
    from arena.store.ratelimit import postgres_buckets, seed_rate_limits
    from arena.worker import WorkerConfig, install_stop_signals, run_worker

    s = settings()
    factory, _ = _factory(args.roster)
    cfg = WorkerConfig(
        lease_seconds=s.arena_lease_seconds,
        heartbeat_seconds=s.arena_heartbeat_seconds,
        max_run_attempts=s.arena_max_run_attempts,
    )
    pool = await create_pool(s.database_url, max_size=args.concurrency + 2)
    # Requests/minute is shared with every other worker through Postgres.
    vendors = await seed_rate_limits(pool, factory.roster.limits)
    factory = factory.with_token_sources(postgres_buckets(pool, vendors))
    stop = asyncio.Event()
    restore_signals = install_stop_signals(stop)
    try:
        await run_worker(
            pool,
            factory,
            cfg,
            concurrency=args.concurrency,
            stop=stop,
            exit_when_idle=args.exit_when_idle,
        )
    finally:
        restore_signals()
        await pool.close()


async def _leaderboard(args: argparse.Namespace) -> None:
    from arena.store.db import create_pool
    from arena.store.tournaments import leaderboard

    pool = await create_pool(settings().database_url)
    try:
        rows = await leaderboard(pool, args.game)
    finally:
        await pool.close()
    # Ranked by rating - 2*RD (Glicko-2); Elo is the v1 rating, shown alongside.
    for i, r in enumerate(rows, 1):
        print(
            f"{i:>2}. {r['player_id']:<28} {r['rating']:>7} +/- {2 * r['rd']:<6}"
            f" elo={r['elo']:<7} games={r['games']}  pts={r['points']}"
        )


async def _ratings(args: argparse.Namespace) -> None:
    from arena.store.db import create_pool
    from arena.store.ratings import rebuild

    pool = await create_pool(settings().database_url)
    try:
        n = await rebuild(pool, args.game)
    finally:
        await pool.close()
    print(f"replayed {n} completed matches into ratings")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="arena")
    ap.add_argument("--roster", help="roster TOML (default: $ARENA_ROSTER_PATH or roster.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("play", help="run one match in memory and print it")
    p.add_argument("game")
    p.add_argument("--p1", default="random:1")
    p.add_argument("--p2", default="random:2")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--budget", type=float, default=0.50, help="USD cap for this match")
    p.add_argument("--quiet", action="store_true")

    sub.add_parser("migrate", help="apply SQL migrations")

    t = sub.add_parser("tournament", help="queue a tournament, or resume a paused one")
    t.add_argument("--game")
    t.add_argument("--players", help="comma-separated roster ids")
    t.add_argument("--format", choices=["round_robin", "swiss"], default="round_robin")
    t.add_argument("--games-per-pair", type=int, default=10, help="round robin only")
    t.add_argument("--rounds", type=int, default=None, help="swiss only (default ceil(log2 N))")
    t.add_argument("--budget", type=float, default=None, help="USD cap (strongly advised)")
    t.add_argument("--seed", type=int, default=None)
    t_sub = t.add_subparsers(dest="tournament_cmd")
    tr = t_sub.add_parser("resume", help="re-queue failed matches and unpause")
    tr.add_argument("tournament_id", type=int)

    w = sub.add_parser("worker", help="run queued matches")
    w.add_argument("--concurrency", type=int, default=1)
    w.add_argument("--exit-when-idle", action="store_true")

    lb = sub.add_parser("leaderboard")
    lb.add_argument("--game", required=True)

    rt = sub.add_parser("ratings", help="maintain the ratings tables")
    rt_sub = rt.add_subparsers(dest="ratings_cmd", required=True)
    rb = rt_sub.add_parser("rebuild", help="replay completed matches into Elo + Glicko-2")
    rb.add_argument("--game", help="only this game (default: all)")

    s = sub.add_parser("serve", help="API + UI")
    s.add_argument("--port", type=int, default=8000)

    args = ap.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("arena.api:app", port=args.port)
        return
    handler = {
        "play": _play,
        "migrate": _migrate,
        "tournament": _tournament,
        "worker": _worker,
        "leaderboard": _leaderboard,
        "ratings": _ratings,
    }[args.cmd]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
