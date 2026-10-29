"""The Game contract. Every game engine implements this; nothing else knows game rules.

Design rules (see CLAUDE.md):
- Engines are pure and deterministic. `apply` returns a NEW state and never mutates.
  All randomness comes from the `seed` passed to `initial_state` (store an RNG state
  in the game state if you need dice/shuffles), so a match can be rebuilt exactly by
  replaying its persisted actions. This is what makes matches resumable.
- Engines never call an LLM, the network or the database.
- Hidden information lives in `player_view`: it returns only what that seat may see.
- Simultaneous phases (e.g. Werewolf votes) are modelled as sequential turns whose
  results stay hidden in `player_view` until the phase resolves.
- Free-text actions (e.g. Werewolf discussion) can't be enumerated, so legality is
  checked by `validate_action`; `legal_actions_hint` is optional help for prompts
  and random players.
"""

from __future__ import annotations

from typing import Any, Protocol

from arena.types import JSON, Action, ActionSpec

State = Any  # each game defines its own (ideally frozen) state type


class Game(Protocol):
    name: str
    min_players: int
    max_players: int

    def rules_text(self) -> str:
        """Plain-language rules, included in the (cacheable) system prompt."""
        ...

    def initial_state(self, num_players: int, seed: int) -> State: ...

    def current_player(self, state: State) -> int:
        """Seat index that must act next. Only called when not terminal."""
        ...

    def player_view(self, state: State, seat: int) -> JSON:
        """JSON-serializable view for one seat. Used for prompts, the UI and replays."""
        ...

    def spectator_view(self, state: State) -> JSON:
        """Omniscient view (all hidden info revealed) for replays and the UI."""
        ...

    def render_view(self, view: JSON) -> str:
        """Turn a player_view into the text an LLM sees on its turn."""
        ...

    def action_spec(self, state: State, seat: int) -> ActionSpec:
        """Tool schema the player must fill in for its next action."""
        ...

    def legal_actions_hint(self, state: State, seat: int) -> list[Action] | None:
        """Enumerated legal actions when practical, else None."""
        ...

    def validate_action(self, state: State, seat: int, action: Action) -> str | None:
        """None if legal; otherwise a short error shown back to the player."""
        ...

    def apply(self, state: State, seat: int, action: Action) -> State:
        """Apply a VALIDATED action. Must not mutate `state`."""
        ...

    def forfeit(self, state: State, seat: int) -> State:
        """State after `seat` forfeits (too many invalid attempts)."""
        ...

    def is_terminal(self, state: State) -> bool: ...

    def scores(self, state: State) -> list[float]:
        """Per-seat result for a terminal state: 1 win, 0 loss, 0.5 draw (2p games)."""
        ...
