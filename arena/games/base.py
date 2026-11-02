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
- `options` are per-match settings from `matches.options` (e.g. Werewolf's scheduled
  roles). They're part of the match's identity for replay, so they go through
  `MatchSpec` to `initial_state`. Games that take none ignore them. Without options a
  game must still be fully playable from its seed alone.
"""

from __future__ import annotations

from collections.abc import Sequence
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

    def initial_state(self, num_players: int, seed: int, options: JSON | None = None) -> State: ...

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

    def fill_action(self, state: State, seat: int, action: Action) -> Action:
        """Runs before validation. A game may complete a reply that left out a field with only
        one legal value. If this changes it, MatchRunner flags the turn as auto_filled."""
        return action

    def normalize_action(self, state: State, seat: int, action: Action) -> Action:
        """The action as it enters the log, for a VALIDATED action (Werewolf truncates long
        messages). If this changes it, MatchRunner flags the turn as truncated."""
        return action

    def role_pool(self, num_players: int) -> list[str] | None:
        """Roles a table of this size deals out, one per seat, if the game has any.
        Table tournaments balance them per player and pass them in options["roles"]."""
        return None

    def teams(self, num_players: int) -> tuple[tuple[int, ...], ...] | None:
        """Seats that win and lose together, for team ratings. None rates seats alone."""
        return None

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

    def placements(self, state: State) -> list[int]:
        """Per-seat finishing rank for a terminal state: 1 is best, ties share a rank
        (team games give all winners rank 1). final_placements applies forfeits, so
        engines don't handle them here. Requires an explicit `class MyGame(Game)` to
        inherit this default."""
        return placements_from_scores(self.scores(state))

    def final_placements(self, state: State, forfeits: Sequence[int]) -> list[int]:
        """What's stored on the match. Forfeited seats rank last, unless a game's forfeit
        doesn't remove the seat (Werewolf). forfeits: seats in the order they forfeited."""
        return rank_forfeits_last(self.placements(state), forfeits)


def placements_from_scores(scores: Sequence[float]) -> list[int]:
    """Competition ranking: higher score is better, equal scores tie (1, 2, 2, 4)."""
    return [1 + sum(other > score for other in scores) for score in scores]


def rank_forfeits_last(placements: Sequence[int], forfeits: Sequence[int]) -> list[int]:
    """Forfeited seats drop below every other seat, a later forfeit above an earlier
    one. forfeits: seats in the order they forfeited."""
    kept = [s for s in range(len(placements)) if s not in forfeits]
    ranks = [1 + sum(placements[o] < placements[s] for o in kept) for s in range(len(placements))]
    for i, seat in enumerate(reversed(forfeits)):
        ranks[seat] = len(kept) + 1 + i
    return ranks
