"""SQLite connection helper and schema migrations.

`schema.sql` is version 1. Later changes live in `migrations/NNN_name.sql`
(NNN >= 2), applied in order and tracked in `schema_migrations`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends

from . import config

PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_([A-Za-z0-9_\-]+)\.sql$")


def now() -> str:
    """Current time as ISO-8601 UTC text."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with WAL, foreign keys and Row factory.

    `check_same_thread=False` because FastAPI may run a sync dependency and the
    endpoint using it on different threadpool threads; each request still gets
    its own connection.
    """
    db_path = path or config.settings().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# Endpoint parameter type: `def endpoint(conn: db.Conn): ...`
Conn = Annotated[sqlite3.Connection, Depends(get_db)]


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    found: list[tuple[int, str, Path]] = []
    if not migrations_dir.is_dir():
        return found
    for path in migrations_dir.iterdir():
        m = _MIGRATION_RE.match(path.name)
        if not m:
            continue
        version = int(m.group(1))
        if version < 2:
            raise ValueError(f"migration {path.name}: versions start at 002 (001 is schema.sql)")
        found.append((version, m.group(2), path))
    found.sort()
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions in {migrations_dir}")
    return found


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply schema.sql (v1) and pending migrations. Returns versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    pending: list[tuple[int, str, Path]] = []
    if 1 not in applied:
        pending.append((1, "schema", SCHEMA_PATH))
    pending += [m for m in _discover_migrations(migrations_dir) if m[0] not in applied]

    done: list[int] = []
    for version, name, path in pending:
        sql = path.read_text()
        # One script = one transaction, so a failing migration leaves no partial state.
        script = (
            "BEGIN;\n"
            f"{sql}\n;\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({version}, {_sql_quote(name)}, {_sql_quote(now())});\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        done.append(version)
    return done


def _sql_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def rows(rs: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


def dumps(value: Any) -> str:
    """Serialize a JSON column value."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    """Parse a JSON column value; `default` when NULL/empty."""
    if text is None or text == "":
        return default
    return json.loads(text)


def unique_slug(conn: sqlite3.Connection, table: str, base: str) -> str:
    """Return `base`, or `base-2`, `base-3`, ... whichever is free in `table.slug`."""
    if not re.fullmatch(r"[a-z_]+", table):
        raise ValueError(f"bad table name {table!r}")
    candidate, n = base, 1
    while conn.execute(f"SELECT 1 FROM {table} WHERE slug = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base}-{n}"
    return candidate
