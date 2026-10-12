from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from frootio import db


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own media root and DB; no real keys."""
    monkeypatch.setenv("FROOTIO_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("FROOTIO_DB", str(tmp_path / "data" / "frootio.db"))
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def conn(env: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect()
    db.migrate(c)
    yield c
    c.close()


def make_episode(conn: sqlite3.Connection, number: int = 1) -> tuple[int, int]:
    """Insert a series (if missing) and an episode; returns (series_id, episode_id)."""
    ts = db.now()
    r = conn.execute("SELECT id FROM series WHERE slug = 'test-series'").fetchone()
    if r is None:
        series_id = conn.execute(
            "INSERT INTO series (name, slug, created_at, updated_at) VALUES ('Test', 'test-series', ?, ?)",
            (ts, ts),
        ).lastrowid
    else:
        series_id = r[0]
    episode_id = conn.execute(
        "INSERT INTO episodes (series_id, number, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (series_id, number, ts, ts),
    ).lastrowid
    conn.commit()
    return series_id, episode_id
