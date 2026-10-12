"""Voice samples: wav conversion, clipping, waveform peaks and the 2-5s rule.

Samples are content-addressed under `characters/<slug>/voice/<sha12>.wav`.
Audio longer than 5s (an upload or a Chatterbox result) is kept as a clipper
source under `characters/<slug>/voice/sources/<sha12>.wav` so the voice
clipper can pick a range from it; the app never deletes media.
"""

from __future__ import annotations

import array
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import config, db, media
from .library import LibraryError, ffprobe, owner_slug

SAMPLE_RATE = 48_000
TOLERANCE_S = 0.01  # sample-rounding slack on the 2-5s bounds
PEAKS_RATE = 8_000
PEAKS_BUCKETS = 1000


def sources_dir(slug: str) -> str:
    return f"{media.character_voice_dir(slug)}/sources"


def duration(path: Path) -> float:
    info = ffprobe(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            d = s.get("duration") or info.get("format", {}).get("duration")
            if d is not None:
                return float(d)
    raise LibraryError("no audio stream found")


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise LibraryError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")


def to_wav(src: Path, dest: Path, start_s: float | None = None, end_s: float | None = None) -> None:
    """Decode any audio (or video's audio) to mono 16-bit 48k wav, optionally a sample-exact range."""
    af = []
    if start_s is not None or end_s is not None:
        trim = [
            f"start={start_s:.6f}" if start_s is not None else "",
            f"end={end_s:.6f}" if end_s is not None else "",
        ]
        af = ["-af", "atrim=" + ":".join(t for t in trim if t) + ",asetpts=PTS-STARTPTS"]
    _ffmpeg(["-i", str(src), "-vn", *af, "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dest)])


def check_length(seconds: float) -> None:
    lo, hi = config.VOICE_SAMPLE_MIN_SECONDS, config.VOICE_SAMPLE_MAX_SECONDS
    if not (lo - TOLERANCE_S <= seconds <= hi + TOLERANCE_S):
        raise LibraryError(f"voice sample must be {lo:g}-{hi:g}s long (got {seconds:.2f}s)")


def save_voice(
    conn: sqlite3.Connection,
    character_id: int,
    wav: Path,
    source: str,
    *,
    source_take_id: int | None = None,
    clip_start_s: float | None = None,
    clip_end_s: float | None = None,
    tts_voice: str | None = None,
    tts_text: str | None = None,
    gen_request_id: str | None = None,
) -> int:
    """Store a 2-5s wav as the character's voice sample and point the character at it (no commit)."""
    seconds = duration(wav)
    check_length(seconds)
    slug = owner_slug(conn, "character", character_id)
    rel = media.store_file(media.character_voice_dir(slug), wav, "wav")
    sha = media.sha256_file(wav)
    existing = conn.execute(
        "SELECT id FROM character_voices WHERE character_id = ? AND sha256 = ?", (character_id, sha)
    ).fetchone()
    if existing:
        voice_id = existing[0]
    else:
        voice_id = conn.execute(
            "INSERT INTO character_voices (character_id, path, sha256, duration_s, source, source_take_id,"
            " clip_start_s, clip_end_s, tts_voice, tts_text, gen_request_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                character_id,
                rel,
                sha,
                round(seconds, 3),
                source,
                source_take_id,
                clip_start_s,
                clip_end_s,
                tts_voice,
                tts_text,
                gen_request_id,
                db.now(),
            ),
        ).lastrowid
    conn.execute(
        "UPDATE characters SET voice_id = ?, updated_at = ? WHERE id = ?", (voice_id, db.now(), character_id)
    )
    return voice_id


def ingest(
    conn: sqlite3.Connection, character_id: int, src: Path, source: str, **meta: Any
) -> dict[str, Any]:
    """Convert any audio to wav, then save it (2-5s), or stage it for the clipper (>5s).

    Returns {"needs_clip": False, "voice_id"} or {"needs_clip": True, "source": {path, duration_s}}.
    Shorter than 2s is an error. Does not commit.
    """
    slug = owner_slug(conn, "character", character_id)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "voice.wav"
        to_wav(src, wav)
        seconds = duration(wav)
        if seconds > config.VOICE_SAMPLE_MAX_SECONDS + TOLERANCE_S:
            rel = media.store_file(sources_dir(slug), wav, "wav")
            return {"needs_clip": True, "source": {"path": rel, "duration_s": round(seconds, 3)}}
        return {"needs_clip": False, "voice_id": save_voice(conn, character_id, wav, source, **meta)}


def clip(
    conn: sqlite3.Connection,
    character_id: int,
    source_rel: str,
    start_s: float,
    end_s: float,
    source: str,
    **meta: Any,
) -> int:
    """Cut [start_s, end_s] out of a media file and save it as the voice sample (no commit).

    `meta` is provenance passed to `save_voice` (source_take_id, tts_voice, ...).
    """
    if start_s < 0 or end_s <= start_s:
        raise LibraryError("clip range must have 0 <= start < end")
    check_length(end_s - start_s)
    src = media.abs_path(source_rel)
    if not src.is_file():
        raise LibraryError(f"source audio not found: {source_rel}", 404)
    available = duration(src)
    if end_s > available + TOLERANCE_S:
        raise LibraryError(f"clip ends at {end_s:.2f}s but the source is only {available:.2f}s long")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "clip.wav"
        to_wav(src, wav, start_s, end_s)
        return save_voice(
            conn,
            character_id,
            wav,
            source,
            clip_start_s=round(start_s, 3),
            clip_end_s=round(end_s, 3),
            **meta,
        )


def is_clip_source(conn: sqlite3.Connection, rel: str) -> bool:
    """Staged clipper sources, voice samples and take vocals stems may be clipped/peaked."""
    parts = rel.split("/")
    if len(parts) == 5 and parts[0] == "characters" and parts[2:4] == ["voice", "sources"]:
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM character_voices WHERE path = ? UNION SELECT 1 FROM takes WHERE vocals_path = ?",
            (rel, rel),
        ).fetchone()
    )


def peaks(path: Path, buckets: int = PEAKS_BUCKETS) -> dict[str, Any]:
    """Waveform peaks (max |sample| per bucket, 0..1) for the voice clipper."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(PEAKS_RATE),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise LibraryError(f"ffmpeg failed: {proc.stderr.decode(errors='replace').strip()[-300:]}")
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) - len(proc.stdout) % 2])
    n = len(samples)
    seconds = n / PEAKS_RATE
    if n == 0:
        return {"duration_s": 0.0, "peaks": []}
    buckets = max(1, min(buckets, n))
    out: list[float] = []
    for i in range(buckets):
        chunk = samples[i * n // buckets : (i + 1) * n // buckets]
        out.append(round(max(max(chunk), -min(chunk)) / 32768, 4) if chunk else 0.0)
    return {"duration_s": round(seconds, 3), "peaks": out}
