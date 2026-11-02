"""Werewolf play-style prompts, the players built from them, and `--tables` on the CLI."""

import pathlib
import re
from types import SimpleNamespace

import pytest

from arena import cli
from arena.games.werewolf import Werewolf
from arena.players.base import TurnContext
from arena.players.prompting import PROMPTS_DIR, load_prompt
from arena.roster import config_hash, load_roster

STYLES = ("plain", "cautious", "aggressive", "analytical")
EXAMPLE = pathlib.Path(__file__).parent.parent / "roster.example.toml"


def rendered(key):
    game = Werewolf()
    state = game.initial_state(7, 1)
    ctx = TurnContext(
        game_name="werewolf",
        seat=0,
        rules=game.rules_text(),
        view=game.player_view(state, 0),
        view_text=game.render_view(game.player_view(state, 0)),
        action_spec=game.action_spec(state, game.current_player(state)),
        legal_actions_hint=None,
    )
    prompt = load_prompt(key)
    return prompt, prompt.render_system(ctx), prompt.render_turn(ctx)


@pytest.mark.parametrize("style", STYLES)
def test_v2_only_adds_the_length_line_to_its_v1_style(style):
    _, v1_system, v1_turn = rendered(f"werewolf/v1-{style}")
    _, system, turn = rendered(f"werewolf/v2-{style}")
    line = "Keep each message to 2\u20133 sentences, under 300 characters.\n\n"
    assert system.count(line) == 1  # an en dash, so this fails if prompts aren't read as UTF-8
    assert system.replace(line, "") == v1_system
    assert turn == v1_turn


def test_every_earlier_prompt_is_ascii_so_reading_utf8_kept_its_identity():
    for path in PROMPTS_DIR.rglob("*.txt"):
        if "v2-" not in str(path) and "werewolf_dayfirst" not in str(path):
            assert path.read_bytes().isascii(), path


@pytest.mark.parametrize("style", STYLES)
def test_a_style_only_adds_one_paragraph_to_v1(style):
    base, base_system, base_turn = rendered("werewolf/v1")
    prompt, system, turn = rendered(f"werewolf/v1-{style}")
    assert turn == base_turn
    added = re.search(r"Play style: .*\n\n", system)
    assert added and system.replace(added[0], "", 1) == base_system
    assert prompt.content_hash != base.content_hash


def test_styles_are_all_different():
    assert len({load_prompt(f"werewolf/v1-{s}").content_hash for s in STYLES}) == 4


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_the_example_roster_has_eight_distinct_werewolf_players(version):
    players = [
        p
        for p in load_roster(EXAMPLE).players.values()
        if p.prompt.startswith(f"werewolf/{version}-")
    ]
    assert len(players) == 8
    assert len({config_hash(p) for p in players}) == 8
    assert {p.model for p in players} == {"claude-haiku-5-5", "claude-haiku-4-5-20251001"}
    assert {p.prompt for p in players} == {f"werewolf/{version}-{s}" for s in STYLES}
    assert all(f"/ww-{version}-" in p.id for p in players)


@pytest.fixture
def created(monkeypatch, tmp_path):
    """Runs the CLI's tournament command against fakes; returns what create_tables got."""
    seen = {}

    class Pool:
        async def close(self):
            pass

    async def create_pool(*a, **k):
        return Pool()

    async def create_tables(pool, game, cfgs, size, tables, budget, seed, spend_mode):
        seen.update(game=game, ids=[c.id for c in cfgs], size=size, tables=tables, budget=budget)
        seen["spend_mode"] = spend_mode
        return 1, tables

    async def create_round_robin(pool, game, cfgs, games_per_pair, budget, seed, spend_mode):
        seen["format"] = "round_robin"
        seen["spend_mode"] = spend_mode
        return 1, 1

    monkeypatch.setattr("arena.store.db.create_pool", create_pool)
    monkeypatch.setattr("arena.store.tournaments.create_tables", create_tables)
    monkeypatch.setattr("arena.store.tournaments.create_round_robin", create_round_robin)
    ids = [f"ww{i}" for i in range(7)]
    roster = tmp_path / "roster.toml"
    roster.write_text(
        "\n".join(
            f'[[players]]\nid = "{p}"\nprovider = "bot"\nmodel = "werewolf"\nseed = {i}'
            for i, p in enumerate(ids)
        )
    )

    def run(game, *flags):
        cli.main(
            ["--roster", str(roster), "tournament", "--game", game, "--players", ",".join(ids)]
            + list(flags)
        )
        return SimpleNamespace(**seen)

    return run


def test_tables_alone_queues_one_werewolf_table_of_seven(created):
    out = created("werewolf", "--tables", "1", "--budget", "0.5")
    assert (out.game, out.size, out.tables, out.budget) == ("werewolf", 7, 1, 0.5)
    assert len(out.ids) == 7


def test_a_variable_size_game_still_needs_table_size(created):
    with pytest.raises(SystemExit, match="--table-size"):
        created("liars_dice", "--tables", "1")
    assert created("liars_dice", "--tables", "1", "--table-size", "3").size == 3


def test_format_tables_still_needs_tables(created):
    with pytest.raises(SystemExit, match="--tables"):
        created("werewolf", "--format", "tables")


def test_without_tables_the_default_is_still_round_robin(created):
    assert created("connect4").format == "round_robin"


def test_spend_is_exact_unless_asked(created):
    assert created("connect4").spend_mode == "exact"
    assert created("connect4", "--spend-mode", "sharded").spend_mode == "sharded"
    assert created("werewolf", "--tables", "1", "--spend-mode", "sharded").spend_mode == "sharded"
