from __future__ import annotations

from arena.games.base import Game
from arena.games.code_break import CodeBreak
from arena.games.connect4 import Connect4
from arena.games.liars_dice import LiarsDice, LiarsDiceSmall
from arena.games.split_deal import SplitDeal
from arena.games.werewolf import Werewolf, WerewolfDayFirst
from arena.games.word_link import WordLink

_GAMES: dict[str, Game] = {
    "code_break": CodeBreak(),
    "connect4": Connect4(),
    "liars_dice": LiarsDice(),
    "liars_dice_small": LiarsDiceSmall(),
    "split_deal": SplitDeal(),
    "werewolf": Werewolf(),
    "werewolf_dayfirst": WerewolfDayFirst(),
    "word_link": WordLink(),
}


def get_game(name: str) -> Game:
    try:
        return _GAMES[name]
    except KeyError:
        raise ValueError(f"unknown game {name!r}; known: {sorted(_GAMES)}") from None


def list_games() -> list[str]:
    return sorted(_GAMES)
