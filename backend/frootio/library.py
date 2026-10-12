"""Character/location library helpers shared by the routers and generators.

- Alias validation: two characters cast in the same series can't share a
  speaker tag (names included), compared case-insensitively.
- Content-addressed images for characters and locations, with their pixel size
  (needed for H3 image-token cost).
- ffprobe helpers and JSON shapes for library rows.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from . import db, media

Owner = Literal["character", "location"]
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}


class LibraryError(ValueError):
    """A user-facing validation error (mapped to HTTP 4xx by the routers)."""

    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# --- aliases -------------------------------------------------------------------


def norm_alias(text: str) -> str:
    return " ".join(text.split()).lower()


def character_names(conn: sqlite3.Connection, character_id: int) -> dict[str, str]:
    """All speaker tags of a character: {normalized: display}, name first."""
    r = conn.execute("SELECT name FROM characters WHERE id = ?", (character_id,)).fetchone()
    if r is None:
        raise LibraryError("character not found", 404)
    names = {norm_alias(r["name"]): r["name"]}
    for a in conn.execute(
        "SELECT alias, alias_norm FROM character_aliases WHERE character_id = ? ORDER BY id", (character_id,)
    ):
        names.setdefault(a["alias_norm"], a["alias"])
    return names


def alias_conflicts(
    conn: sqlite3.Connection,
    character_id: int,
    names: dict[str, str],
    series_ids: Iterable[int] | None = None,
) -> list[str]:
    """Collisions of `names` with other characters cast in the same series.

    `series_ids` defaults to every series the character is cast in (used when
    editing a character's name/aliases); casting passes the target series.
    """
    if series_ids is None:
        series_ids = [
            r[0]
            for r in conn.execute("SELECT series_id FROM series_cast WHERE character_id = ?", (character_id,))
        ]
    problems: list[str] = []
    for sid in series_ids:
        series_name = conn.execute("SELECT name FROM series WHERE id = ?", (sid,)).fetchone()[0]
        others = conn.execute(
            "SELECT c.id, c.name FROM series_cast sc JOIN characters c ON c.id = sc.character_id"
            " WHERE sc.series_id = ? AND c.id != ? ORDER BY c.name",
            (sid, character_id),
        ).fetchall()
        for other in others:
            shared = names.keys() & character_names(conn, other["id"]).keys()
            for n in sorted(shared):
                problems.append(f"'{names[n]}' is already used by {other['name']} in series {series_name}")
    return problems


def check_aliases(
    conn: sqlite3.Connection, character_id: int, names: dict[str, str], series_ids=None
) -> None:
    problems = alias_conflicts(conn, character_id, names, series_ids)
    if problems:
        raise LibraryError("; ".join(problems), 409)


def clean_aliases(aliases: Iterable[str]) -> dict[str, str]:
    """Trimmed, non-empty aliases deduplicated case-insensitively: {norm: display}."""
    out: dict[str, str] = {}
    for a in aliases:
        display = " ".join(a.split())
        if not display:
            continue
        if any(ch in display for ch in "[]:"):
            raise LibraryError(f"alias {display!r} can't contain '[', ']' or ':'")
        out.setdefault(norm_alias(display), display)
    return out


def set_aliases(conn: sqlite3.Connection, character_id: int, aliases: Iterable[str]) -> None:
    """Replace a character's aliases after validating them against its series casts (no commit)."""
    wanted = clean_aliases(aliases)
    name = conn.execute("SELECT name FROM characters WHERE id = ?", (character_id,)).fetchone()
    if name is None:
        raise LibraryError("character not found", 404)
    check_aliases(conn, character_id, {norm_alias(name[0]): name[0], **wanted})
    conn.execute("DELETE FROM character_aliases WHERE character_id = ?", (character_id,))
    ts = db.now()
    for n, display in wanted.items():
        conn.execute(
            "INSERT INTO character_aliases (character_id, alias, alias_norm, created_at) VALUES (?, ?, ?, ?)",
            (character_id, display, n, ts),
        )


def rename_character(conn: sqlite3.Connection, character_id: int, name: str) -> None:
    """Rename a character; its name is a speaker tag too, so validate it like an alias (no commit)."""
    name = " ".join(name.split())
    if not name:
        raise LibraryError("name can't be empty")
    names = {norm_alias(name): name}
    for a in conn.execute(
        "SELECT alias, alias_norm FROM character_aliases WHERE character_id = ?", (character_id,)
    ):
        names.setdefault(a["alias_norm"], a["alias"])
    check_aliases(conn, character_id, names)
    conn.execute("UPDATE characters SET name = ? WHERE id = ?", (name, character_id))


def cast_character(conn: sqlite3.Connection, series_id: int, character_id: int) -> None:
    """Cast a (non-archived) character into a series, enforcing alias uniqueness (no commit)."""
    c = conn.execute("SELECT archived FROM characters WHERE id = ?", (character_id,)).fetchone()
    if c is None:
        raise LibraryError("character not found", 404)
    if conn.execute(
        "SELECT 1 FROM series_cast WHERE series_id = ? AND character_id = ?", (series_id, character_id)
    ).fetchone():
        return
    if c["archived"]:
        raise LibraryError("archived characters can't be cast")
    check_aliases(conn, character_id, character_names(conn, character_id), [series_id])
    conn.execute(
        "INSERT INTO series_cast (series_id, character_id, created_at) VALUES (?, ?, ?)",
        (series_id, character_id, db.now()),
    )


# --- probing -------------------------------------------------------------------


def ffprobe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise LibraryError(f"unreadable media file: {proc.stderr.strip()[:200] or 'ffprobe failed'}")
    return json.loads(proc.stdout)


def image_size(path: Path) -> tuple[int, int]:
    info = ffprobe(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and s.get("width") and s.get("height"):
            return int(s["width"]), int(s["height"])
    raise LibraryError("not an image")


def sniff_image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def image_ext(filename: str | None, content_type: str | None = None, data: bytes | None = None) -> str:
    """Extension for an image: sniffed from the bytes when given, else from the name/content type."""
    ext = sniff_image_ext(data) if data is not None else None
    if ext is None:
        ext = Path(filename or "").suffix.lower().lstrip(".")
        if not ext and content_type and content_type.startswith("image/"):
            ext = content_type.split("/", 1)[1].split(";")[0].strip()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in IMAGE_EXTS:
        raise LibraryError(f"unsupported image type {ext or '?'!r} (png, jpg, webp)")
    return ext


# --- images --------------------------------------------------------------------

_IMAGE_TABLE = {
    "character": ("character_images", "character_id"),
    "location": ("location_images", "location_id"),
}


def owner_slug(conn: sqlite3.Connection, owner: Owner, owner_id: int) -> str:
    table = "characters" if owner == "character" else "locations"
    r = conn.execute(f"SELECT slug FROM {table} WHERE id = ?", (owner_id,)).fetchone()
    if r is None:
        raise LibraryError(f"{owner} not found", 404)
    return r[0]


def images_dir(owner: Owner, slug: str) -> str:
    return media.character_images_dir(slug) if owner == "character" else media.location_images_dir(slug)


def add_image(
    conn: sqlite3.Connection,
    owner: Owner,
    owner_id: int,
    data: bytes,
    ext: str,
    *,
    source: str = "upload",
    gen_prompt: str | None = None,
    gen_model: str | None = None,
    gen_input_paths: list[str] | None = None,
    gen_request_id: str | None = None,
) -> int:
    """Store image bytes content-addressed for a character/location; returns the image row id.

    Validates that the bytes are an image before writing anything. Re-adding the
    same bytes returns the existing row (no commit).
    """
    table, fk = _IMAGE_TABLE[owner]
    slug = owner_slug(conn, owner, owner_id)
    sha = media.sha256_bytes(data)
    existing = conn.execute(
        f"SELECT id FROM {table} WHERE {fk} = ? AND sha256 = ?", (owner_id, sha)
    ).fetchone()
    if existing:
        return existing[0]
    with tempfile.NamedTemporaryFile(suffix=f".{ext}") as tmp:
        tmp.write(data)
        tmp.flush()
        width, height = image_size(Path(tmp.name))
    rel = media.store_bytes(images_dir(owner, slug), data, ext)
    cur = conn.execute(
        f"INSERT INTO {table} ({fk}, path, sha256, width, height, source, gen_prompt, gen_model,"
        " gen_input_paths, gen_request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            owner_id,
            rel,
            sha,
            width,
            height,
            source,
            gen_prompt,
            gen_model,
            None if gen_input_paths is None else db.dumps(gen_input_paths),
            gen_request_id,
            db.now(),
        ),
    )
    return cur.lastrowid


def is_library_image(conn: sqlite3.Connection, rel: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM character_images WHERE path = ?"
            " UNION SELECT 1 FROM location_images WHERE path = ?",
            (rel, rel),
        ).fetchone()
    )


def image_json(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["gen_input_paths"] = db.loads(d.get("gen_input_paths"), [])
    return d


# --- JSON shapes -----------------------------------------------------------------


def voice_json(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def character_json(conn: sqlite3.Connection, c: sqlite3.Row) -> dict[str, Any]:
    main = conn.execute("SELECT * FROM character_images WHERE id = ?", (c["main_image_id"],)).fetchone()
    voice = conn.execute("SELECT * FROM character_voices WHERE id = ?", (c["voice_id"],)).fetchone()
    aliases = [
        r[0]
        for r in conn.execute(
            "SELECT alias FROM character_aliases WHERE character_id = ? ORDER BY id", (c["id"],)
        )
    ]
    series = db.rows(
        conn.execute(
            "SELECT s.id, s.name, s.slug FROM series_cast sc JOIN series s ON s.id = sc.series_id"
            " WHERE sc.character_id = ? ORDER BY s.name",
            (c["id"],),
        ).fetchall()
    )
    return {
        **dict(c),
        "archived": bool(c["archived"]),
        "main_image": image_json(main) if main else None,
        "voice": voice_json(voice),
        "aliases": aliases,
        "series": series,
    }


def location_json(conn: sqlite3.Connection, loc: sqlite3.Row) -> dict[str, Any]:
    refs = [
        image_json(r)
        for r in conn.execute(
            "SELECT * FROM location_images WHERE location_id = ? AND ref_order IS NOT NULL"
            " ORDER BY ref_order",
            (loc["id"],),
        )
    ]
    series = db.rows(
        conn.execute(
            "SELECT s.id, s.name, s.slug FROM series_locations sl JOIN series s ON s.id = sl.series_id"
            " WHERE sl.location_id = ? ORDER BY s.name",
            (loc["id"],),
        ).fetchall()
    )
    return {**dict(loc), "archived": bool(loc["archived"]), "ref_images": refs, "series": series}


# --- music -------------------------------------------------------------------------


def audio_duration(path: Path) -> float:
    info = ffprobe(path)
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise LibraryError("not an audio file")
    return float(info.get("format", {}).get("duration") or 0.0)


def add_music_track(conn: sqlite3.Connection, series_id: int, src: Path, track_name: str) -> int:
    """Add an audio file to a series' music library as `series/<slug>/music/<track>.mp3` (no commit).

    Non-mp3 input is transcoded to mp3. Identical bytes already in the library
    return the existing track; a name clash with different audio gets `-2`, `-3`, ...
    """
    s = conn.execute("SELECT slug FROM series WHERE id = ?", (series_id,)).fetchone()
    if s is None:
        raise LibraryError("series not found", 404)
    audio_duration(src)  # validates before anything is written
    with tempfile.TemporaryDirectory() as tmp:
        if src.suffix.lower() == ".mp3":
            mp3 = src
        else:
            mp3 = Path(tmp) / "track.mp3"
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(src),
                    "-vn",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(mp3),
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise LibraryError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")
        data = mp3.read_bytes()
        seconds = audio_duration(mp3)
    sha = media.sha256_bytes(data)
    existing = conn.execute(
        "SELECT id FROM music_tracks WHERE series_id = ? AND sha256 = ?", (series_id, sha)
    ).fetchone()
    if existing:
        return existing[0]
    base = media.slugify(track_name)
    music_dir = media.series_music_dir(s[0])
    name, n = base, 1
    while True:
        rel = f"{music_dir}/{name}.mp3"
        dest = media.abs_path(rel)
        taken = conn.execute(
            "SELECT 1 FROM music_tracks WHERE series_id = ? AND path = ?", (series_id, rel)
        ).fetchone()
        if not taken and not dest.exists():
            media.write_file(rel, data)
            break
        if not taken and media.sha256_file(dest) == sha:
            break  # same audio left on disk by an earlier (rolled back) run: reuse, never overwrite
        n += 1
        name = f"{base}-{n}"
    return conn.execute(
        "INSERT INTO music_tracks (series_id, name, path, sha256, duration_s, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (series_id, name, rel, sha, round(seconds, 3), db.now()),
    ).lastrowid
