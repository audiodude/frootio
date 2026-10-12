"""Episode assembly: trims, waveform peaks, caption alignment cache, ffmpeg render.

One filter graph builder (`build_graph`) serves both the preview and the export
(SPEC "Assembly"): every selected take is trimmed (video, vocals and fx stems
identically) and concatenated; the fx stems play at the episode fx volume; the
music track starts at its offset, at its volume, and is ducked under the
concatenated vocals with `sidechaincompress`; the three are mixed and the ASS
captions are burned in with libass when captions are on.

Ducking: `music_duck_db` is how far the music drops while someone speaks. The
compressor's threshold is fixed at -40 dBFS (`DUCK_THRESHOLD`) and dialogue in a
vocals stem sits around 20 dB above it (RMS detection), so the ratio that gives
D dB of gain reduction is `20 / (20 - D)` (ffmpeg caps the ratio at 20, i.e.
about 19 dB). 0 dB means no ducking (ratio 1).

Preview renders a fast, low-quality `preview.mp4` and is reused until the hash
of its inputs changes (settings, takes, trims, stem files, alignment words).
Export renders the final 768x1344 MP4 to `exports/<timestamp>.mp4`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import captions, db, events, jobs, media, overdub

log = logging.getLogger(__name__)

OUT_W, OUT_H = 768, 1344
OUT_FPS = 24
AUDIO_RATE = 48_000
PREVIEW_W, PREVIEW_H = 432, 756
RENDER_VERSION = 1  # bump when the graph or caption style changes so old previews re-render

DUCK_THRESHOLD = 0.01  # -40 dBFS
DUCK_ASSUMED_OVER_DB = 20.0  # typical dialogue RMS above the threshold
DUCK_MAX_RATIO = 20.0  # ffmpeg sidechaincompress limit
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 400

PEAKS_PER_SECOND = 50
MIN_TRIM_S = 0.2

ALIGN_JOB = "caption_align"
PREVIEW_JOB = "assembly_preview"
EXPORT_JOB = "assembly_export"


# --- probing ---------------------------------------------------------------------------


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe could not read the duration of {path}") from None


def _file_sig(path: Path) -> list[Any]:
    st = path.stat()
    return [str(path), st.st_size, st.st_mtime_ns]


# --- ducking + filter graph --------------------------------------------------------------


def duck_params(duck_db: float) -> dict[str, float]:
    """sidechaincompress parameters for ducking the music by about `duck_db` dB."""
    d = max(0.0, float(duck_db))
    if d <= 0:
        ratio = 1.0
    elif d >= DUCK_ASSUMED_OVER_DB * (1 - 1 / DUCK_MAX_RATIO):
        ratio = DUCK_MAX_RATIO
    else:
        ratio = DUCK_ASSUMED_OVER_DB / (DUCK_ASSUMED_OVER_DB - d)
    return {
        "threshold": DUCK_THRESHOLD,
        "ratio": round(min(DUCK_MAX_RATIO, ratio), 3),
        "attack": DUCK_ATTACK_MS,
        "release": DUCK_RELEASE_MS,
    }


@dataclass(frozen=True)
class Clip:
    video: Path
    vocals: Path
    fx: Path
    trim_in: float
    trim_out: float
    muted: bool = False  # drop this take's vocals and fx; music plays unducked over it
    overdubs: tuple[overdub.Placed, ...] = ()  # lines mixed into this clip's vocals

    @property
    def duration(self) -> float:
        return self.trim_out - self.trim_in


@dataclass(frozen=True)
class Music:
    path: Path
    offset: float
    volume: float
    duck_db: float


def _f(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") if x else "0"


def build_graph(
    clips: list[Clip],
    *,
    fx_volume: float,
    music: Music | None,
    ass_file: str | None,
    out_size: tuple[int, int] = (OUT_W, OUT_H),
) -> tuple[list[str], str]:
    """Input args and `-filter_complex` graph; outputs are labelled [vout] and [aout].

    Inputs are, per clip, video, vocals, fx and then that clip's overdub lines, then
    the music track (seeked to its offset with `-ss`). `ass_file` is a path relative
    to ffmpeg's working directory (no filter escaping needed).
    """
    if not clips:
        raise ValueError("nothing to assemble")
    inputs: list[str] = []
    chains: list[str] = []
    concat_in = ""
    norm_audio = f"aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
    n = 0  # next input index
    for i, c in enumerate(clips):
        v_in, voc_in, fx_in = n, n + 1, n + 2
        n += 3
        inputs += ["-i", str(c.video), "-i", str(c.vocals), "-i", str(c.fx)]
        trim = f"start={_f(c.trim_in)}:end={_f(c.trim_out)}"
        dur = _f(c.duration)
        chains.append(
            f"[{v_in}:v]trim={trim},setpts=PTS-STARTPTS,"
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"setsar=1,fps={OUT_FPS},format=yuv420p[v{i}]"
        )
        # A muted clip keeps its inputs but contributes silence, which also leaves the
        # music unducked over it. Overdubs are not part of the take's audio: they still play.
        mute = ",volume=0" if c.muted else ""
        lines = [p for p in c.overdubs if p.offset < c.trim_out and p.offset + p.duration > c.trim_in]
        voc_out = f"vocbase{i}" if lines else f"voc{i}"
        # Punch-in lines silence the take's own voice under them (fx untouched).
        punch = overdub.voice_gain_filter(tuple(lines), shift=c.trim_in)
        for k, name, extra in ((voc_in, voc_out, punch), (fx_in, f"fx{i}", "")):
            chains.append(
                f"[{k}:a]atrim={trim},asetpts=PTS-STARTPTS,{norm_audio}{mute}{extra},"
                f"apad=whole_dur={dur},atrim=end={dur}[{name}]"
            )
        if lines:
            mix = f"[{voc_out}]"
            for j, p in enumerate(lines):
                inputs += ["-i", str(p.path)]
                rel = p.offset - c.trim_in
                place = (
                    f"adelay=delays={round(rel * 1000)}:all=1"
                    if rel >= 0
                    else f"atrim=start={_f(-rel)},asetpts=PTS-STARTPTS"
                )
                chains.append(f"[{n}:a]{norm_audio},volume={_f(p.volume)},{place}[od{i}_{j}]")
                mix += f"[od{i}_{j}]"
                n += 1
            chains.append(
                f"{mix}amix=inputs={len(lines) + 1}:duration=first:normalize=0,atrim=end={dur}[voc{i}]"
            )
        concat_in += f"[v{i}][voc{i}][fx{i}]"
    chains.append(f"{concat_in}concat=n={len(clips)}:v=1:a=2[vcat][voccat][fxcat]")
    chains.append(f"[fxcat]volume={_f(fx_volume)}[fxv]")
    if music is not None:
        inputs += ["-ss", _f(music.offset), "-i", str(music.path)]
        m = n
        p = duck_params(music.duck_db)
        chains.append("[voccat]asplit=2[vocmix][vocsc]")
        chains.append(f"[{m}:a]{norm_audio},volume={_f(music.volume)}[mus]")
        chains.append(
            f"[mus][vocsc]sidechaincompress=threshold={_f(p['threshold'])}:ratio={_f(p['ratio'])}"
            f":attack={_f(p['attack'])}:release={_f(p['release'])}[ducked]"
        )
        chains.append("[vocmix][fxv][ducked]amix=inputs=3:duration=first:normalize=0[amixed]")
    else:
        chains.append("[voccat][fxv]amix=inputs=2:duration=first:normalize=0[amixed]")
    chains.append("[amixed]alimiter=limit=0.95:level=disabled[aout]")
    video = "[vcat]"
    tail: list[str] = []
    if ass_file is not None:
        tail.append(f"ass=filename={ass_file}")
    if out_size != (OUT_W, OUT_H):
        tail.append(f"scale={out_size[0]}:{out_size[1]}")
    if tail:
        chains.append(f"{video}{','.join(tail)}[vout]")
    else:
        chains.append(f"{video}null[vout]")
    return inputs, ";".join(chains)


def ffmpeg_command(
    clips: list[Clip],
    output: Path,
    *,
    quality: str,
    fx_volume: float,
    music: Music | None,
    ass_file: str | None,
) -> list[str]:
    if quality == "preview":
        size, codec = (PREVIEW_W, PREVIEW_H), ["-preset", "ultrafast", "-crf", "30", "-b:a", "96k"]
    elif quality == "export":
        size, codec = (OUT_W, OUT_H), ["-preset", "medium", "-crf", "18", "-b:a", "192k"]
    else:
        raise ValueError(f"unknown quality {quality!r}")
    inputs, graph = build_graph(clips, fx_volume=fx_volume, music=music, ass_file=ass_file, out_size=size)
    total = sum(c.duration for c in clips)
    return [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
        *inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(OUT_FPS),
        "-c:a", "aac", "-ar", str(AUDIO_RATE),
        *codec,
        "-t", _f(total),
        "-movflags", "+faststart",
        str(output),
    ]  # fmt: skip


# --- waveform peaks ----------------------------------------------------------------------


def compute_peaks(path: Path, per_second: int = PEAKS_PER_SECOND) -> dict[str, Any]:
    """Max-abs peaks (0..1, normalised to the loudest bucket) at `per_second` buckets/s."""
    sr = 8000
    samples = captions.load_audio(str(path), sr)
    hop = sr // per_second
    n = -(-len(samples) // hop) if len(samples) else 0
    padded = np.zeros(n * hop, dtype=np.float32)
    padded[: len(samples)] = np.abs(samples)
    peaks = padded.reshape(n, hop).max(axis=1) if n else np.zeros(0, dtype=np.float32)
    top = float(peaks.max()) if n else 0.0
    norm = peaks / top if top > 0 else peaks
    return {
        "per_second": per_second,
        "duration": round(len(samples) / sr, 3),
        "peaks": [round(float(p), 3) for p in norm],
    }


def ensure_peaks(conn, take: dict[str, Any], paths: dict[str, str]) -> str:
    """Peaks JSON for a take's vocals stem; recomputed only when the stem file changes."""
    vocals = media.abs_path(take["vocals_path"])
    sig = _file_sig(vocals)[1:]
    rel = take.get("peaks_path") or paths["peaks"]
    dest = media.abs_path(rel)
    if dest.exists():
        try:
            cached = json.loads(dest.read_text())
            if cached.get("source") == [take["vocals_path"], *sig]:
                if take.get("peaks_path") != rel:
                    _set_peaks_path(conn, take["id"], rel)
                return rel
        except (OSError, ValueError):
            pass
    data = compute_peaks(vocals)
    data["source"] = [take["vocals_path"], *sig]
    media.write_file(rel, json.dumps(data, separators=(",", ":")).encode())
    _set_peaks_path(conn, take["id"], rel)
    return rel


def _set_peaks_path(conn, take_id: int, rel: str) -> None:
    conn.execute("UPDATE takes SET peaks_path = ? WHERE id = ?", (rel, take_id))
    conn.commit()


# --- loading the episode's assembly state ----------------------------------------------


class NotFound(LookupError):
    pass


class NotReady(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Item:
    snippet_id: int
    position: int
    index: int  # 1-based order on the board
    script_slice: str
    lines: list[str]
    caption_hash: str
    take: dict[str, Any] | None
    paths: dict[str, str] | None  # canonical take paths (for peaks)
    duration: float | None = None
    trim_in: float = 0.0
    trim_out: float | None = None
    problems: list[str] = field(default_factory=list)
    overdubs: list[dict[str, Any]] = field(default_factory=list)  # finished lines on the take

    @property
    def label(self) -> str:
        return f"snippet {self.index} (#{self.snippet_id})"


def episode_context(conn, episode_id: int) -> dict[str, Any]:
    ep = db.row(
        conn.execute(
            "SELECT e.*, s.slug AS series_slug FROM episodes e JOIN series s ON s.id = e.series_id"
            " WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
    )
    if ep is None:
        raise NotFound("episode not found")
    return ep


def take_duration(conn, take: dict[str, Any]) -> float:
    """Video duration (from the DB, else ffprobe, cached into takes.video_duration_s)."""
    if take.get("video_duration_s"):
        return float(take["video_duration_s"])
    dur = probe_duration(media.abs_path(take["video_path"]))
    conn.execute("UPDATE takes SET video_duration_s = ? WHERE id = ?", (dur, take["id"]))
    conn.commit()
    take["video_duration_s"] = dur
    return dur


def effective_trims(take: dict[str, Any], duration: float) -> tuple[float, float]:
    t_in = min(max(0.0, float(take.get("trim_in_s") or 0.0)), duration)
    t_out = duration if take.get("trim_out_s") is None else min(float(take["trim_out_s"]), duration)
    return t_in, max(t_in, t_out)


def load_items(conn, episode: dict[str, Any]) -> list[Item]:
    """Active snippets in board order with their selected take and any blocking problems."""
    snippets = conn.execute(
        "SELECT id, position, script_slice, selected_take_id FROM snippets"
        " WHERE episode_id = ? AND archived = 0 ORDER BY position, id",
        (episode["id"],),
    ).fetchall()
    items: list[Item] = []
    for index, s in enumerate(snippets, start=1):
        lines = captions.caption_lines(s["script_slice"])
        take = None
        if s["selected_take_id"] is not None:
            take = db.row(
                conn.execute("SELECT * FROM takes WHERE id = ?", (s["selected_take_id"],)).fetchone()
            )
        lines_on_take = overdub.active(conn, take["id"]) if take is not None else []
        item = Item(
            snippet_id=s["id"],
            position=s["position"],
            index=index,
            script_slice=s["script_slice"],
            lines=lines,
            caption_hash=caption_key(lines, take, lines_on_take),
            take=take,
            paths=None,
            overdubs=lines_on_take,
        )
        items.append(item)
        if take is None:
            item.problems.append("no selected take")
            continue
        item.paths = media.take_paths(episode["series_slug"], episode["number"], s["id"], take["id"])
        if take["status"] != "done":
            item.problems.append(f"selected take is {take['status']}")
        for col, what in (("video_path", "video"), ("vocals_path", "vocals stem"), ("fx_path", "fx stem")):
            if not take.get(col) or not media.abs_path(take[col]).is_file():
                item.problems.append(f"{what} missing")
        if take.get("video_path") and media.abs_path(take["video_path"]).is_file():
            item.duration = take_duration(conn, take)
            item.trim_in, item.trim_out = effective_trims(take, item.duration)
    return items


def caption_key(lines: list[str], take: dict[str, Any] | None, lines_on_take: list[dict[str, Any]]) -> str:
    """Alignment cache key: the caption text, plus the overdubs it was aligned against.

    Takes without overdubs keep the plain text hash, so their cached alignments stay valid.
    """
    text = "\n".join(lines)
    if not lines_on_take:
        return captions.text_hash(text)
    audio = {"muted": bool(take and take.get("audio_muted")), "overdubs": overdub.signature(lines_on_take)}
    return captions.text_hash(text + "\n#overdubs " + json.dumps(audio, sort_keys=True))


def alignment_row(conn, take_id: int, text_hash: str) -> dict[str, Any] | None:
    r = db.row(
        conn.execute(
            "SELECT * FROM caption_alignments WHERE take_id = ? AND slice_sha256 = ?", (take_id, text_hash)
        ).fetchone()
    )
    if r is not None:
        r["words"] = db.loads(r["words"], [])
    return r


def store_alignment(conn, take_id: int, text_hash: str, text: str, words: list, method: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO caption_alignments (take_id, slice_sha256, caption_text, words, method,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (take_id, text_hash, text, db.dumps(words), method, db.now()),
    )
    conn.commit()


def _pending_job(conn, kind: str, **match: Any) -> int | None:
    sql = "SELECT id FROM jobs WHERE kind = ? AND status IN ('queued', 'running')"
    params: list[Any] = [kind]
    for key, value in match.items():
        sql += f" AND json_extract(payload, '$.{key}') = ?"
        params.append(value)
    r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return r[0] if r else None


def _last_failed_job(conn, kind: str, **match: Any) -> dict[str, Any] | None:
    sql = "SELECT id, error FROM jobs WHERE kind = ?"
    params: list[Any] = [kind]
    for key, value in match.items():
        sql += f" AND json_extract(payload, '$.{key}') = ?"
        params.append(value)
    r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return {"id": r[0], "error": r[1]} if r and r[1] else None


def caption_state(conn, item: Item) -> dict[str, Any]:
    """Caption preview state for one item: lines, status and the aligned words."""
    out: dict[str, Any] = {
        "lines": item.lines,
        "status": "unavailable",
        "method": None,
        "words": [],
        "error": None,
        "job_id": None,
    }
    if item.take is None:
        return out
    row = alignment_row(conn, item.take["id"], item.caption_hash)
    if row is not None:
        out.update(status="ready", method=row["method"], words=row["words"])
        return out
    if "vocals stem missing" in item.problems:
        return out
    job_id = _pending_job(conn, ALIGN_JOB, take_id=item.take["id"], slice_sha256=item.caption_hash)
    if job_id is not None:
        out.update(status="pending", job_id=job_id)
        return out
    failed = _last_failed_job(conn, ALIGN_JOB, take_id=item.take["id"], slice_sha256=item.caption_hash)
    out.update(status="failed" if failed else "missing", error=failed["error"] if failed else None)
    return out


# --- settings ------------------------------------------------------------------------------

SETTINGS_FIELDS = (
    "music_track_id",
    "music_offset_s",
    "music_volume",
    "music_duck_db",
    "fx_volume",
    "captions_on",
)


def settings_of(episode: dict[str, Any]) -> dict[str, Any]:
    out = {k: episode[k] for k in SETTINGS_FIELDS}
    out["captions_on"] = bool(out["captions_on"])
    return out


def music_track(conn, track_id: int | None) -> dict[str, Any] | None:
    if track_id is None:
        return None
    track = db.row(conn.execute("SELECT * FROM music_tracks WHERE id = ?", (track_id,)).fetchone())
    if track is not None and track.get("duration_s") is None:
        p = media.abs_path(track["path"])
        if p.is_file():
            track["duration_s"] = probe_duration(p)
            conn.execute(
                "UPDATE music_tracks SET duration_s = ? WHERE id = ?", (track["duration_s"], track_id)
            )
            conn.commit()
    return track


# --- render spec + hash ------------------------------------------------------------------


@dataclass
class RenderSpec:
    episode: dict[str, Any]
    items: list[Item]
    clips: list[Clip]
    music: Music | None
    fx_volume: float
    captions_on: bool
    captions: list[captions.TakeCaptions]
    missing_alignments: list[Item]
    total_duration: float
    hash: str | None


def build_spec(conn, episode_id: int, *, captions_on: bool | None = None) -> RenderSpec:
    """Everything a render needs. Raises NotReady listing blocking problems."""
    ep = episode_context(conn, episode_id)
    items = load_items(conn, ep)
    problems = [f"{it.label}: {p}" for it in items for p in it.problems]
    if not items:
        problems.append("no active snippets")
    track = music_track(conn, ep["music_track_id"])
    if ep["music_track_id"] is not None and (track is None or not media.abs_path(track["path"]).is_file()):
        problems.append("music track file missing")
    if problems:
        raise NotReady(problems)
    for it in items:
        assert it.trim_out is not None
        if it.trim_out - it.trim_in < MIN_TRIM_S:
            problems.append(f"{it.label}: trimmed to under {MIN_TRIM_S}s")
    if problems:
        raise NotReady(problems)

    caps_on = bool(ep["captions_on"]) if captions_on is None else captions_on
    clips = [
        Clip(
            video=media.abs_path(it.take["video_path"]),  # type: ignore[index]
            vocals=media.abs_path(it.take["vocals_path"]),  # type: ignore[index]
            fx=media.abs_path(it.take["fx_path"]),  # type: ignore[index]
            trim_in=it.trim_in,
            trim_out=it.trim_out,  # type: ignore[arg-type]
            muted=bool(it.take.get("audio_muted")),  # type: ignore[union-attr]
            overdubs=overdub.placed(it.overdubs),
        )
        for it in items
    ]
    music = None
    if track is not None:
        music = Music(
            media.abs_path(track["path"]),
            float(ep["music_offset_s"]),
            float(ep["music_volume"]),
            float(ep["music_duck_db"]),
        )
    caps: list[captions.TakeCaptions] = []
    missing: list[Item] = []
    offset = 0.0
    for it, clip in zip(items, clips, strict=True):
        if caps_on:
            row = alignment_row(conn, it.take["id"], it.caption_hash)  # type: ignore[index]
            if row is None:
                missing.append(it)
            else:
                caps.append(captions.TakeCaptions(row["words"], clip.trim_in, clip.trim_out, offset))
        offset += clip.duration
    spec = RenderSpec(ep, items, clips, music, float(ep["fx_volume"]), caps_on, caps, missing, offset, None)
    if not missing:
        spec.hash = _spec_hash(spec)
    return spec


def _spec_hash(spec: RenderSpec) -> str:
    payload = {
        "v": RENDER_VERSION,
        "clips": [
            [
                _file_sig(c.video),
                _file_sig(c.vocals),
                _file_sig(c.fx),
                round(c.trim_in, 3),
                round(c.trim_out, 3),
                c.muted,
                [[*_file_sig(p.path), p.offset, p.volume] for p in c.overdubs],
            ]
            for c in spec.clips
        ],
        "music": None
        if spec.music is None
        else [_file_sig(spec.music.path), spec.music.offset, spec.music.volume, spec.music.duck_db],
        "fx_volume": spec.fx_volume,
        "captions": [[c.words, c.trim_in, c.trim_out, c.offset] for c in spec.captions]
        if spec.captions_on
        else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def preview_state(conn, episode_id: int) -> dict[str, Any]:
    ep = episode_context(conn, episode_id)
    out: dict[str, Any] = {
        "path": ep["preview_path"],
        "url": media.url(ep["preview_path"]) if ep["preview_path"] else None,
        "rendered_at": ep["preview_rendered_at"],
        "fresh": False,
        "job_id": _pending_job(conn, PREVIEW_JOB, episode_id=episode_id),
    }
    if ep["preview_path"] and media.abs_path(ep["preview_path"]).is_file():
        try:
            spec = build_spec(conn, episode_id)
        except NotReady:
            return out
        out["fresh"] = spec.hash is not None and spec.hash == ep["preview_hash"]
    return out


# --- alignment ----------------------------------------------------------------------------


def request_alignment(conn, item: Item) -> int | None:
    """Make sure an alignment exists or is queued for an item. Returns the queued job id."""
    assert item.take is not None
    if alignment_row(conn, item.take["id"], item.caption_hash) is not None:
        return None
    if not item.lines:
        store_alignment(conn, item.take["id"], item.caption_hash, "", [], "aligned")
        return None
    pending = _pending_job(conn, ALIGN_JOB, take_id=item.take["id"], slice_sha256=item.caption_hash)
    if pending is not None:
        return pending
    return jobs.enqueue(
        conn,
        ALIGN_JOB,
        {
            "take_id": item.take["id"],
            "slice_sha256": item.caption_hash,
            "lines": item.lines,
            "episode_id": item.take["episode_id"],
            "snippet_id": item.snippet_id,
        },
    )


async def align_take(take_id: int, lines: list[str], text_hash: str, episode_id: int) -> dict[str, Any]:
    """Run (or reuse) the alignment for one take and caption text."""
    conn = db.connect()
    try:
        row = alignment_row(conn, take_id, text_hash)
        if row is not None:
            return {"cached": True, "method": row["method"]}
        take = db.row(conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone())
        lines_on_take = overdub.active(conn, take_id) if take is not None else []
    finally:
        conn.close()
    if take is None:
        raise RuntimeError(f"take {take_id} not found")
    text = "\n".join(lines)
    if not lines:
        words, method = [], "aligned"
        result: dict[str, Any] = {}
    else:
        if not take.get("vocals_path") or not media.abs_path(take["vocals_path"]).is_file():
            raise RuntimeError(f"take {take_id} has no vocals stem")
        vocals = media.abs_path(take["vocals_path"])
        if lines_on_take:
            # Align against what the render plays: the take's vocals (unless stripped) plus its lines.
            with tempfile.TemporaryDirectory(prefix="frootio-align-") as tmp:
                mixed = Path(tmp) / "vocals+overdubs.wav"
                await asyncio.to_thread(
                    overdub.mix_vocals,
                    vocals,
                    overdub.placed(lines_on_take),
                    mixed,
                    mute_base=bool(take.get("audio_muted")),
                )
                result = await jobs.run_heavy("align", {"vocals": str(mixed), "lines": lines})
        else:
            result = await jobs.run_heavy("align", {"vocals": str(vocals), "lines": lines})
        words, method = result["words"], result["method"]
    conn = db.connect()
    try:
        store_alignment(conn, take_id, text_hash, text, words, method)
    finally:
        conn.close()
    events.publish(
        {
            "type": "assembly",
            "episode_id": episode_id,
            "kind": "align",
            "take_id": take_id,
            "status": "done",
            "method": method,
        }
    )
    return {"cached": False, "method": method, "device": result.get("device"), "error": result.get("error")}


@jobs.handler(ALIGN_JOB)
async def _align_job(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await align_take(
            payload["take_id"], payload["lines"], payload["slice_sha256"], payload["episode_id"]
        )
    except Exception as exc:
        events.publish(
            {
                "type": "assembly",
                "episode_id": payload["episode_id"],
                "kind": "align",
                "take_id": payload["take_id"],
                "status": "failed",
                "error": str(exc),
            }
        )
        raise


async def _ensure_alignments(spec: RenderSpec, episode_id: int, captions_on: bool | None) -> RenderSpec:
    for it in spec.missing_alignments:
        await align_take(it.take["id"], it.lines, it.caption_hash, episode_id)  # type: ignore[index]
    if not spec.missing_alignments:
        return spec
    conn = db.connect()
    try:
        return build_spec(conn, episode_id, captions_on=captions_on)
    finally:
        conn.close()


# --- rendering -----------------------------------------------------------------------------


def _publish(episode_id: int, kind: str, status: str, **extra: Any) -> None:
    events.publish({"type": "assembly", "episode_id": episode_id, "kind": kind, "status": status, **extra})


async def run_ffmpeg(cmd: list[str], total: float, on_progress, cwd: Path) -> None:
    """Run ffmpeg with `-progress pipe:1`, reporting progress 0..1 (throttled)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    last = 0.0
    try:
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        async for raw in proc.stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit() and total > 0:
                frac = min(1.0, int(value) / 1e6 / total)
                if time.monotonic() - last >= 0.5:
                    last = time.monotonic()
                    on_progress(frac)
        err = (await stderr_task).decode(errors="replace")
        code = await proc.wait()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if code != 0:
        raise RuntimeError(f"ffmpeg exited {code}: {err.strip()[-800:]}")


async def render(spec: RenderSpec, dest_rel: str, quality: str, on_progress) -> None:
    """Render `spec` to `dest_rel` (atomic: temp file in the same dir, then rename)."""
    dest = media.abs_path(dest_rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frootio-render-") as tmp:
        work = Path(tmp)
        ass_file = None
        if spec.captions_on:
            (work / "captions.ass").write_text(captions.build_ass(spec.captions))
            ass_file = "captions.ass"
        tmp_out = dest.parent / f".{dest.stem}.{os.getpid()}.rendering.mp4"
        cmd = ffmpeg_command(
            spec.clips,
            tmp_out,
            quality=quality,
            fx_volume=spec.fx_volume,
            music=spec.music,
            ass_file=ass_file,
        )
        try:
            await run_ffmpeg(cmd, spec.total_duration, on_progress, work)
            os.replace(tmp_out, dest)
        finally:
            tmp_out.unlink(missing_ok=True)


@jobs.handler(PREVIEW_JOB)
async def _preview_job(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    episode_id = payload["episode_id"]
    _publish(episode_id, "preview", "running", job_id=job_id, progress=0.0)
    try:
        conn = db.connect()
        try:
            spec = build_spec(conn, episode_id)
        finally:
            conn.close()
        spec = await _ensure_alignments(spec, episode_id, None)
        ep = spec.episode
        rel = media.preview_path(ep["series_slug"], ep["number"])
        if (
            ep["preview_hash"] == spec.hash
            and ep["preview_path"]
            and media.abs_path(ep["preview_path"]).is_file()
        ):
            _publish(episode_id, "preview", "done", job_id=job_id, progress=1.0, reused=True)
            return {"path": ep["preview_path"], "reused": True}
        await render(
            spec,
            rel,
            "preview",
            lambda f: _publish(episode_id, "preview", "running", job_id=job_id, progress=round(f, 3)),
        )
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE episodes SET preview_path = ?, preview_hash = ?, preview_rendered_at = ?"
                " WHERE id = ?",
                (rel, spec.hash, db.now(), episode_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _publish(episode_id, "preview", "failed", job_id=job_id, error=str(exc))
        raise
    _publish(episode_id, "preview", "done", job_id=job_id, progress=1.0)
    return {"path": rel, "reused": False, "hash": spec.hash}


def export_rel(episode: dict[str, Any]) -> str:
    base = media.exports_dir(episode["series_slug"], episode["number"])
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    rel, n = f"{base}/{stamp}.mp4", 1
    while media.abs_path(rel).exists():
        n += 1
        rel = f"{base}/{stamp}-{n}.mp4"
    return rel


def snapshot(spec: RenderSpec) -> dict[str, Any]:
    """JSON record of what an export was rendered from (exports.settings)."""
    ep = spec.episode
    return {
        "settings": settings_of(ep) | {"captions_on": spec.captions_on},
        "takes": [
            {
                "snippet_id": it.snippet_id,
                "take_id": it.take["id"],
                "trim_in_s": c.trim_in,  # type: ignore[index]
                "trim_out_s": c.trim_out,
                "audio_muted": c.muted,
                "overdubs": overdub.signature(it.overdubs),
            }
            for it, c in zip(spec.items, spec.clips, strict=True)
        ],
        "duration_s": round(spec.total_duration, 3),
    }


def _update_export(export_id: int, **cols: Any) -> None:
    conn = db.connect()
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE exports SET {sets}, updated_at = ? WHERE id = ?", (*cols.values(), db.now(), export_id)
        )
        conn.commit()
    finally:
        conn.close()


@jobs.handler(EXPORT_JOB)
async def _export_job(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    episode_id, export_id = payload["episode_id"], payload["export_id"]
    captions_on = bool(payload["captions_on"])
    _update_export(export_id, status="running", error=None)
    _publish(episode_id, "export", "running", job_id=job_id, export_id=export_id, progress=0.0)
    try:
        conn = db.connect()
        try:
            spec = build_spec(conn, episode_id, captions_on=captions_on)
        finally:
            conn.close()
        spec = await _ensure_alignments(spec, episode_id, captions_on)
        rel = export_rel(spec.episode)
        await render(
            spec,
            rel,
            "export",
            lambda f: _publish(
                episode_id, "export", "running", job_id=job_id, export_id=export_id, progress=round(f, 3)
            ),
        )
        _update_export(export_id, status="done", path=rel, settings=db.dumps(snapshot(spec)))
    except asyncio.CancelledError:
        _update_export(export_id, status="failed", error="cancelled")
        raise
    except Exception as exc:
        _update_export(export_id, status="failed", error=str(exc))
        _publish(episode_id, "export", "failed", job_id=job_id, export_id=export_id, error=str(exc))
        raise
    _publish(episode_id, "export", "done", job_id=job_id, export_id=export_id, progress=1.0, path=rel)
    return {"path": rel, "export_id": export_id}
