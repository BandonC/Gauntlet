"""The demo compose profile: static checks only, since tests never start Docker."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_demo_services_are_all_in_the_demo_profile():
    text = (ROOT / "docker-compose.yml").read_text()
    blocks = text.split("\n  demo-")[1:]
    assert len(blocks) == 5  # db, migrate, api, worker, seed (the anchor is in migrate)
    for block in blocks:
        assert "profiles: [demo]" in block.split("\n\n")[0] or "*demo-app" in block


def test_dockerfile_installs_editable_so_migrations_are_found():
    assert "pip install --no-cache-dir -e ." in (ROOT / "Dockerfile").read_text()


def test_serve_accepts_a_host():
    assert '"--host"' in (ROOT / "arena" / "cli.py").read_text()


def test_seed_script_is_idempotent_by_construction():
    text = (ROOT / "scripts" / "demo_seed.py").read_text()
    assert "_already_seeded" in text and "FROM tournaments WHERE game = $1" in text
