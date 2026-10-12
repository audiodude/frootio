from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from frootio import backup, config, db, media


def test_backup_writes_consistent_copy(conn: sqlite3.Connection, env: Path) -> None:
    ts = db.now()
    conn.execute(
        "INSERT INTO series (name, slug, created_at, updated_at) VALUES ('B&S', 'kiwi-and-lemon', ?, ?)",
        (ts, ts),
    )
    conn.commit()
    path = backup.backup_now(today=date(2026, 10, 11))
    assert path == env / "media" / config.BACKUP_DIRNAME / "frootio-2026-10-11.db"
    copy = sqlite3.connect(path)
    try:
        assert copy.execute("SELECT slug FROM series").fetchall() == [("kiwi-and-lemon",)]
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        copy.close()
    assert sorted(p.name for p in path.parent.iterdir()) == ["frootio-2026-10-11.db"]


def test_backup_same_day_replaces(conn: sqlite3.Connection) -> None:
    day = date(2026, 1, 1)
    first = backup.backup_now(today=day)
    second = backup.backup_now(today=day)
    assert first == second
    assert len(list(media.backup_dir().iterdir())) == 1


def test_rotation_keeps_newest_14(conn: sqlite3.Connection) -> None:
    start = date(2026, 1, 1)
    for i in range(20):
        backup.backup_now(today=start + timedelta(days=i))
    (media.backup_dir() / "notes.txt").write_text("not a backup")
    backup.rotate(media.backup_dir())
    names = sorted(p.name for p in media.backup_dir().iterdir() if p.suffix == ".db")
    assert len(names) == 14
    assert names[0] == f"frootio-{(start + timedelta(days=6)).isoformat()}.db"
    assert names[-1] == f"frootio-{(start + timedelta(days=19)).isoformat()}.db"
    assert (media.backup_dir() / "notes.txt").exists()
