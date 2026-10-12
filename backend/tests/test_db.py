from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frootio import db

EXPECTED_TABLES = {
    "settings",
    "series",
    "characters",
    "character_images",
    "character_voices",
    "character_aliases",
    "series_cast",
    "locations",
    "location_images",
    "series_locations",
    "music_tracks",
    "overdubs",
    "episodes",
    "exports",
    "script_versions",
    "split_proposals",
    "snippets",
    "snippet_cast",
    "takes",
    "caption_alignments",
    "jobs",
    "spend",
    "schema_migrations",
}


def tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r[0].startswith("sqlite_")
    }


def test_migrate_creates_full_schema(conn: sqlite3.Connection) -> None:
    assert tables(conn) == EXPECTED_TABLES
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    shipped = sorted(int(p.name.split("_", 1)[0]) for p in db.MIGRATIONS_DIR.glob("[0-9]*_*.sql"))
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [1, *shipped]


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    assert db.migrate(conn) == []


def test_schema_columns_used_by_contracts(conn: sqlite3.Connection) -> None:
    def cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    assert {"kind", "payload", "status", "fal_request_id", "attempts", "error", "result"} <= cols("jobs")
    assert {
        "kind",
        "amount_usd",
        "episode_id",
        "series_id",
        "character_id",
        "location_id",
        "take_id",
    } <= cols("spend")
    assert {"spend_cap_usd", "take_count", "music_track_id", "fx_volume", "captions_on"} <= cols("episodes")
    take_cols = cols("takes")
    for part in ("style", "legend", "location"):
        assert {f"{part}_text", f"{part}_overridden"} <= take_cols
        assert {f"{part}_override", f"{part}_overridden", f"{part}_source_snapshot"} <= cols("snippets")
    assert {
        "body_text",
        "tail_text",
        "prompt",
        "refs",
        "seed",
        "fal_request_id",
        "cost_usd",
        "estimated_cost_usd",
        "start_frame_take_id",
        "start_frame_time_s",
        "trim_in_s",
        "trim_out_s",
        "vocals_path",
        "fx_path",
        "poster_path",
    } <= take_cols


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO episodes (series_id, number, created_at, updated_at) VALUES (999, 1, 'x', 'x')"
        )


def test_status_check_constraint(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (kind, status, created_at, updated_at) VALUES ('k', 'bogus', 'x', 'x')"
        )


def test_later_migrations_apply_in_order(conn: sqlite3.Connection, tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "003_second.sql").write_text("ALTER TABLE probe ADD COLUMN b TEXT;")
    (mdir / "002_first.sql").write_text("CREATE TABLE probe (a TEXT);")
    (mdir / "README.txt").write_text("ignored")
    assert db.migrate(conn, mdir) == [2, 3]
    assert {r["name"] for r in conn.execute("PRAGMA table_info(probe)")} == {"a", "b"}
    assert db.migrate(conn, mdir) == []


def test_failed_migration_rolls_back(conn: sqlite3.Connection, tmp_path: Path) -> None:
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    before = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    (mdir / "999_bad.sql").write_text("CREATE TABLE half (a TEXT);\nTHIS IS NOT SQL;")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, mdir)
    assert "half" not in tables(conn)
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == before


def test_unique_slug(conn: sqlite3.Connection) -> None:
    ts = db.now()
    conn.execute(
        "INSERT INTO characters (name, slug, created_at, updated_at) VALUES ('Kiwi', 'kiwi', ?, ?)", (ts, ts)
    )
    assert db.unique_slug(conn, "characters", "kiwi") == "kiwi-2"
    assert db.unique_slug(conn, "characters", "lemon") == "lemon"
