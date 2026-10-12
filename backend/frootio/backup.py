"""SQLite online backups into the (synced) media tree.

`<media root>/.frootio-backup/frootio-<YYYY-MM-DD>.db`, written to a temp file
then renamed, newest 14 kept. Runs on startup and every 24h while running.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import date
from pathlib import Path

from . import config, media

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^frootio-(\d{4}-\d{2}-\d{2})\.db$")


def backup_now(
    db_path: Path | None = None,
    dest_dir: Path | None = None,
    *,
    keep: int = config.BACKUP_KEEP,
    today: date | None = None,
) -> Path:
    db_path = db_path or config.settings().db_path
    dest_dir = dest_dir or media.backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    day = (today or date.today()).isoformat()  # local date: what the user sees in the folder
    final = dest_dir / f"frootio-{day}.db"
    tmp = dest_dir / f".frootio-{day}.db.tmp"
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            # A standalone rollback-journal file: no -wal/-shm siblings in the synced folder.
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    os.replace(tmp, final)
    rotate(dest_dir, keep)
    return final


def rotate(dest_dir: Path, keep: int = config.BACKUP_KEEP) -> list[Path]:
    """Delete all but the newest `keep` dated backups. Returns removed paths."""
    dated = sorted(
        (p for p in dest_dir.iterdir() if _NAME_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = dated[keep:]
    for p in removed:
        p.unlink()
    return removed


async def backup_loop(interval: float = config.BACKUP_INTERVAL_SECONDS) -> None:
    """Back up every `interval` seconds (the startup backup is done by the caller)."""
    while True:
        await asyncio.sleep(interval)
        try:
            path = await asyncio.to_thread(backup_now)
            log.info("backup written: %s", path)
        except Exception:
            log.exception("periodic backup failed")
