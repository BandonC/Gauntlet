"""Providers for tests: never spend tokens in tests.

- MockProvider returns scripted completions (or raises scripted errors) in order.
- load_recording() turns a JSONL file written by RecordingProvider into a MockProvider,
  so real model behaviour can be replayed deterministically in tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from arena.providers.base import Completion, CompletionRequest, Provider, ProviderError
from arena.types import Usage


class MockProvider:
    def __init__(self, script: list[Completion | ProviderError], name: str = "mock"):
        self.name = name
        self._script = list(script)
        self.requests: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> Completion:
        self.requests.append(req)
        if not self._script:
            raise AssertionError("MockProvider script exhausted")
        item = self._script.pop(0)
        if isinstance(item, ProviderError):
            raise item
        return item


class RecordingProvider:
    """Wraps a real provider and appends every completion to a JSONL file."""

    def __init__(self, inner: Provider, path: Path):
        self.name = inner.name
        self._inner = inner
        self._path = path

    async def complete(self, req: CompletionRequest) -> Completion:
        out = await self._inner.complete(req)
        with self._path.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "model": req.model,  # a vendor's players share one file
                        "tool_input": out.tool_input,
                        "raw_text": out.raw_text,
                        "usage": out.usage.__dict__,
                        "stop_reason": out.stop_reason,
                    }
                )
                + "\n"
            )
        return out


def load_recording(path: Path, model: str | None = None) -> MockProvider:
    """A MockProvider that replays a recording, or only `model`'s lines of it (players of
    one vendor share a file)."""
    script: list[Completion | ProviderError] = []
    for line in path.read_text().splitlines():
        d = json.loads(line)
        if model is None or d.get("model") == model:
            script.append(
                Completion(
                    d["tool_input"], d["raw_text"], Usage(**d["usage"]), d.get("stop_reason")
                )
            )
    return MockProvider(script, name="recording")
