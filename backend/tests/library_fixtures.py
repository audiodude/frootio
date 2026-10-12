"""Library rows for script/snippet tests (T2 owns the real library API)."""

from __future__ import annotations

import sqlite3

from frootio import db, media


def add_character(
    conn: sqlite3.Connection,
    series_id: int | None,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    description: str = "",
    image: bool = True,
    voice: bool = False,
    archived: bool = False,
) -> int:
    ts = db.now()
    slug = db.unique_slug(conn, "characters", media.slugify(name))
    cid = conn.execute(
        "INSERT INTO characters (name, slug, description, archived, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name, slug, description, int(archived), ts, ts),
    ).lastrowid
    if image:
        sha = media.sha256_bytes(f"img-{slug}".encode())
        img = conn.execute(
            "INSERT INTO character_images (character_id, path, sha256, width, height, created_at)"
            " VALUES (?, ?, ?, 768, 1344, ?)",
            (cid, f"characters/{slug}/images/{sha[:12]}.png", sha, ts),
        ).lastrowid
        conn.execute("UPDATE characters SET main_image_id = ? WHERE id = ?", (img, cid))
    if voice:
        sha = media.sha256_bytes(f"voice-{slug}".encode())
        v = conn.execute(
            "INSERT INTO character_voices (character_id, path, sha256, duration_s, source, created_at)"
            " VALUES (?, ?, ?, 4.0, 'upload', ?)",
            (cid, f"characters/{slug}/voice/{sha[:12]}.wav", sha, ts),
        ).lastrowid
        conn.execute("UPDATE characters SET voice_id = ? WHERE id = ?", (v, cid))
    for a in aliases:
        conn.execute(
            "INSERT INTO character_aliases (character_id, alias, alias_norm, created_at) VALUES (?, ?, ?, ?)",
            (cid, a, a.strip().lower(), ts),
        )
    if series_id is not None:
        conn.execute(
            "INSERT INTO series_cast (series_id, character_id, created_at) VALUES (?, ?, ?)",
            (series_id, cid, ts),
        )
    conn.commit()
    return cid


def add_location(
    conn: sqlite3.Connection, series_id: int | None, name: str, *, description: str = "", images: int = 0
) -> int:
    ts = db.now()
    slug = db.unique_slug(conn, "locations", media.slugify(name))
    lid = conn.execute(
        "INSERT INTO locations (name, slug, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, slug, description, ts, ts),
    ).lastrowid
    for i in range(images):
        sha = media.sha256_bytes(f"loc-{slug}-{i}".encode())
        conn.execute(
            "INSERT INTO location_images (location_id, path, sha256, width, height, ref_order, created_at)"
            " VALUES (?, ?, ?, 768, 1344, ?, ?)",
            (lid, f"locations/{slug}/images/{sha[:12]}.png", sha, i + 1, ts),
        )
    if series_id is not None:
        conn.execute(
            "INSERT INTO series_locations (series_id, location_id, created_at) VALUES (?, ?, ?)",
            (series_id, lid, ts),
        )
    conn.commit()
    return lid


def kiwi_and_lemon_cast(conn: sqlite3.Connection, series_id: int) -> dict[str, int]:
    """A small recurring cast with aliases, including multi-alias and alias-free characters."""
    return {
        "kiwi": add_character(
            conn, series_id, "Kiwi Kowalski", aliases=("Kiwi",), description="a lanky kiwi", voice=True
        ),
        "lemon": add_character(
            conn,
            series_id,
            "Lemon Lee",
            aliases=("Lemon",),
            description="a sweet lemon",
            voice=True,
        ),
        "mango": add_character(conn, series_id, "Mango", aliases=("Mango",), description="a baker"),
        "plum": add_character(
            conn, series_id, "Plum Prentice", aliases=("Plum", "Ms. Plum", "Landlady")
        ),
        "fig": add_character(conn, series_id, "Detective Fig", aliases=("Detective",)),
        "olive": add_character(conn, series_id, "Chef Olive", aliases=("Olive",)),
        "quince": add_character(conn, series_id, "Quince"),
    }


def add_take(
    conn: sqlite3.Connection,
    snippet_id: int,
    *,
    trim_out_s: float | None = None,
    video_duration_s: float | None = 10.0,
    start_frame: tuple[int, float | None] | None = None,
    select: bool = True,
) -> int:
    ts = db.now()
    ep = conn.execute("SELECT episode_id FROM snippets WHERE id = ?", (snippet_id,)).fetchone()[0]
    tid = conn.execute(
        "INSERT INTO takes (snippet_id, episode_id, status, requested_duration_s, trim_out_s,"
        " video_duration_s,"
        " start_frame_take_id, start_frame_time_s, created_at, updated_at)"
        " VALUES (?, ?, 'done', 10, ?, ?, ?, ?, ?, ?)",
        (
            snippet_id,
            ep,
            trim_out_s,
            video_duration_s,
            start_frame[0] if start_frame else None,
            start_frame[1] if start_frame else None,
            ts,
            ts,
        ),
    ).lastrowid
    if select:
        conn.execute("UPDATE snippets SET selected_take_id = ? WHERE id = ?", (tid, snippet_id))
    conn.commit()
    return tid
