"""Media tree layout and the content-addressed store.

All paths stored in the DB are relative to the media root (POSIX style) and
served read-only at `/media/<relpath>`. The app never deletes media files.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from . import config

_CHUNK = 1 << 20


def root() -> Path:
    return config.settings().media_root


def abs_path(rel: str | PurePosixPath) -> Path:
    """Absolute path for a canonical media-root-relative path; rejects escapes."""
    rel_s = str(rel)
    if rel_s.startswith("/") or "\\" in rel_s:
        raise ValueError(f"media path must be relative: {rel_s!r}")
    parts = rel_s.split("/")
    if any(p in ("..", ".", "") for p in parts):
        raise ValueError(f"invalid media path: {rel_s!r}")
    return root().joinpath(*parts)


def rel_path(path: Path) -> str:
    """Media-root-relative POSIX path for an absolute path inside the root."""
    return path.resolve().relative_to(root().resolve()).as_posix()


def url(rel: str) -> str:
    return "/media/" + quote(rel)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _norm_ext(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]+", ext):
        raise ValueError(f"invalid extension {ext!r}")
    return ext


def _atomic_write(dest: Path, write) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            write(f)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def store_bytes(dir: str, data: bytes, ext: str) -> str:
    """Write `data` as `<dir>/<sha12>.<ext>` unless it already exists. Returns relpath."""
    rel = f"{dir.strip('/')}/{sha256_bytes(data)[:12]}.{_norm_ext(ext)}"
    dest = abs_path(rel)
    if not dest.exists():
        _atomic_write(dest, lambda f: f.write(data))
    return rel


def store_file(dir: str, src: Path, ext: str | None = None) -> str:
    """Copy a file into the content-addressed store (streamed). Returns relpath."""
    ext = _norm_ext(ext or src.suffix)
    rel = f"{dir.strip('/')}/{sha256_file(src)[:12]}.{ext}"
    dest = abs_path(rel)
    if not dest.exists():

        def copy(f) -> None:
            with src.open("rb") as s:
                shutil.copyfileobj(s, f, _CHUNK)

        _atomic_write(dest, copy)
    return rel


def write_file(rel: str, data: bytes) -> Path:
    """Atomically write a non-content-addressed file (takes, previews, exports)."""
    dest = abs_path(rel)
    _atomic_write(dest, lambda f: f.write(data))
    return dest


def slugify(name: str) -> str:
    """Lowercase, `-` separated ASCII slug."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


# --- media tree layout (see SPEC "Storage") -----------------------------------


def character_images_dir(slug: str) -> str:
    return f"characters/{slug}/images"


def character_voice_dir(slug: str) -> str:
    return f"characters/{slug}/voice"


def location_images_dir(slug: str) -> str:
    return f"locations/{slug}/images"


def series_music_dir(series_slug: str) -> str:
    return f"series/{series_slug}/music"


def episode_dir(series_slug: str, number: int) -> str:
    return f"series/{series_slug}/episodes/{number}"


def snippet_dir(series_slug: str, number: int, snippet_id: int) -> str:
    return f"{episode_dir(series_slug, number)}/snippets/{snippet_id}"


def takes_dir(series_slug: str, number: int, snippet_id: int) -> str:
    return f"{snippet_dir(series_slug, number, snippet_id)}/takes"


def take_paths(series_slug: str, number: int, snippet_id: int, take_id: int) -> dict[str, str]:
    base = f"{takes_dir(series_slug, number, snippet_id)}/{take_id}"
    return {
        "video": f"{base}.mp4",
        "vocals": f"{base}.vocals.wav",
        "fx": f"{base}.fx.wav",
        "poster": f"{base}.poster.jpg",
        "peaks": f"{base}.peaks.json",
    }


def start_frames_dir(series_slug: str, number: int, snippet_id: int) -> str:
    return f"{snippet_dir(series_slug, number, snippet_id)}/start-frames"


def preview_path(series_slug: str, number: int) -> str:
    return f"{episode_dir(series_slug, number)}/preview.mp4"


def exports_dir(series_slug: str, number: int) -> str:
    return f"{episode_dir(series_slug, number)}/exports"


def backup_dir() -> Path:
    return root() / config.BACKUP_DIRNAME
