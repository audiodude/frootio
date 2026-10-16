"""Assembly API: settings, prepare (peaks + alignment), trims, preview, export."""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import assembly, db, events, jobs, media, overdub

router = APIRouter(tags=["assembly"])


def _episode(conn, episode_id: int) -> dict[str, Any]:
    try:
        return assembly.episode_context(conn, episode_id)
    except assembly.NotFound:
        raise HTTPException(404, "episode not found") from None


def _url(rel: str | None) -> str | None:
    return media.url(rel) if rel else None


def _item_out(conn, item: assembly.Item) -> dict[str, Any]:
    take = item.take
    take_out = None
    if take is not None:
        take_out = {
            "id": take["id"],
            "status": take["status"],
            "video_url": _url(take.get("video_path")),
            "vocals_url": _url(take.get("vocals_path")),
            "poster_url": _url(take.get("poster_path")),
            "peaks_url": _url(take.get("peaks_path"))
            if take.get("peaks_path") and media.abs_path(take["peaks_path"]).is_file()
            else None,
            "duration_s": item.duration,
            "trim_in_s": item.trim_in,
            "trim_out_s": item.trim_out,
            "trim_out_set": take.get("trim_out_s") is not None,
            "seed": take.get("seed"),
            "audio_muted": bool(take.get("audio_muted")),
            # every non-archived line, incl. queued/failed, so the card can show progress
            "overdubs": [overdub.serialize(o) for o in overdub.list_for_take(conn, take["id"])],
        }
    return {
        "snippet_id": item.snippet_id,
        "index": item.index,
        "script_slice": item.script_slice,
        "take": take_out,
        "problems": item.problems,
        "captions": assembly.caption_state(conn, item),
    }


def _exports(conn, episode_id: int) -> list[dict[str, Any]]:
    out = []
    for r in db.rows(
        conn.execute("SELECT * FROM exports WHERE episode_id = ? ORDER BY id DESC", (episode_id,)).fetchall()
    ):
        r["settings"] = db.loads(r["settings"], {})
        r["captions_on"] = bool(r["captions_on"])
        r["url"] = _url(r["path"]) if r["status"] == "done" else None
        out.append(r)
    return out


@router.get("/episodes/{episode_id}/assembly")
def get_assembly(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    ep = _episode(conn, episode_id)
    items = assembly.load_items(conn, ep)
    tracks = db.rows(
        conn.execute(
            "SELECT id, name, path, duration_s, archived FROM music_tracks WHERE series_id = ?"
            " AND (archived = 0 OR id = ?) ORDER BY name",
            (ep["series_id"], ep["music_track_id"]),
        ).fetchall()
    )
    for t in tracks:
        t["url"] = media.url(t["path"])
        t["archived"] = bool(t["archived"])
    total = sum((it.trim_out or 0) - it.trim_in for it in items if it.trim_out is not None)
    return {
        "episode": {
            "id": ep["id"],
            "number": ep["number"],
            "title": ep["title"],
            "series_id": ep["series_id"],
        },
        "settings": assembly.settings_of(ep),
        "music_tracks": tracks,
        "items": [_item_out(conn, it) for it in items],
        "total_duration_s": round(total, 3),
        "preview": assembly.preview_state(conn, episode_id),
        "exports": _exports(conn, episode_id),
    }


class SettingsPatch(BaseModel):
    music_track_id: int | None = None
    music_offset_s: float | None = Field(None, ge=0)
    music_volume: float | None = Field(None, ge=0, le=2)
    music_duck_db: float | None = Field(None, ge=0, le=30)
    fx_volume: float | None = Field(None, ge=0, le=2)
    captions_on: bool | None = None


@router.patch("/episodes/{episode_id}/assembly/settings")
def patch_settings(episode_id: int, body: SettingsPatch, conn: db.Conn) -> dict[str, Any]:
    ep = _episode(conn, episode_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if key != "music_track_id" and value is None:
            raise HTTPException(422, f"{key} cannot be null")
        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(422, f"{key} must be finite")
    track_id = changes.get("music_track_id", ep["music_track_id"])
    if "music_track_id" in changes and track_id is not None:
        track = conn.execute(
            "SELECT series_id, archived FROM music_tracks WHERE id = ?", (track_id,)
        ).fetchone()
        if track is None or track["series_id"] != ep["series_id"]:
            raise HTTPException(422, "music track is not in this series' library")
        if track["archived"] and track_id != ep["music_track_id"]:
            raise HTTPException(422, "music track is archived")
    if "music_offset_s" in changes and track_id is not None:
        track = assembly.music_track(conn, track_id)
        if track and track.get("duration_s") and changes["music_offset_s"] >= track["duration_s"]:
            raise HTTPException(422, "music offset is past the end of the track")
    if changes:
        if "captions_on" in changes:
            changes["captions_on"] = int(changes["captions_on"])
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(
            f"UPDATE episodes SET {sets}, updated_at = ? WHERE id = ?",
            (*changes.values(), db.now(), episode_id),
        )
        conn.commit()
        events.publish({"type": "assembly", "episode_id": episode_id, "kind": "settings"})
    return assembly.settings_of(_episode(conn, episode_id))


@router.post("/episodes/{episode_id}/assembly/prepare")
def prepare(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    """Waveform peaks + caption alignment for every active snippet's selected take."""
    ep = _episode(conn, episode_id)
    out: list[dict[str, Any]] = []
    for item in assembly.load_items(conn, ep):
        entry: dict[str, Any] = {
            "snippet_id": item.snippet_id,
            "index": item.index,
            "take_id": item.take["id"] if item.take else None,
            "problems": list(item.problems),
            "peaks_path": None,
            "align_job_id": None,
            "captions": None,
        }
        if item.take is not None and "vocals stem missing" not in item.problems:
            try:
                entry["peaks_path"] = assembly.ensure_peaks(conn, item.take, item.paths)  # type: ignore[arg-type]
            except RuntimeError as exc:
                entry["problems"].append(f"waveform failed: {exc}")
            entry["align_job_id"] = assembly.request_alignment(conn, item)
            entry["captions"] = assembly.caption_state(conn, item)["status"]
        out.append(entry)
    missing_takes = [e["index"] for e in out if e["take_id"] is None]
    missing_stems = [
        e["index"] for e in out if any(p in ("vocals stem missing", "fx stem missing") for p in e["problems"])
    ]
    events.publish({"type": "assembly", "episode_id": episode_id, "kind": "prepare"})
    return {"items": out, "missing_selected_takes": missing_takes, "missing_stems": missing_stems}


class TrimBody(BaseModel):
    trim_in_s: float = Field(ge=0)
    trim_out_s: float | None = None


@router.patch("/takes/{take_id}/trim")
def patch_trim(take_id: int, body: TrimBody, conn: db.Conn) -> dict[str, Any]:
    take = db.row(conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone())
    if take is None:
        raise HTTPException(404, "take not found")
    if not take.get("video_path") or not media.abs_path(take["video_path"]).is_file():
        raise HTTPException(409, "take has no video to trim")
    duration = assembly.take_duration(conn, take)
    t_in, t_out = body.trim_in_s, body.trim_out_s
    if not math.isfinite(t_in) or (t_out is not None and not math.isfinite(t_out)):
        raise HTTPException(422, "trim points must be finite")
    end = duration if t_out is None else t_out
    if t_out is not None and t_out > duration + 1e-3:
        raise HTTPException(422, f"trim out is past the end of the video ({duration:.3f}s)")
    if end - t_in < assembly.MIN_TRIM_S:
        raise HTTPException(422, f"trim in must be at least {assembly.MIN_TRIM_S}s before trim out")
    if t_out is not None:
        t_out = min(t_out, duration)
    conn.execute(
        "UPDATE takes SET trim_in_s = ?, trim_out_s = ?, updated_at = ? WHERE id = ?",
        (t_in, t_out, db.now(), take_id),
    )
    conn.commit()
    events.publish({"type": "take", "id": take_id, "snippet_id": take["snippet_id"], "kind": "trim"})
    events.publish({"type": "assembly", "episode_id": take["episode_id"], "kind": "trim", "take_id": take_id})
    return {"id": take_id, "trim_in_s": t_in, "trim_out_s": t_out, "duration_s": duration}


class AudioBody(BaseModel):
    muted: bool


@router.patch("/takes/{take_id}/audio")
def patch_audio(take_id: int, body: AudioBody, conn: db.Conn) -> dict[str, Any]:
    """Strip (or restore) all of a take's own audio, vocals and fx, in assembly."""
    take = db.row(conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone())
    if take is None:
        raise HTTPException(404, "take not found")
    conn.execute(
        "UPDATE takes SET audio_muted = ?, updated_at = ? WHERE id = ?",
        (int(body.muted), db.now(), take_id),
    )
    conn.commit()
    events.publish({"type": "take", "id": take_id, "snippet_id": take["snippet_id"], "kind": "audio"})
    events.publish(
        {"type": "assembly", "episode_id": take["episode_id"], "kind": "audio", "take_id": take_id}
    )
    return {"id": take_id, "audio_muted": body.muted}



class CaptionWordIn(BaseModel):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    line: int = Field(ge=0)


class CaptionsPutBody(BaseModel):
    words: list[CaptionWordIn]


@router.put("/snippets/{snippet_id}/captions")
@router.post("/snippets/{snippet_id}/captions")
def put_snippet_captions(snippet_id: int, body: CaptionsPutBody, conn: db.Conn) -> dict[str, Any]:
    words = []
    for w in body.words:
        if not math.isfinite(w.start) or not math.isfinite(w.end):
            raise HTTPException(422, "word start and end must be finite")
        if w.end < w.start:
            raise HTTPException(422, f"word {w.word!r} end ({w.end}) is before start ({w.start})")
        words.append(
            {
                "word": w.word,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "line": w.line,
            }
        )
    try:
        return assembly.update_alignment(conn, snippet_id, words)
    except assembly.NotFound as exc:
        raise HTTPException(404, str(exc)) from None


@router.post("/snippets/{snippet_id}/captions/realign")
def realign_snippet_captions(snippet_id: int, conn: db.Conn) -> dict[str, Any]:
    try:
        job_id = assembly.reset_alignment(conn, snippet_id)
        return {"job_id": job_id}
    except assembly.NotFound as exc:
        raise HTTPException(404, str(exc)) from None

def _spec_or_409(conn, episode_id: int, captions_on: bool | None = None) -> assembly.RenderSpec:
    try:
        return assembly.build_spec(conn, episode_id, captions_on=captions_on)
    except assembly.NotReady as exc:
        raise HTTPException(409, {"message": "assembly is not ready", "problems": exc.problems}) from None


@router.post("/episodes/{episode_id}/assembly/preview")
def render_preview(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    ep = _episode(conn, episode_id)
    spec = _spec_or_409(conn, episode_id)
    if (
        spec.hash is not None
        and spec.hash == ep["preview_hash"]
        and ep["preview_path"]
        and media.abs_path(ep["preview_path"]).is_file()
    ):
        return {"reused": True, "job_id": None, "url": media.url(ep["preview_path"])}
    job_id = assembly._pending_job(conn, assembly.PREVIEW_JOB, episode_id=episode_id)
    if job_id is None:
        job_id = jobs.enqueue(conn, assembly.PREVIEW_JOB, {"episode_id": episode_id})
    return {"reused": False, "job_id": job_id, "url": None}


class ExportBody(BaseModel):
    captions_on: bool | None = None


@router.post("/episodes/{episode_id}/assembly/export", status_code=201)
def start_export(episode_id: int, conn: db.Conn, body: ExportBody | None = None) -> dict[str, Any]:
    ep = _episode(conn, episode_id)
    captions_on = bool(ep["captions_on"]) if body is None or body.captions_on is None else body.captions_on
    spec = _spec_or_409(conn, episode_id, captions_on)
    ts = db.now()
    export_id = conn.execute(
        "INSERT INTO exports (episode_id, settings, captions_on, status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'queued', ?, ?)",
        (episode_id, db.dumps(assembly.snapshot(spec)), int(captions_on), ts, ts),
    ).lastrowid
    conn.commit()
    job_id = jobs.enqueue(
        conn,
        assembly.EXPORT_JOB,
        {"episode_id": episode_id, "export_id": export_id, "captions_on": captions_on},
    )
    conn.execute("UPDATE exports SET job_id = ? WHERE id = ?", (job_id, export_id))
    conn.commit()
    events.publish(
        {
            "type": "assembly",
            "episode_id": episode_id,
            "kind": "export",
            "status": "queued",
            "export_id": export_id,
            "job_id": job_id,
        }
    )
    return {"export_id": export_id, "job_id": job_id}


@router.get("/episodes/{episode_id}/assembly/exports")
def list_exports(episode_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    _episode(conn, episode_id)
    return _exports(conn, episode_id)
