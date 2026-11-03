"""A paid player must list its prices: without them its spend counts as $0 and no budget cap can
stop it. This happened on 2026-11-03 with a hand-added roster entry, caught after one test game."""

import pytest

from arena.roster import load_roster

PAID = """
[[players]]
id = "x"
provider = "anthropic"
model = "claude-haiku-5-5"
prompt = "liars_dice/v1"
"""


def test_a_paid_player_without_prices_is_refused(tmp_path):
    path = tmp_path / "roster.toml"
    path.write_text(PAID, encoding="utf-8")
    with pytest.raises(ValueError, match="needs price_input_per_mtok"):
        load_roster(path)


def test_priced_and_free_players_load(tmp_path):
    path = tmp_path / "roster.toml"
    path.write_text(
        PAID
        + "price_input_per_mtok = 0.1\nprice_output_per_mtok = 0.5\n"
        + '\n[[players]]\nid = "b"\nprovider = "bot"\nmodel = "liars_dice_cfr"\nseed = 1\n',
        encoding="utf-8",
    )
    players = load_roster(path).players
    assert players["x"].pricing.input_per_mtok == 0.1 and "b" in players
