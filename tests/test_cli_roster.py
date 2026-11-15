"""`--roster` is accepted before the command, after it, and after a nested command."""

import pytest

from arena.cli import build_parser


@pytest.mark.parametrize(
    ("argv", "roster"),
    [
        (["worker"], None),
        (["--roster", "a.toml", "worker"], "a.toml"),
        (["worker", "--roster", "b.toml"], "b.toml"),
        (["--roster", "a.toml", "worker", "--roster", "b.toml"], "b.toml"),
        (["tournament", "cancel", "3", "--reason", "x", "--roster", "n.toml"], "n.toml"),
        (["tournament", "--roster", "t.toml", "cancel", "3", "--reason", "x"], "t.toml"),
        (["--roster", "g.toml", "tournament", "resume", "4"], "g.toml"),
    ],
)
def test_roster_works_in_every_position(argv, roster):
    assert build_parser().parse_args(argv).roster == roster
