"""Overdubs: single dialogue lines generated for fixing in post.

A line is Chatterbox HD speech in the character's cloned voice (its current voice
sample is sent as `audio_url`), placed on a take at `offset_s` (take time, untrimmed)
and mixed into that take's vocals in assembly, so music ducks under it and caption
alignment hears it. The take's own audio is left as is underneath.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, db, events, falapi, jobs, media, paidgen, spend, voice
from .paidgen import tts_cost

log = logging.getLogger(__name__)

JOB = "overdub_tts"
MAX_CHARS = 300
MAX_VOLUME = 2.0


class OverdubError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class Placed:
    """A finished overdub, ready to mix: `offset` is in the take's own time."""

    path: Path
    offset: float
    volume: float
    duration: float
    replace_voice: bool = False  # punch-in: silence the take's vocals under this line


PUNCH_FADE_S = 0.05


def voice_gain_filter(lines: tuple[Placed, ...], shift: float = 0.0) -> str:
    """Filter fragment (leading comma, or "") that silences a vocals stem under punch-in lines.

    `shift` is subtracted from the line offsets: 0 for take time, trim_in for a trimmed clip.
    Fully silent over the line, fading over PUNCH_FADE_S either side so the cut doesn't click.
    """
    spans = [(p.offset - shift, p.offset - shift + p.duration) for p in lines if p.replace_voice]
    if not spans:
        return ""
    f = PUNCH_FADE_S
    gain = "*".join(f"(1-clip(min((t-{a:.3f}+{f})/{f}\\,({b:.3f}+{f}-t)/{f})\\,0\\,1))" for a, b in spans)
    # Small frames so the per-frame gain steps are short enough to sound like a fade.
    return f",asetnsamples=n=240,volume='{gain}':eval=frame"


def _row(conn, overdub_id: int) -> dict[str, Any]:
    row = db.row(conn.execute("SELECT * FROM overdubs WHERE id = ?", (overdub_id,)).fetchone())
    if row is None or row["archived"]:
        raise OverdubError(404, "overdub not found")
    return row


def list_for_take(conn, take_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT o.*, c.name AS character_name FROM overdubs o JOIN characters c ON c.id = o.character_id"
        " WHERE o.take_id = ? AND o.archived = 0 ORDER BY o.offset_s, o.id",
        (take_id,),
    ).fetchall()
    return [db.row(r) for r in rows]  # type: ignore[misc]


def active(conn, take_id: int) -> list[dict[str, Any]]:
    """Finished, non-archived overdubs whose audio file exists: what assembly mixes in."""
    return [
        o
        for o in list_for_take(conn, take_id)
        if o["status"] == "done" and o["audio_path"] and media.abs_path(o["audio_path"]).is_file()
    ]


def placed(rows: list[dict[str, Any]]) -> tuple[Placed, ...]:
    return tuple(
        Placed(
            media.abs_path(o["audio_path"]),
            float(o["offset_s"]),
            float(o["volume"]),
            float(o["duration_s"]),
            bool(o["replace_voice"]),
        )
        for o in rows
    )


def signature(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Stable identity of the mixed-in lines, for caption and preview cache keys."""
    return [
        [
            o["audio_path"],
            round(float(o["offset_s"]), 3),
            round(float(o["volume"]), 3),
            bool(o["replace_voice"]),
        ]
        for o in rows
    ]


def serialize(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": o["id"],
        "take_id": o["take_id"],
        "character_id": o["character_id"],
        "character_name": o.get("character_name"),
        "text": o["text"],
        "exaggeration": o["exaggeration"],
        "offset_s": o["offset_s"],
        "volume": o["volume"],
        "replace_voice": bool(o["replace_voice"]),
        "status": o["status"],
        "error": o["error"],
        "audio_url": media.url(o["audio_path"]) if o["audio_path"] else None,
        "duration_s": o["duration_s"],
        "cost_usd": o["cost_usd"],
        "estimated_cost_usd": tts_cost(o["text"]),
    }


def _take(conn, take_id: int) -> dict[str, Any]:
    take = db.row(conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone())
    if take is None:
        raise OverdubError(404, "take not found")
    return take


def _check_placement(take: dict[str, Any], offset_s: float, volume: float) -> None:
    if offset_s < 0:
        raise OverdubError(422, "offset must be ≥ 0")
    duration = take.get("video_duration_s")
    if duration is not None and offset_s >= float(duration):
        raise OverdubError(422, f"offset is past the end of the take ({float(duration):.2f}s)")
    if not 0 <= volume <= MAX_VOLUME:
        raise OverdubError(422, f"volume must be between 0 and {MAX_VOLUME}")


def create(
    conn, take_id: int, *, character_id: int, text: str, exaggeration: float = 0.5, offset_s: float = 0.0
) -> dict[str, Any]:
    take = _take(conn, take_id)
    text = text.strip()
    if not text:
        raise OverdubError(422, "the line is empty")
    if len(text) > MAX_CHARS:
        raise OverdubError(422, f"keep it to one line (≤ {MAX_CHARS} characters)")
    if not 0.25 <= exaggeration <= 2.0:
        raise OverdubError(422, "exaggeration must be between 0.25 and 2.0")
    _check_placement(take, offset_s, 1.0)
    char = conn.execute(
        "SELECT id, name, archived, voice_id FROM characters WHERE id = ?", (character_id,)
    ).fetchone()
    if char is None:
        raise OverdubError(404, "character not found")
    if char["voice_id"] is None:
        raise OverdubError(409, f"{char['name']} has no voice sample to clone; add one on the character page")
    if not config.settings().fal_key:
        raise OverdubError(503, "FAL_KEY is not set")
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO overdubs (take_id, character_id, text, exaggeration, voice_id, offset_s,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (take_id, character_id, text, exaggeration, char["voice_id"], offset_s, ts, ts),
    )
    overdub_id = cur.lastrowid
    job_id = jobs.enqueue(conn, JOB, {"overdub_id": overdub_id})
    conn.execute("UPDATE overdubs SET job_id = ? WHERE id = ?", (job_id, overdub_id))
    conn.commit()
    _publish(take, overdub_id)
    return serialize(_get(conn, overdub_id))


def _get(conn, overdub_id: int) -> dict[str, Any]:
    row = db.row(
        conn.execute(
            "SELECT o.*, c.name AS character_name FROM overdubs o JOIN characters c ON c.id = o.character_id"
            " WHERE o.id = ?",
            (overdub_id,),
        ).fetchone()
    )
    assert row is not None
    return row


def update(
    conn,
    overdub_id: int,
    *,
    offset_s: float | None = None,
    volume: float | None = None,
    replace_voice: bool | None = None,
) -> dict[str, Any]:
    o = _row(conn, overdub_id)
    take = _take(conn, o["take_id"])
    new_offset = o["offset_s"] if offset_s is None else offset_s
    new_volume = o["volume"] if volume is None else volume
    new_replace = o["replace_voice"] if replace_voice is None else int(replace_voice)
    _check_placement(take, float(new_offset), float(new_volume))
    conn.execute(
        "UPDATE overdubs SET offset_s = ?, volume = ?, replace_voice = ?, updated_at = ? WHERE id = ?",
        (new_offset, new_volume, new_replace, db.now(), overdub_id),
    )
    conn.commit()
    _publish(take, overdub_id)
    return serialize(_get(conn, overdub_id))


def archive(conn, overdub_id: int) -> None:
    o = _row(conn, overdub_id)
    conn.execute("UPDATE overdubs SET archived = 1, updated_at = ? WHERE id = ?", (db.now(), overdub_id))
    conn.commit()
    _publish(_take(conn, o["take_id"]), overdub_id)


def _publish(take: dict[str, Any], overdub_id: int) -> None:
    events.publish({"type": "take", "id": take["id"], "snippet_id": take["snippet_id"], "kind": "overdub"})
    events.publish(
        {"type": "assembly", "episode_id": take["episode_id"], "kind": "overdub", "overdub_id": overdub_id}
    )


def _set(overdub_id: int, **cols: Any) -> dict[str, Any]:
    conn = db.connect()
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE overdubs SET {sets}, updated_at = ? WHERE id = ?", (*cols.values(), db.now(), overdub_id)
        )
        conn.commit()
        o = db.row(conn.execute("SELECT * FROM overdubs WHERE id = ?", (overdub_id,)).fetchone())
        take = _take(conn, o["take_id"])  # type: ignore[index]
    finally:
        conn.close()
    _publish(take, overdub_id)
    return o  # type: ignore[return-value]


def _log_spend_once(job_id: int, o: dict[str, Any], take: dict[str, Any], amount: float) -> None:
    tag = f"(overdub job {job_id})"
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM spend WHERE note LIKE ?", (f"%{tag}",)).fetchone():
            return
        spend.log(
            conn,
            "tts",
            amount,
            episode_id=take["episode_id"],
            character_id=o["character_id"],
            take_id=take["id"],
            note=f"overdub line {tag}",
        )
    finally:
        conn.close()


@jobs.handler(JOB)
async def _tts_job(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    overdub_id = payload["overdub_id"]
    conn = db.connect()
    try:
        o = db.row(conn.execute("SELECT * FROM overdubs WHERE id = ?", (overdub_id,)).fetchone())
        if o is None:
            raise RuntimeError(f"overdub {overdub_id} not found")
        take = _take(conn, o["take_id"])
        ep = db.row(
            conn.execute(
                "SELECT e.number, s.slug FROM episodes e JOIN series s ON s.id = e.series_id WHERE e.id = ?",
                (take["episode_id"],),
            ).fetchone()
        )
        sample = conn.execute("SELECT path FROM character_voices WHERE id = ?", (o["voice_id"],)).fetchone()
    finally:
        conn.close()
    try:

        async def make_arguments() -> dict[str, Any]:
            return {
                "text": o["text"],
                "exaggeration": o["exaggeration"],
                "audio_url": await falapi.upload_file(media.abs_path(sample["path"])),
                "high_quality_audio": True,
            }

        if sample is None:
            raise RuntimeError("the character's voice sample is gone")
        request_id, result = await paidgen.run_fal(job_id, config.CHATTERBOX_ENDPOINT, make_arguments)
        cost = tts_cost(o["text"])
        _log_spend_once(job_id, o, take, cost)
        data = await paidgen.download(result["audio"]["url"])

        def save() -> tuple[str, float]:
            with tempfile.TemporaryDirectory(prefix="frootio-overdub-") as tmp:
                src, wav = Path(tmp) / "line.audio", Path(tmp) / "line.wav"
                src.write_bytes(data)
                voice.to_wav(src, wav)
                rel = media.store_file(
                    f"{media.snippet_dir(ep['slug'], ep['number'], take['snippet_id'])}/overdubs", wav
                )
                return rel, voice.duration(wav)

        rel, duration = await asyncio.to_thread(save)
        _set(overdub_id, status="done", error=None, audio_path=rel, duration_s=duration, cost_usd=cost)
        return {"request_id": request_id, "audio_path": rel, "cost_usd": cost}
    except Exception as exc:
        _set(overdub_id, status="failed", error=str(exc))
        raise


def mix_vocals(vocals: Path, lines: tuple[Placed, ...], out: Path, *, mute_base: bool = False) -> None:
    """The take's vocals stem with its overdubs mixed in (same timeline), for caption alignment.

    `mute_base` silences the take's own vocals (the take's audio is stripped in assembly).
    """
    inputs = ["-i", str(vocals)]
    chains = [f"[0:a]volume={0 if mute_base else 1}{voice_gain_filter(lines)}[base]"]
    labels = "[base]"
    for k, p in enumerate(lines, start=1):
        inputs += ["-i", str(p.path)]
        ms = int(round(p.offset * 1000))
        chains.append(f"[{k}:a]volume={p.volume},adelay=delays={ms}:all=1[o{k}]")
        labels += f"[o{k}]"
    chains.append(f"{labels}amix=inputs={len(lines) + 1}:duration=first:normalize=0[a]")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chains),
         "-map", "[a]", str(out)],
        check=True,
        capture_output=True,
    )  # fmt: skip
