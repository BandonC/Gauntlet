"""Command line entry point.

arena play connect4 --p1 random --p2 haiku-5.5/c4-v1     one match, in memory, printed
arena play werewolf --players werewolf-bot:1,...,werewolf-bot:7
arena migrate                                            apply SQL migrations
arena tournament --game connect4 --players a,b,c --games-per-pair 10 --budget 5
arena tournament --game liars_dice --players a,b,c,d --format tables --table-size 3 --tables 12
arena tournament --game werewolf --players a,b,c,d,e,f,g --tables 1
arena worker --concurrency 4 --record recordings         run matches from the queue
arena leaderboard --game connect4
arena serve                                              API + UI on :8000
arena export --out site                                  static HTML + JSON (GitHub Pages)
arena export --out docs --tournaments 6,7 --matches 116  only those pages
arena bench --matches 5000 --workers 1,2,4,8             scaling table, bots, own database
arena chaos --matches 2000 --workers 4                   kill workers, pause Postgres, verify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

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
    """'random', 'random:SEED', 'liars-dice-bot', 'liars-dice-bot-v2', 'werewolf-bot' and
    'werewolf-bot:SEED' are built in; anything else is a roster id."""
    from arena.costs import FREE
    from arena.players.bots import (
        LIARS_DICE_V2_THRESHOLD,
        LiarsDiceBot,
        RandomPlayer,
        WerewolfBot,
    )

    if spec == "liars-dice-bot":
        return LiarsDiceBot(spec), FREE
    if spec == "liars-dice-bot-v2":
        return LiarsDiceBot(spec, LIARS_DICE_V2_THRESHOLD), FREE
    if spec == "werewolf-bot" or spec.startswith("werewolf-bot:"):
        seed = int(spec.split(":", 1)[1]) if ":" in spec else 0
        return WerewolfBot(spec, seed), FREE
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
    if args.record:
        factory = factory.with_record_dir(Path(args.record))
    seats = [s.strip() for s in args.players.split(",")] if args.players else [args.p1, args.p2]
    built = [_build_player(factory, s) for s in seats]

    def show(record, state):
        if args.quiet:
            return
        who = seats[record.seat]
        move = "FORFEIT" if record.forfeited else record.action
        extra = f"  (attempts={record.attempts})" if record.attempts > 1 else ""
        print(f"\nturn {record.turn_no}: {who} -> {move}{extra}  ${record.cost_usd:.5f}")
        if game.name == "connect4":  # no hidden info: show the board from the rendered view
            print(game.render_view(game.player_view(state, 0)).split("\n\n")[1])
        else:  # hidden-info games: the omniscient frame, compact
            print(json.dumps(game.spectator_view(state), separators=(",", ":")))

    store = InMemoryTurnStore()
    runner = MatchRunner(
        game,
        [p for p, _ in built],
        store,
        pricing=[pr for _, pr in built],
        budget=Budget(args.budget),
        on_turn=show,
    )
    out = await runner.run(MatchSpec("cli", args.game, args.seed, seats))
    print(f"\nscores: {dict(zip(seats, out.scores, strict=True))}")
    print(f"placements: {dict(zip(seats, out.placements, strict=True))}")
    print(f"turns: {out.num_turns}  forfeits: {out.forfeits}  cost: ${out.cost_usd:.5f}")
    turns = store.turns["cli"]
    for seat, (name, u) in enumerate(zip(seats, out.usage_by_seat, strict=True)):
        mine = [t for t in turns if t.seat == seat]
        illegal = sum(len(t.rejections) for t in mine)
        filled = sum(t.auto_filled for t in mine)
        print(
            f"  {name}: in={u.input_tokens} out={u.output_tokens} "
            f"cache_read={u.cache_read_tokens} illegal={illegal} auto_filled={filled}"
        )


async def _migrate(args: argparse.Namespace) -> None:
    from arena.store.migrate import migrate

    names = await migrate(settings().database_url)
    print("applied:", ", ".join(names) if names else "nothing (up to date)")


def _table_size(game: str, requested: int | None) -> int:
    """--table-size, which a game that seats only one number of players doesn't need."""
    from arena.games.registry import get_game

    g = get_game(game)
    if requested:
        return requested
    if g.min_players == g.max_players:
        return g.min_players
    sys.exit("arena tournament: --tables needs --table-size for this game")


async def _tournament(args: argparse.Namespace) -> None:
    from arena.store.db import create_pool
    from arena.store.tournaments import (
        create_round_robin,
        create_swiss,
        create_tables,
        resume_tournament,
    )

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
        fmt = args.format or ("tables" if args.tables else "round_robin")
        if fmt == "swiss":
            tid, n = await create_swiss(
                pool, args.game, cfgs, args.budget, rounds=args.rounds, seed=args.seed
            )
            print(f"tournament {tid}: swiss, round 1 queued ({n} matches)")
        elif fmt == "tables":
            if not args.tables:
                sys.exit("arena tournament: --format tables needs --tables")
            size = _table_size(args.game, args.table_size)
            try:
                tid, n = await create_tables(
                    pool, args.game, cfgs, size, args.tables, args.budget, args.seed
                )
            except ValueError as e:
                sys.exit(f"arena tournament: {e}")
            print(f"tournament {tid}: {n} tables of {size} queued")
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
    from arena.telemetry import configure_tracing, serve_metrics
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
    if args.record:
        factory = factory.with_record_dir(Path(args.record))
    stop = asyncio.Event()
    restore_signals = install_stop_signals(stop)
    serve_metrics(args.metrics_port)
    flush_traces = configure_tracing(s.otel_exporter_otlp_endpoint)
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
        flush_traces()
        await pool.close()


async def _leaderboard(args: argparse.Namespace) -> None:
    from arena.store.db import create_pool
    from arena.store.tournaments import leaderboard

    pool = await create_pool(settings().database_url)
    try:
        rows = await leaderboard(pool, args.game)
    finally:
        await pool.close()
    # Ranked by live Weng-Lin ordinal (mu - 3*sigma); Glicko-2 and Elo are heads-up-only.
    if rows and rows[0]["final_tournament_id"] is not None:
        print(f"final = Bradley-Terry fit of tournament {rows[0]['final_tournament_id']}")
    for i, r in enumerate(rows, 1):
        f = r["final"]
        final = f"{f['rating']} ({f['low']}-{f['high']})" if f else "-"
        print(
            f"{i:>2}. {r['player_id']:<28} final={final}"
            f" live: {r['ordinal']:>7} (mu={r['mu']} sigma={r['sigma']})"
            f" glicko={r['rating']} +/- {2 * r['rd']:<6}"
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


def _ids(text: str) -> list[int]:
    return [int(part) for part in text.split(",")]


async def _chaos(args: argparse.Namespace) -> None:
    from arena.bench import BenchError
    from arena.chaos import format_report, run_chaos

    try:
        report = await run_chaos(
            settings().database_url,
            matches=args.matches,
            workers=args.workers,
            players=args.players,
            seed=args.seed,
            time_limit=args.timeout,
        )
    except BenchError as e:
        sys.exit(f"arena chaos: {e}")
    print(format_report(report))
    if report.violations:
        sys.exit(1)


async def _bench(args: argparse.Namespace) -> None:
    from arena.bench import BenchError, format_table, result_json, run_bench

    try:
        results = await run_bench(settings().database_url, args.matches, args.workers, args.players)
    except BenchError as e:
        sys.exit(f"arena bench: {e}")
    print(f"\n{format_table(results)}")
    if args.json:
        await asyncio.to_thread(Path(args.json).write_text, result_json(results))


async def _export(args: argparse.Namespace) -> None:
    from arena.export import export_site
    from arena.store.db import create_pool

    pool = await create_pool(settings().database_url)
    try:
        summary = await export_site(pool, Path(args.out), args.tournaments, args.matches)
    except ValueError as e:
        sys.exit(f"arena export: {e}")
    finally:
        await pool.close()
    print(f"wrote {summary.pages} pages and {summary.json_files} JSON files to {args.out}")
    print(f"open {Path(args.out) / 'index.html'}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="arena")
    ap.add_argument("--roster", help="roster TOML (default: $ARENA_ROSTER_PATH or roster.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("play", help="run one match in memory and print it")
    p.add_argument("game")
    p.add_argument("--p1", default="random:1")
    p.add_argument("--p2", default="random:2")
    p.add_argument("--players", help="comma-separated seats, instead of --p1/--p2")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--budget", type=float, default=0.50, help="USD cap for this match")
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--record", metavar="DIR", help="append every model completion to DIR/<vendor>-*.jsonl"
    )

    sub.add_parser("migrate", help="apply SQL migrations")

    t = sub.add_parser("tournament", help="queue a tournament, or resume a paused one")
    t.add_argument("--game")
    t.add_argument("--players", help="comma-separated roster ids")
    t.add_argument(
        "--format",
        choices=["round_robin", "swiss", "tables"],
        help="default: tables if --tables is given, otherwise round_robin",
    )
    t.add_argument("--games-per-pair", type=int, default=10, help="round robin only")
    t.add_argument("--rounds", type=int, default=None, help="swiss only (default ceil(log2 N))")
    t.add_argument(
        "--table-size",
        type=int,
        default=None,
        help="tables only: seats per match, optional if the game seats one number",
    )
    t.add_argument("--tables", type=int, default=None, help="tables only: number of matches")
    t.add_argument("--budget", type=float, default=None, help="USD cap (strongly advised)")
    t.add_argument("--seed", type=int, default=None)
    t_sub = t.add_subparsers(dest="tournament_cmd")
    tr = t_sub.add_parser("resume", help="re-queue failed matches and unpause")
    tr.add_argument("tournament_id", type=int)

    w = sub.add_parser("worker", help="run queued matches")
    w.add_argument("--concurrency", type=int, default=1)
    w.add_argument("--exit-when-idle", action="store_true")
    w.add_argument("--metrics-port", type=int, help="serve Prometheus metrics on this port")
    w.add_argument(
        "--record", metavar="DIR", help="append every model completion to DIR/<vendor>-*.jsonl"
    )

    lb = sub.add_parser("leaderboard")
    lb.add_argument("--game", required=True)

    rt = sub.add_parser("ratings", help="maintain the ratings tables")
    rt_sub = rt.add_subparsers(dest="ratings_cmd", required=True)
    rb = rt_sub.add_parser("rebuild", help="replay completed matches into every rating")
    rb.add_argument("--game", help="only this game (default: all)")

    s = sub.add_parser("serve", help="API + UI")
    s.add_argument("--port", type=int, default=8000)

    e = sub.add_parser("export", help="render the site to static HTML + JSON files")
    e.add_argument("--out", default="site", help="output directory (default: site)")
    e.add_argument("--tournaments", type=_ids, help="only these tournament pages (comma-separated)")
    e.add_argument("--matches", type=_ids, help="only these match pages (comma-separated)")

    b = sub.add_parser("bench", help="scaling table from bot matches, in its own database")
    b.add_argument("--matches", type=int, default=5000)
    b.add_argument("--workers", type=_ids, default=[1, 2, 4, 8], help="worker process counts")
    b.add_argument("--players", type=int, default=8, help="bots sharing the rating rows")
    b.add_argument("--json", metavar="PATH", help="also write the results as JSON")

    c = sub.add_parser("chaos", help="kill workers and freeze Postgres, then check invariants")
    c.add_argument("--matches", type=int, default=2000)
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--players", type=int, default=8)
    c.add_argument("--seed", type=int, default=0, help="which kills and pauses, not the games")
    c.add_argument("--timeout", type=float, default=1800.0, help="seconds before giving up")

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
        "export": _export,
        "bench": _bench,
        "chaos": _chaos,
    }[args.cmd]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
