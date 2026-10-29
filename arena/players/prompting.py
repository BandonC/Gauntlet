"""Versioned prompt templates.

Prompts live in arena/players/prompts/<game>/<version>/{system,turn}.txt and use
string.Template ($var) placeholders, so JSON braces in prompts need no escaping.

NEVER edit a prompt version that has been used in a recorded match: copy it to a new
version directory instead. The prompt's content hash is part of a player's identity,
so editing in place would silently mix two different players' ratings.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import Template

from arena.players.base import TurnContext

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class PromptTemplate:
    key: str  # e.g. "connect4/v1"
    system: str
    turn: str

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256(f"{self.system}\x00{self.turn}".encode())
        return h.hexdigest()[:12]

    def render_system(self, ctx: TurnContext) -> str:
        return Template(self.system).substitute(
            game_name=ctx.game_name, rules=ctx.rules, tool_name=ctx.action_spec.name
        )

    def render_turn(self, ctx: TurnContext) -> str:
        feedback = ""
        if ctx.previous_error:
            feedback = (
                f"Your previous move was rejected: {ctx.previous_error}. Choose a legal move.\n\n"
            )
        return Template(self.turn).substitute(feedback=feedback, state=ctx.view_text)


def load_prompt(key: str) -> PromptTemplate:
    d = PROMPTS_DIR / key
    if not d.is_dir():
        raise ValueError(f"no prompt directory {d}")
    return PromptTemplate(
        key=key,
        system=(d / "system.txt").read_text(),
        turn=(d / "turn.txt").read_text(),
    )
