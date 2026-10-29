from __future__ import annotations

from arena.games.base import Game
from arena.games.connect4 import Connect4

_GAMES: dict[str, Game] = {
    "connect4": Connect4(),
    # v2: "liars_dice": LiarsDice(), "werewolf": Werewolf(),
}


def get_game(name: str) -> Game:
    try:
        return _GAMES[name]
    except KeyError:
        raise ValueError(f"unknown game {name!r}; known: {sorted(_GAMES)}") from None


def list_games() -> list[str]:
    return sorted(_GAMES)
