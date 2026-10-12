"""Generate click, the h3_take job (submit/poll/download/resume), cancel, retry,
continuity start frames and post-processing, with fal mocked."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from frootio import db, events, falapi, generation, jobs, media, spend

from .genkit import World, add_snippet, fake_stems, make_world, setup, take_row, wait_take


@pytest.fixture
def world(conn: sqlite3.Connection, tmp_path: Path) -> World:
    return make_world(conn, tmp_path)


@pytest.fixture
async def worker(conn: sqlite3.Connection) -> AsyncIterator[jobs.Worker]:
    w = await jobs.start(poll_interval=0.02)
    yield w
    await jobs.stop()


def video_spend(conn: sqlite3.Connection, take_id: int) -> list[float]:
    return [
        r[0]
        for r in conn.execute("SELECT amount_usd FROM spend WHERE take_id = ? AND kind = 'video'", (take_id,))
    ]


async def test_generate_n_takes_end_to_end(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    fal.statuses = [falapi.FalStatus("queued"), falapi.FalStatus("running"), falapi.FalStatus("completed")]
    sid = add_snippet(conn, world, 0, duration=5)
    sub = events.subscribe()
    take_ids = await generation.generate(conn, sid, 2, None)
    assert len(take_ids) == 2
    # Every ref is uploaded once per click and shared by the N takes.
    assert fal.count("upload") == 2
    for tid in take_ids:
        t = await wait_take(tid, "done")
        assert t["cost_usd"] == pytest.approx(0.40) and t["estimated_cost_usd"] == pytest.approx(0.40)
        assert t["seed"] == 4242 and t["seed_requested"] is None
        assert video_spend(conn, tid) == [pytest.approx(0.40)]
        for col in ("video_path", "poster_path", "vocals_path", "fx_path"):
            assert media.abs_path(t[col]).is_file(), col
        assert t["poster_path"].endswith(f"/takes/{tid}.poster.jpg")
        assert (t["width"], t["height"]) == (96, 168) and t["video_duration_s"] == pytest.approx(2.0, abs=0.1)
        assert t["prompt"].startswith("Pixar style.") and t["legend_text"].startswith("Image 1 is Kiwi")
        assert t["style_overridden"] == 1 and t["legend_overridden"] == 0
        refs = db.loads(t["refs"])
        assert [(r["kind"], r["tokens"]) for r in refs] == [("image", 1024), ("audio", 200)]
        assert all(r["url"].startswith("https://fal.test/files/") for r in refs)
    submits = [a for k, a in fal.calls if k == "submit"]
    assert len(submits) == 2
    args = submits[0]
    assert args["prompt_expansion_mode"] == "disabled" and args["resolution"] == "768P"
    assert args["aspect_ratio"] == "9:16" and args["duration"] == 5
    assert len(args["reference_image_urls"]) == 1 and len(args["reference_audio_urls"]) == 1
    assert "seed" not in args and "image_url" not in args
    seen = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if ev["type"] == "take" and ev["id"] == take_ids[0]:
            seen.append(ev["status"])
    events.unsubscribe(sub)
    assert seen[0] == "queued" and seen[-1] == "done"
    assert {"running", "processing"} <= set(seen)


async def test_pinned_seed_increments_per_take(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    take_ids = await generation.generate(conn, sid, 3, 100)
    rows = [take_row(conn, t) for t in take_ids]
    assert [r["seed_requested"] for r in rows] == [100, 101, 102]
    assert [db.loads(r["request_args"])["seed"] for r in rows] == [100, 101, 102]
    assert len({r["batch_id"] for r in rows}) == 1
    assert fal.count("upload") == 2


async def test_submit_failure_is_failed_and_never_resubmitted(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path, submit_error=RuntimeError("connection reset"))
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    await jobs.start(poll_interval=0.02)
    try:
        t = await wait_take(tid, "failed")
    finally:
        await jobs.stop()
    assert "Submit failed: connection reset" in t["error"] and t["cost_usd"] == 0
    assert jobs.get(t["job_id"])["status"] == "failed"
    assert fal.count("submit") == 1
    # A resumed job (e.g. crash mid-submit: submitted_at set, no request id) must not submit again.
    fal.submit_error = None
    conn.execute("UPDATE takes SET status = 'queued', error = NULL, cost_usd = NULL WHERE id = ?", (tid,))
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (t["job_id"],))
    conn.commit()
    await jobs.start(poll_interval=0.02)
    try:
        t = await wait_take(tid, "failed")
    finally:
        await jobs.stop()
    assert fal.count("submit") == 1
    assert "not resubmitted" in t["error"]


async def test_fal_error_is_failed_at_zero_cost(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    fal.statuses = [
        falapi.FalStatus("running"),
        falapi.FalStatus("completed", error="content flagged", error_type="content_policy_violation"),
    ]
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    t = await wait_take(tid, "failed")
    assert t["error"] == "content_policy_violation: content flagged"
    assert t["cost_usd"] == 0 and video_spend(conn, tid) == []
    est = generation.estimate(conn, sid, 1)
    assert est["cap"]["in_flight_usd"] == 0  # its estimate is released


async def test_resume_polls_stored_request_instead_of_submitting(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    # Simulate a restart: the job was running with its fal request id saved.
    t = take_row(conn, tid)
    conn.execute(
        "UPDATE takes SET status = 'running', submitted_at = ?, fal_request_id = 'req-old' WHERE id = ?",
        (db.now(), tid),
    )
    conn.execute(
        "UPDATE jobs SET status = 'running', fal_request_id = 'req-old' WHERE id = ?", (t["job_id"],)
    )
    conn.commit()
    await jobs.start(poll_interval=0.02)
    try:
        t = await wait_take(tid, "done")
    finally:
        await jobs.stop()
    assert fal.count("submit") == 0
    assert ("status", "req-old") in fal.calls and ("result", "req-old") in fal.calls
    assert video_spend(conn, tid) == [pytest.approx(0.40)]


async def test_resume_after_cost_logged_does_not_double_log(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    t = take_row(conn, tid)
    conn.execute(
        "UPDATE takes SET status = 'processing', submitted_at = ?, fal_request_id = 'req-x', cost_usd = 0.4"
        " WHERE id = ?",
        (db.now(), tid),
    )
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (t["job_id"],))
    conn.commit()
    spend.log(conn, "video", 0.4, episode_id=world.episode_id, take_id=tid)
    await jobs.start(poll_interval=0.02)
    try:
        await wait_take(tid, "done")
    finally:
        await jobs.stop()
    assert fal.count("submit") == 0 and fal.count("status") == 0
    assert video_spend(conn, tid) == [pytest.approx(0.40)]


async def test_download_retries_then_succeeds(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path, result_failures=2)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    t = await wait_take(tid, "done")
    assert fal.count("result") == 3 and fal.count("submit") == 1
    assert t["download_attempts"] == 3


async def test_download_fails_three_times_then_retry_refetches(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path, result_failures=3)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    t = await wait_take(tid, "download_failed")
    assert "Download failed after 3 tries" in t["error"]
    assert fal.count("result") == 3
    # The video was generated: its cost counts even though the download failed.
    assert t["cost_usd"] == pytest.approx(0.40) and video_spend(conn, tid) == [pytest.approx(0.40)]
    assert generation.take_summary(t, None)["can_retry"]
    same = await generation.retry(conn, tid)
    assert same == tid
    t = await wait_take(tid, "done")
    assert fal.count("submit") == 1 and fal.count("result") == 4
    assert video_spend(conn, tid) == [pytest.approx(0.40)]


async def test_cancel_running_take(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    fal.statuses = [falapi.FalStatus("running")]
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    t = await wait_take(tid, "running", settled=False)
    assert t["status"] == "running" and t["fal_request_id"] == "req-1"
    await generation.cancel(conn, tid)
    t = await wait_take(tid, "cancelled")
    assert t["cost_usd"] == 0 and ("cancel", "req-1") in fal.calls
    assert jobs.get(t["job_id"])["status"] == "cancelled"
    assert video_spend(conn, tid) == []
    with pytest.raises(generation.GenerateError) as exc:
        await generation.cancel(conn, tid)
    assert exc.value.status_code == 409


async def test_cancel_refused_by_fal_keeps_take(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    conn.execute("UPDATE takes SET status = 'running', fal_request_id = 'req-9' WHERE id = ?", (tid,))
    conn.commit()
    import httpx

    resp = httpx.Response(400, request=httpx.Request("PUT", "https://queue.fal.run/x"))
    fal.cancel_error = falapi.FalClientHTTPError("already completed", 400, {}, resp)
    with pytest.raises(generation.GenerateError) as exc:
        await generation.cancel(conn, tid)
    assert exc.value.status_code == 409 and "already completed" in exc.value.detail
    assert take_row(conn, tid)["status"] == "running"


async def test_cancel_refused_while_submit_in_flight(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Submit sent (submitted_at set) but no request id yet: fal may run and bill it, so the
    # take must not become a $0 cancelled take that drops out of spend and the cap.
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    conn.execute("UPDATE takes SET submitted_at = ? WHERE id = ?", ("2026-01-01T00:00:00Z", tid))
    conn.commit()
    with pytest.raises(generation.GenerateError) as exc:
        await generation.cancel(conn, tid)
    assert exc.value.status_code == 409
    assert take_row(conn, tid)["status"] == "queued"
    assert fal.count("cancel") == 0


async def test_retry_failed_creates_new_take_with_stored_params(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    fal.statuses = [falapi.FalStatus("completed", error="model failure")]
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, 77)
    await wait_take(tid, "failed")
    # The snippet changes afterwards; the retry still uses the failed take's stored prompt.
    conn.execute("UPDATE snippets SET body = 'edited later' WHERE id = ?", (sid,))
    conn.commit()
    fal.statuses = [falapi.FalStatus("completed")]
    new_id = await generation.retry(conn, tid)
    assert new_id != tid
    t = await wait_take(new_id, "done")
    old = take_row(conn, tid)
    assert t["retry_of_take_id"] == tid and t["prompt"] == old["prompt"] and "edited later" not in t["prompt"]
    assert t["seed_requested"] == 77
    submits = [a for k, a in fal.calls if k == "submit"]
    assert len(submits) == 2 and submits[1]["seed"] == 77 and submits[1]["prompt"] == old["prompt"]
    assert fal.count("upload") == 4  # refs re-uploaded for the retry's fal call


async def test_processing_failed_retry_reprocesses_without_fal(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)

    async def stems_missing(task: str, args: dict) -> dict:
        raise jobs.HeavyError("stems", 1, "ModuleNotFoundError: No module named 'frootio.stems'")

    monkeypatch.setattr(jobs, "run_heavy", stems_missing)
    sid = add_snippet(conn, world, 0)
    [tid] = await generation.generate(conn, sid, 1, None)
    t = await wait_take(tid, "processing_failed")
    assert "frootio.stems" in t["error"]
    assert t["poster_path"] and media.abs_path(t["poster_path"]).is_file()  # poster made before stems
    assert video_spend(conn, tid) == [pytest.approx(0.40)]

    monkeypatch.setattr(jobs, "run_heavy", fake_stems)
    assert await generation.retry(conn, tid) == tid
    t = await wait_take(tid, "done")
    assert t["vocals_path"].endswith(f"{tid}.vocals.wav") and t["fx_path"].endswith(f"{tid}.fx.wav")
    assert fal.count("submit") == 1 and fal.count("result") == 1


async def test_continuity_start_frame(
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: jobs.Worker,
) -> None:
    setup(monkeypatch, world, tmp_path)
    first = add_snippet(conn, world, 0)
    second = add_snippet(conn, world, 1, continue_prev=True)
    with pytest.raises(generation.GenerateError) as exc:
        await generation.generate(conn, second, 1, None)
    assert exc.value.status_code == 409 and "no selected take" in exc.value.detail
    assert generation.estimate(conn, second, 1)["blocked"] == "Previous snippet has no selected take"

    [prev] = await generation.generate(conn, first, 1, None)
    await wait_take(prev, "done")
    conn.execute("UPDATE takes SET trim_out_s = 1.5 WHERE id = ?", (prev,))
    conn.execute("UPDATE snippets SET selected_take_id = ? WHERE id = ?", (prev, first))
    conn.commit()
    [tid] = await generation.generate(conn, second, 1, None)
    t = take_row(conn, tid)
    assert t["start_frame_take_id"] == prev and t["start_frame_time_s"] == 1.5
    assert t["start_frame_path"].startswith(media.start_frames_dir("test-series", 1, second) + "/")
    assert t["start_frame_path"].endswith(".png") and media.abs_path(t["start_frame_path"]).is_file()
    args = db.loads(t["request_args"])
    assert (
        args["image_url"]
        == t["start_frame_url"]
        == f"https://fal.test/files/{Path(t['start_frame_path']).name}"
    )
    # Untrimmed predecessor: the frame at the end of its video (not an empty seek past the end).
    conn.execute("UPDATE takes SET trim_out_s = NULL WHERE id = ?", (prev,))
    conn.commit()
    [tid2] = await generation.generate(conn, second, 1, None)
    t2 = take_row(conn, tid2)
    assert t2["start_frame_time_s"] == pytest.approx(2.0, abs=0.1)
    assert media.abs_path(t2["start_frame_path"]).is_file()
    assert t2["start_frame_path"] != t["start_frame_path"]


async def test_prompt_errors_block_generate(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    conn.execute("UPDATE snippets SET body = ? WHERE id = ?", ("x" * 50_001, sid))
    conn.commit()
    with pytest.raises(generation.GenerateError) as exc:
        await generation.generate(conn, sid, 1, None)
    assert exc.value.status_code == 409 and "fal allows 50000" in exc.value.detail
    assert fal.calls == [] and conn.execute("SELECT COUNT(*) FROM takes").fetchone()[0] == 0


async def test_cap_blocks_generate_before_uploading(
    conn: sqlite3.Connection, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    spend.log(conn, "video", 19.0, episode_id=world.episode_id)
    await generation.generate(conn, sid, 1, None)  # 19.40 projected: allowed, now in flight
    with pytest.raises(generation.GenerateError) as exc:
        await generation.generate(conn, sid, 2, None)  # 19 + 0.4 in flight + 0.8 > 20
    assert exc.value.status_code == 409 and "spend cap" in exc.value.detail
    assert fal.count("upload") == 2  # only the first click uploaded


def test_slow_flag_after_30_minutes(conn: sqlite3.Connection, world: World) -> None:
    sid = add_snippet(conn, world, 0)
    old = (datetime.now(UTC) - timedelta(minutes=31)).isoformat(timespec="seconds")
    for status in ("running", "queued", "done"):
        conn.execute(
            "INSERT INTO takes (snippet_id, episode_id, status, requested_duration_s, created_at, updated_at)"
            " VALUES (?, ?, ?, 5, ?, ?)",
            (sid, world.episode_id, status, old, old),
        )
    conn.commit()
    rows = conn.execute("SELECT * FROM takes ORDER BY id").fetchall()
    assert [generation.is_slow(r) for r in rows] == [True, True, False]
    fresh = dict(rows[0]) | {"created_at": db.now()}
    assert generation.is_slow(fresh) is False
