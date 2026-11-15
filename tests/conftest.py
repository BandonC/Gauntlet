"""Shared test setup.

Tests marked `db` need Postgres. Without DATABASE_URL they skip here, in one place, so plain
`pytest` never fails on a db test that forgot its own skipif (test_agent_chaos and test_bench
used to error with KeyError: 'DATABASE_URL').
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="db test: DATABASE_URL not set")
    for item in items:
        if item.get_closest_marker("db"):
            item.add_marker(skip)
