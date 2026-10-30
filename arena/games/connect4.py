"""Connect 4: 7 columns x 6 rows, two seats. Seat 0 plays X and moves first."""

from __future__ import annotations

from dataclasses import dataclass, replace

from arena.games.base import Game
from arena.types import JSON, Action, ActionSpec

COLS = 7
ROWS = 6
SYMBOLS = ("X", "O")

RULES = f"""\
Connect 4 on a {COLS}-column by {ROWS}-row board. Players alternate dropping one piece into
a column (0 to {COLS - 1}, left to right); the piece falls to the lowest empty cell.
The first player to line up 4 of their pieces horizontally, vertically or diagonally wins.
If the board fills with no line of 4, the game is a draw. You may not play a full column."""


@dataclass(frozen=True)
class C4State:
    # columns[c] holds seat indices bottom-up, e.g. (0, 1) = X at the bottom, O above it
    columns: tuple[tuple[int, ...], ...]
    to_move: int = 0
    move_count: int = 0
    winner: int | None = None
    draw: bool = False
    forfeited_by: int | None = None


class Connect4(Game):
    name = "connect4"
    min_players = 2
    max_players = 2

    def rules_text(self) -> str:
        return RULES

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> C4State:
        if num_players != 2:
            raise ValueError("connect4 needs exactly 2 players")
        return C4State(columns=tuple(() for _ in range(COLS)))

    def current_player(self, state: C4State) -> int:
        return state.to_move

    def _legal_columns(self, state: C4State) -> list[int]:
        return [c for c in range(COLS) if len(state.columns[c]) < ROWS]

    def player_view(self, state: C4State, seat: int) -> JSON:
        # Connect 4 has no hidden information, so every seat sees the same board.
        rows = []
        for r in range(ROWS - 1, -1, -1):
            rows.append("".join(SYMBOLS[col[r]] if r < len(col) else "." for col in state.columns))
        return {
            "you": SYMBOLS[seat],
            "to_move": SYMBOLS[state.to_move],
            "move_number": state.move_count + 1,
            "board": rows,  # top row first
            "legal_columns": self._legal_columns(state),
        }

    def spectator_view(self, state: C4State) -> JSON:
        view = self.player_view(state, 0)
        del view["you"]
        view["winner"] = SYMBOLS[state.winner] if state.winner is not None else None
        view["draw"] = state.draw
        view["forfeited_by"] = (
            SYMBOLS[state.forfeited_by] if state.forfeited_by is not None else None
        )
        return view

    def render_view(self, view: JSON) -> str:
        board = "\n".join(" ".join(row) for row in view["board"])
        header = " ".join(str(c) for c in range(COLS))
        legal = ", ".join(str(c) for c in view["legal_columns"])
        return (
            f"You are {view['you']}. Move {view['move_number']}.\n\n"
            f"{header}\n{board}\n\n"
            f"Legal columns: {legal}"
        )

    def action_spec(self, state: C4State, seat: int) -> ActionSpec:
        return ActionSpec(
            name="drop_piece",
            description="Drop one of your pieces into a column.",
            input_schema={
                "type": "object",
                "properties": {
                    "column": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": COLS - 1,
                        "description": "Column index, 0 (left) to 6 (right).",
                    }
                },
                "required": ["column"],
                "additionalProperties": False,
            },
        )

    def legal_actions_hint(self, state: C4State, seat: int) -> list[Action]:
        return [{"column": c} for c in self._legal_columns(state)]

    def validate_action(self, state: C4State, seat: int, action: Action) -> str | None:
        if self.is_terminal(state):
            return "the game is over"
        if seat != state.to_move:
            return "it is not your turn"
        col = action.get("column") if isinstance(action, dict) else None
        if not isinstance(col, int) or isinstance(col, bool):
            return "action must be an object with an integer 'column'"
        if not 0 <= col < COLS:
            return f"column {col} is out of range 0-{COLS - 1}"
        if len(state.columns[col]) >= ROWS:
            return f"column {col} is full"
        return None

    def apply(self, state: C4State, seat: int, action: Action) -> C4State:
        col = action["column"]
        new_col = state.columns[col] + (seat,)
        columns = state.columns[:col] + (new_col,) + state.columns[col + 1 :]
        row = len(new_col) - 1
        move_count = state.move_count + 1
        winner = seat if _wins(columns, col, row, seat) else None
        draw = winner is None and move_count == COLS * ROWS
        return replace(
            state,
            columns=columns,
            to_move=1 - seat,
            move_count=move_count,
            winner=winner,
            draw=draw,
        )

    def forfeit(self, state: C4State, seat: int) -> C4State:
        return replace(state, winner=1 - seat, forfeited_by=seat)

    def is_terminal(self, state: C4State) -> bool:
        return state.winner is not None or state.draw

    def scores(self, state: C4State) -> list[float]:
        if state.winner is not None:
            return [1.0 if s == state.winner else 0.0 for s in range(2)]
        if state.draw:
            return [0.5, 0.5]
        raise ValueError("scores() called on a non-terminal state")


def _wins(columns: tuple[tuple[int, ...], ...], col: int, row: int, seat: int) -> bool:
    def at(c: int, r: int) -> int | None:
        if 0 <= c < COLS and 0 <= r < len(columns[c]):
            return columns[c][r]
        return None

    for dc, dr in ((1, 0), (0, 1), (1, 1), (1, -1)):
        count = 1
        for sign in (1, -1):
            c, r = col + sign * dc, row + sign * dr
            while at(c, r) == seat:
                count += 1
                c, r = c + sign * dc, r + sign * dr
        if count >= 4:
            return True
    return False
