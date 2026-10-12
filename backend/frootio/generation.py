"""Generating takes: preflight (prompt, continuity, cost, cap), the Generate click,
the `h3_take` job (submit -> poll -> cost -> download), cancel and retry.

Take lifecycle (`takes.status`):

    queued --submit--> queued/running (fal IN_QUEUE/IN_PROGRESS)
      -> processing (fal completed: cost logged, then download + `postprocess` job) -> done
    failures: failed (fal error or submit failure, $0), cancelled ($0),
              download_failed / processing_failed (cost still counts; Retry re-fetches /
              reprocesses without a new fal call)

A take's fal request is submitted at most once: `submitted_at` is set before
submitting and `fal_request_id` right after, so a job resumed after a restart
polls the stored request, and a submit that never returned an id is marked
failed instead of being resubmitted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import config, continuity, cost, db, events, falapi, jobs, media, mediaprobe, prompt, spend

log = logging.getLogger(__name__)

PART_NAMES = ("style", "legend", "location", "body", "tail")
DERIVED_PARTS = ("style", "legend", "location")  # inherited or overridden; body/tail are plain text
IN_FLIGHT = ("queued", "running")
HAS_VIDEO = ("processing", "done", "processing_failed")

# Poll backoff (no timeout: slow takes are flagged in the API and can be cancelled).
POLL_INITIAL_S = 2.0
POLL_MAX_S = 30.0
POLL_FACTOR = 1.5
DOWNLOAD_RETRY_DELAY_S = 3.0
DOWNLOAD_TIMEOUT_S = 300.0


class GenerateError(Exception):
    """A request-level refusal, mapped to an HTTP status by the routers."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class TakeFailed(RuntimeError):
    """Raised by job handlers after recording a take failure, so the job ends `failed` too."""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# --- events / serialisation --------------------------------------------------------


def publish_take(conn: sqlite3.Connection, take_id: int) -> None:
    t = conn.execute(
        "SELECT id, snippet_id, episode_id, status FROM takes WHERE id = ?", (take_id,)
    ).fetchone()
    if t is not None:
        events.publish(
            {
                "type": "take",
                "id": t["id"],
                "snippet_id": t["snippet_id"],
                "episode_id": t["episode_id"],
                "status": t["status"],
            }
        )


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def is_slow(take: sqlite3.Row | dict[str, Any], now: datetime | None = None) -> bool:
    if take["status"] not in IN_FLIGHT:
        return False
    now = now or datetime.now(UTC)
    return (now - _parse_ts(take["created_at"])).total_seconds() > config.SLOW_TAKE_SECONDS


def _url(rel: str | None) -> str | None:
    return media.url(rel) if rel else None


def take_summary(take: sqlite3.Row, selected_take_id: int | None) -> dict[str, Any]:
    status = take["status"]
    return {
        "id": take["id"],
        "snippet_id": take["snippet_id"],
        "episode_id": take["episode_id"],
        "status": status,
        "error": take["error"],
        "hidden": bool(take["hidden"]),
        "selected": take["id"] == selected_take_id,
        "slow": is_slow(take),
        "batch_id": take["batch_id"],
        "retry_of_take_id": take["retry_of_take_id"],
        "seed": take["seed"],
        "seed_requested": take["seed_requested"],
        "requested_duration_s": take["requested_duration_s"],
        "estimated_cost_usd": take["estimated_cost_usd"],
        "cost_usd": take["cost_usd"],
        "fal_request_id": take["fal_request_id"],
        "created_at": take["created_at"],
        "submitted_at": take["submitted_at"],
        "completed_at": take["completed_at"],
        "video_url": _url(take["video_path"]),
        "poster_url": _url(take["poster_path"]),
        "vocals_url": _url(take["vocals_path"]),
        "fx_url": _url(take["fx_path"]),
        "video_duration_s": take["video_duration_s"],
        "width": take["width"],
        "height": take["height"],
        "trim_in_s": take["trim_in_s"],
        "trim_out_s": take["trim_out_s"],
        "start_frame": _start_frame_out(take),
        "can_cancel": status in IN_FLIGHT,
        "can_retry": status in ("failed", "download_failed", "processing_failed"),
        "can_select": status in HAS_VIDEO and take["video_path"] is not None,
    }


def _start_frame_out(take: sqlite3.Row) -> dict[str, Any] | None:
    if take["start_frame_path"] is None:
        return None
    return {
        "take_id": take["start_frame_take_id"],
        "time_s": take["start_frame_time_s"],
        "path": take["start_frame_path"],
        "url": media.url(take["start_frame_path"]),
    }


def take_prompt(take: sqlite3.Row) -> dict[str, Any]:
    """Exactly what was sent: five parts with flags, joined prompt, refs, start frame, seed.
    Body and tail are always per-snippet text, so only the derived parts carry a flag."""
    parts = {}
    for name in PART_NAMES:
        derived = name in DERIVED_PARTS
        parts[name] = {
            "text": take[f"{name}_text"],
            "overridden": bool(take[f"{name}_overridden"]) if derived else False,
        }
    return {
        "take_id": take["id"],
        "snippet_id": take["snippet_id"],
        "parts": parts,
        "joined": take["prompt"],
        "char_count": len(take["prompt"]),
        "refs": db.loads(take["refs"], []),
        "ref_tokens": take["ref_tokens"],
        "start_frame": _start_frame_out(take),
        "seed": take["seed"],
        "seed_requested": take["seed_requested"],
        "requested_duration_s": take["requested_duration_s"],
        "request_args": db.loads(take["request_args"]),
    }


# --- preflight -----------------------------------------------------------------------


@dataclass
class Plan:
    snippet: sqlite3.Row
    episode: sqlite3.Row
    series_slug: str
    built: prompt.BuiltPrompt
    refs: list[dict[str, Any]]
    ref_tokens: int
    per_take_usd: float
    start_take: sqlite3.Row | None
    start_time_s: float | None
    blocked: str | None


def _snippet_rows(conn: sqlite3.Connection, snippet_id: int) -> tuple[sqlite3.Row, sqlite3.Row, str]:
    snippet = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if snippet is None:
        raise GenerateError(404, "snippet not found")
    episode = conn.execute("SELECT * FROM episodes WHERE id = ?", (snippet["episode_id"],)).fetchone()
    slug = conn.execute("SELECT slug FROM series WHERE id = ?", (episode["series_id"],)).fetchone()[0]
    return snippet, episode, slug


def plan(conn: sqlite3.Connection, snippet_id: int) -> Plan:
    """Everything Generate needs, plus why it's blocked (if it is). Doesn't touch fal."""
    snippet, episode, slug = _snippet_rows(conn, snippet_id)
    built = prompt.build_prompt(conn, snippet_id)
    refs = [{"kind": r.kind, "path": r.path, "sha": r.sha, "label": r.label} for r in built.refs]
    blocked: list[str] = []
    if snippet["archived"]:
        blocked.append("Snippet is archived")
    blocked += list(built.errors)
    try:
        for ref, tokens in zip(refs, cost.ref_token_counts(refs), strict=True):
            ref["tokens"] = tokens
    except (mediaprobe.MediaError, ValueError) as exc:
        blocked.append(f"Cannot read a reference file: {exc}")
    tokens = sum(r.get("tokens", 0) for r in refs)

    start_take = None
    start_time = None
    reason = continuity.blocked_reason(conn, snippet_id)
    if reason:
        blocked.append(reason)
    else:
        expected = continuity.expected_start(conn, snippet_id)
        if expected is not None:
            start_take = conn.execute("SELECT * FROM takes WHERE id = ?", (expected[0],)).fetchone()
            start_time = expected[1]
            if start_take is None or start_take["video_path"] is None:
                blocked.append("Previous snippet's selected take has no video")
            elif start_time is None:
                try:
                    start_time = mediaprobe.probe(media.abs_path(start_take["video_path"])).duration_s
                except mediaprobe.MediaError as exc:
                    blocked.append(f"Cannot read previous take's video: {exc}")
    return Plan(
        snippet=snippet,
        episode=episode,
        series_slug=slug,
        built=built,
        refs=refs,
        ref_tokens=tokens,
        per_take_usd=cost.h3_cost(snippet["target_duration_s"], tokens),
        start_take=start_take,
        start_time_s=start_time,
        blocked="; ".join(blocked) or None,
    )


def estimate(conn: sqlite3.Connection, snippet_id: int, n: int | None) -> dict[str, Any]:
    p = plan(conn, snippet_id)
    n = n or p.episode["take_count"]
    total = round(p.per_take_usd * n, 6)
    cap = cost.cap_status(conn, p.episode["id"], total)
    return {
        "snippet_id": snippet_id,
        "n": n,
        "default_n": p.episode["take_count"],
        "duration_s": p.snippet["target_duration_s"],
        "ref_tokens": p.ref_tokens,
        "refs": p.refs,
        "per_take_usd": p.per_take_usd,
        "total_usd": total,
        "cap": cap.as_dict(),
        "cap_error": cap.message() if cap.exceeded else None,
        "blocked": p.blocked,
        "pinned_seed": p.snippet["pinned_seed"],
        "start_frame": (
            {"take_id": p.start_take["id"], "time_s": p.start_time_s} if p.start_take is not None else None
        ),
    }


def _check_cap(conn: sqlite3.Connection, episode_id: int, new_usd: float) -> None:
    cap = cost.cap_status(conn, episode_id, new_usd)
    if cap.exceeded:
        raise GenerateError(409, cap.message())


# --- launching takes ------------------------------------------------------------------


async def _upload(path_rel: str) -> str:
    return await falapi.upload_file(media.abs_path(path_rel))


async def _upload_inputs(
    refs: list[dict[str, Any]], start_frame_path: str | None
) -> tuple[list[str], str | None]:
    """Upload every ref (and the start frame) once; the URLs are shared by the click's takes."""
    if not falapi.is_configured():
        raise GenerateError(503, "FAL_KEY is not set")
    paths = [r["path"] for r in refs] + ([start_frame_path] if start_frame_path else [])
    try:
        urls = await asyncio.gather(*(_upload(p) for p in paths))
    except GenerateError:
        raise
    except Exception as exc:
        raise GenerateError(502, f"Uploading refs to fal failed: {exc}") from exc
    if start_frame_path:
        return list(urls[:-1]), urls[-1]
    return list(urls), None


def fal_arguments(
    prompt_text: str,
    duration_s: int,
    refs: list[dict[str, Any]],
    start_frame_url: str | None,
    seed: int | None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "prompt": prompt_text,
        "prompt_expansion_mode": "disabled",
        "resolution": config.H3_RESOLUTION,
        "aspect_ratio": config.H3_ASPECT_RATIO,
        "duration": duration_s,
    }
    images = [r["url"] for r in refs if r["kind"] == "image"]
    audio = [r["url"] for r in refs if r["kind"] == "audio"]
    if images:
        args["reference_image_urls"] = images
    if audio:
        args["reference_audio_urls"] = audio
    if start_frame_url:
        args["image_url"] = start_frame_url
    if seed is not None:
        args["seed"] = seed
    return args


def _insert_take(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
    ts = db.now()
    values = {**values, "status": "queued", "created_at": ts, "updated_at": ts}
    cols = ", ".join(values)
    marks = ", ".join("?" * len(values))
    return conn.execute(f"INSERT INTO takes ({cols}) VALUES ({marks})", tuple(values.values())).lastrowid


def _launch(
    conn: sqlite3.Connection, rows: list[dict[str, Any]], episode_id: int, new_usd: float
) -> list[int]:
    """Insert takes and their `h3_take` jobs. The cap is re-checked here, after the uploads'
    awaits, with no await until the inserts are committed, so concurrent clicks can't both pass."""
    _check_cap(conn, episode_id, new_usd)
    take_ids = [_insert_take(conn, r) for r in rows]
    conn.commit()
    for take_id in take_ids:
        job_id = jobs.enqueue(conn, "h3_take", {"take_id": take_id})
        conn.execute("UPDATE takes SET job_id = ? WHERE id = ?", (job_id, take_id))
        conn.commit()
        publish_take(conn, take_id)
    return take_ids


async def extract_start_frame(
    series_slug: str, episode_number: int, snippet_id: int, take: sqlite3.Row, at_s: float
) -> str:
    """Frame of `take` at its trim-out, stored content-addressed under the snippet's start-frames/."""
    data = await asyncio.to_thread(mediaprobe.extract_frame, media.abs_path(take["video_path"]), at_s, "png")
    return media.store_bytes(media.start_frames_dir(series_slug, episode_number, snippet_id), data, "png")


async def generate(conn: sqlite3.Connection, snippet_id: int, n: int | None, seed: int | None) -> list[int]:
    p = plan(conn, snippet_id)
    if p.blocked:
        raise GenerateError(409, p.blocked)
    n = n or p.episode["take_count"]
    total = round(p.per_take_usd * n, 6)
    _check_cap(conn, p.episode["id"], total)  # fail fast, before extracting/uploading

    start_path = None
    if p.start_take is not None:
        assert p.start_time_s is not None
        try:
            start_path = await extract_start_frame(
                p.series_slug, p.episode["number"], snippet_id, p.start_take, p.start_time_s
            )
        except mediaprobe.MediaError as exc:
            raise GenerateError(500, f"Extracting the start frame failed: {exc}") from exc

    ref_urls, start_url = await _upload_inputs(p.refs, start_path)
    refs = [{**r, "url": u} for r, u in zip(p.refs, ref_urls, strict=True)]
    parts = p.built.parts
    batch = uuid.uuid4().hex
    rows = []
    for i in range(n):
        seed_i = seed + i if seed is not None else None
        args = fal_arguments(p.built.joined, p.snippet["target_duration_s"], refs, start_url, seed_i)
        row: dict[str, Any] = {
            "snippet_id": snippet_id,
            "episode_id": p.episode["id"],
            "batch_id": batch,
            "model": config.H3_ENDPOINT,
            "prompt": p.built.joined,
            "request_args": db.dumps(args),
            "refs": db.dumps(refs),
            "ref_tokens": p.ref_tokens,
            "requested_duration_s": p.snippet["target_duration_s"],
            "seed_requested": seed_i,
            "start_frame_take_id": p.start_take["id"] if p.start_take is not None else None,
            "start_frame_time_s": p.start_time_s if p.start_take is not None else None,
            "start_frame_path": start_path,
            "start_frame_url": start_url,
            "estimated_cost_usd": p.per_take_usd,
        }
        for name in PART_NAMES:
            row[f"{name}_text"] = parts[name].text
            if name in DERIVED_PARTS:
                row[f"{name}_overridden"] = int(parts[name].overridden)
        rows.append(row)
    return _launch(conn, rows, p.episode["id"], total)


# --- cancel / retry ----------------------------------------------------------------------


def _take(conn: sqlite3.Connection, take_id: int) -> sqlite3.Row:
    t = conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone()
    if t is None:
        raise GenerateError(404, "take not found")
    return t


async def cancel(conn: sqlite3.Connection, take_id: int) -> None:
    take = _take(conn, take_id)
    if take["status"] not in IN_FLIGHT:
        raise GenerateError(
            409, f"Only queued or running takes can be cancelled (this one is {take['status']})"
        )
    if take["submitted_at"] is not None and not take["fal_request_id"]:
        # The submit POST is on the wire: fal may already have the request, but we don't have
        # its id to cancel it. Marking it cancelled now would hide a billed video from spend/cap.
        raise GenerateError(409, "Submitting to fal; try Cancel again in a moment")
    if take["fal_request_id"]:
        try:
            await falapi.cancel(take["model"], take["fal_request_id"])
        except falapi.FalClientHTTPError as exc:
            # e.g. 400 "already completed": the video exists and is billed; let the job finish it.
            raise GenerateError(409, f"fal refused to cancel: {exc}") from exc
    # The take update and the job cancel run with no await in between, so the job's poller
    # (same event loop) can't complete the take after we decided to cancel it.
    ts = db.now()
    marks = ",".join("?" * len(IN_FLIGHT))
    cur = conn.execute(
        f"UPDATE takes SET status = 'cancelled', cost_usd = 0, completed_at = ?, updated_at = ?"
        f" WHERE id = ? AND status IN ({marks})",
        (ts, ts, take_id, *IN_FLIGHT),
    )
    conn.commit()
    if not cur.rowcount:
        raise GenerateError(409, "The take finished before it could be cancelled")
    if take["job_id"] is not None:
        jobs.cancel(take["job_id"])
    publish_take(conn, take_id)


async def retry(conn: sqlite3.Connection, take_id: int) -> int:
    """Failed: a new take from the stored parameters (new fal call). download_failed /
    processing_failed: fetch/process the same fal result again. Returns the take id to watch."""
    take = _take(conn, take_id)
    status = take["status"]
    if status == "failed":
        return await _retry_failed(conn, take)
    if status in ("download_failed", "processing_failed"):
        video_ok = take["video_path"] is not None and media.abs_path(take["video_path"]).is_file()
        kind = "postprocess" if status == "processing_failed" and video_ok else "h3_take"
        ts = db.now()
        conn.execute(
            "UPDATE takes SET status = 'processing', error = NULL, download_attempts = 0, updated_at = ?"
            " WHERE id = ?",
            (ts, take_id),
        )
        conn.commit()
        job_id = jobs.enqueue(conn, kind, {"take_id": take_id})
        conn.execute("UPDATE takes SET job_id = ? WHERE id = ?", (job_id, take_id))
        conn.commit()
        publish_take(conn, take_id)
        return take_id
    raise GenerateError(
        409, f"Only failed, download_failed or processing_failed takes can be retried ({status})"
    )


async def _retry_failed(conn: sqlite3.Connection, take: sqlite3.Row) -> int:
    refs = db.loads(take["refs"], [])
    for r in refs:
        if not media.abs_path(r["path"]).is_file():
            raise GenerateError(409, f"Reference file is missing: {r['path']}")
    estimate_usd = take["estimated_cost_usd"]
    _check_cap(conn, take["episode_id"], estimate_usd)
    ref_urls, start_url = await _upload_inputs(refs, take["start_frame_path"])
    refs = [{**r, "url": u} for r, u in zip(refs, ref_urls, strict=True)]
    args = fal_arguments(
        take["prompt"], take["requested_duration_s"], refs, start_url, take["seed_requested"]
    )
    copied = (
        "snippet_id", "episode_id", "model", "prompt", "ref_tokens", "requested_duration_s",
        "seed_requested", "start_frame_take_id", "start_frame_time_s", "start_frame_path",
        "estimated_cost_usd",
        *(f"{n}_text" for n in PART_NAMES),
        *(f"{n}_overridden" for n in DERIVED_PARTS),
    )  # fmt: skip
    row = {c: take[c] for c in copied}
    row.update(
        batch_id=uuid.uuid4().hex,
        retry_of_take_id=take["id"],
        request_args=db.dumps(args),
        refs=db.dumps(refs),
        start_frame_url=start_url,
    )
    return _launch(conn, [row], take["episode_id"], estimate_usd)[0]


# --- the h3_take job -----------------------------------------------------------------------


def update_take(take_id: int, *, only_if: tuple[str, ...] | None = None, **values: Any) -> bool:
    """Update take columns (and publish); `only_if` guards against overwriting e.g. a cancel."""
    values["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in values)
    sql = f"UPDATE takes SET {sets} WHERE id = ?"
    params: list[Any] = [*values.values(), take_id]
    if only_if:
        sql += f" AND status IN ({','.join('?' * len(only_if))})"
        params += only_if
    with _db() as conn:
        changed = conn.execute(sql, params).rowcount > 0
        conn.commit()
        if changed and ("status" in values or "poster_path" in values or "video_path" in values):
            publish_take(conn, take_id)
    return changed


def load_take(take_id: int) -> sqlite3.Row:
    with _db() as conn:
        t = conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone()
    if t is None:
        raise TakeFailed(f"take {take_id} not found")
    return t


def fail_take(take_id: int, status: str, error: str, **values: Any) -> None:
    update_take(take_id, status=status, error=error, **values)
    raise TakeFailed(error)


@jobs.handler("h3_take")
async def h3_take(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    take_id = int(payload["take_id"])
    take = load_take(take_id)
    if take["status"] not in (*IN_FLIGHT, "processing"):
        return {"skipped": take["status"]}
    if take["cost_usd"] is None:
        request_id = take["fal_request_id"]
        if request_id is None:
            request_id = await _submit(job_id, take)
        await _poll(take, request_id)
    return await _download(job_id, take_id)


async def _submit(job_id: int, take: sqlite3.Row) -> str:
    take_id = take["id"]
    if take["submitted_at"] is not None:
        fail_take(
            take_id, "failed",
            "Submit was interrupted before fal returned a request id; not resubmitted (it may have"
            " reached fal). Use Retry for a new take.",
            cost_usd=0, completed_at=db.now(),
        )  # fmt: skip
    if not update_take(take_id, only_if=IN_FLIGHT, submitted_at=db.now()):
        raise TakeFailed(f"take {take_id} is no longer queued")
    args = db.loads(take["request_args"], {})
    try:
        request_id = await falapi.submit(take["model"], args)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        fail_take(take_id, "failed", f"Submit failed: {exc}", cost_usd=0, completed_at=db.now())
    update_take(take_id, fal_request_id=request_id)
    jobs.set_fal_request_id(job_id, request_id)
    return request_id


async def _poll(take: sqlite3.Row, request_id: str) -> None:
    take_id = take["id"]
    delay = POLL_INITIAL_S
    last = take["status"]
    while True:
        try:
            st = await falapi.status(take["model"], request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network blip: keep polling, no timeout
            log.warning("take %s: status poll failed: %s", take_id, exc)
            st = None
        if st is not None and st.state == "completed":
            if st.error:
                msg = f"{st.error_type}: {st.error}" if st.error_type else st.error
                fail_take(take_id, "failed", msg, cost_usd=0, completed_at=db.now())
            _completed(take)
            return
        if st is not None:
            status = "queued" if st.state == "queued" else "running"
            if status != last:
                if not update_take(take_id, only_if=IN_FLIGHT, status=status):
                    raise TakeFailed(f"take {take_id} left the in-flight states")
                last = status
        await asyncio.sleep(delay)
        delay = min(delay * POLL_FACTOR, POLL_MAX_S)


def _completed(take: sqlite3.Row) -> None:
    """fal finished: the video exists and is billed. Record its cost and spend row together."""
    take_id = take["id"]
    usd = cost.h3_cost(take["requested_duration_s"], take["ref_tokens"] or 0)
    ts = db.now()
    marks = ",".join("?" * len(IN_FLIGHT))
    with _db() as conn:
        cur = conn.execute(
            f"UPDATE takes SET status = 'processing', cost_usd = ?, completed_at = ?, updated_at = ?"
            f" WHERE id = ? AND status IN ({marks})",
            (usd, ts, ts, take_id, *IN_FLIGHT),
        )
        if not cur.rowcount:
            conn.rollback()
            raise TakeFailed(f"take {take_id} left the in-flight states")
        # spend.log commits the take update and the spend row in one transaction.
        spend.log(
            conn, "video", usd, episode_id=take["episode_id"], take_id=take_id,
            note=f"H3 take {take_id} ({take['requested_duration_s']}s)",
        )  # fmt: skip
        publish_take(conn, take_id)


async def fetch_to(url: str, dest: Path) -> None:
    """Stream `url` into `dest` atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(1 << 20):
                        f.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def take_media_paths(take: sqlite3.Row) -> dict[str, str]:
    with _db() as conn:
        _, episode, slug = _snippet_rows(conn, take["snippet_id"])
    return media.take_paths(slug, episode["number"], take["snippet_id"], take["id"])


async def _download(job_id: int, take_id: int) -> dict[str, Any]:
    take = load_take(take_id)
    if take["status"] != "processing":
        return {"skipped": take["status"]}
    paths = take_media_paths(take)
    dest = media.abs_path(paths["video"])
    if take["video_path"] is None or not dest.is_file():
        attempts = take["download_attempts"]
        last_error = "no attempts left"
        while attempts < config.DOWNLOAD_MAX_TRIES:
            attempts += 1
            update_take(take_id, download_attempts=attempts)
            try:
                res = await falapi.result(take["model"], take["fal_request_id"])
                video_url = res["video"]["url"]
                update_take(take_id, seed=res.get("seed"), result_url=video_url)
                await fetch_to(video_url, dest)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("take %s: download attempt %s failed: %s", take_id, attempts, last_error)
                if attempts < config.DOWNLOAD_MAX_TRIES:
                    await asyncio.sleep(DOWNLOAD_RETRY_DELAY_S * attempts)
        else:
            fail_take(take_id, "download_failed", f"Download failed after {attempts} tries: {last_error}")
        info = None
        try:
            info = await asyncio.to_thread(mediaprobe.probe, dest)
        except mediaprobe.MediaError as exc:
            log.warning("take %s: probe after download failed: %s", take_id, exc)
        extra = (
            {"video_duration_s": info.duration_s, "width": info.width, "height": info.height} if info else {}
        )
        update_take(take_id, video_path=paths["video"], error=None, **extra)
    with _db() as conn:
        current_job = conn.execute("SELECT job_id FROM takes WHERE id = ?", (take_id,)).fetchone()[0]
        if current_job == job_id:  # not yet handed to post-processing
            pp_job = jobs.enqueue(conn, "postprocess", {"take_id": take_id})
            conn.execute("UPDATE takes SET job_id = ? WHERE id = ?", (pp_job, take_id))
            conn.commit()
    return {"take_id": take_id, "video_path": paths["video"]}


# h3_take hands finished downloads to the `postprocess` job; make sure its handler is registered.
from . import postprocess  # noqa: E402, F401
