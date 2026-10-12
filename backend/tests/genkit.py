"""Fixtures for generation tests: real media files, a cast character with a main
image and voice sample (so the real prompt builder produces refs), snippet rows,
and a scripted fake fal."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from frootio import db, falapi, generation, jobs, media, prompt

from .conftest import make_episode

_ASSETS: dict[str, Path] = {}


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def asset(tmp_root: Path, name: str) -> Path:
    """Small real media files generated once per session dir and copied per test."""
    cache = tmp_root.parent / "frootio-genkit-assets"
    cache.mkdir(exist_ok=True)
    path = cache / name
    if not path.exists():
        if name == "square.png":
            _ffmpeg("-f", "lavfi", "-i", "color=c=yellow:s=1024x1024", "-frames:v", "1", str(path))
        elif name == "voice.wav":
            _ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=2.5", "-ar", "44100", str(path))
        elif name == "take.mp4":
            _ffmpeg(
                "-f", "lavfi", "-i", "testsrc=s=96x168:r=24:d=2",
                "-f", "lavfi", "-i", "sine=f=330:d=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
            )  # fmt: skip
        else:
            raise ValueError(name)
    return path


@dataclass
class World:
    series_id: int
    episode_id: int
    character_id: int
    image_rel: str
    voice_rel: str
    video_src: Path


STYLE = "Pixar style."


def make_world(conn: sqlite3.Connection, tmp_path: Path) -> World:
    series_id, episode_id = make_episode(conn)
    ts = db.now()
    conn.execute("UPDATE series SET style_preamble = ? WHERE id = ?", (STYLE, series_id))
    character_id = conn.execute(
        "INSERT INTO characters (name, slug, description, created_at, updated_at)"
        " VALUES ('Kiwi', 'kiwi', 'a cocky kiwi', ?, ?)",
        (ts, ts),
    ).lastrowid
    image_src, voice_src = asset(tmp_path, "square.png"), asset(tmp_path, "voice.wav")
    image_rel = media.store_file(media.character_images_dir("kiwi"), image_src)
    voice_rel = media.store_file(media.character_voice_dir("kiwi"), voice_src)
    image_id = conn.execute(
        "INSERT INTO character_images (character_id, path, sha256, width, height, created_at)"
        " VALUES (?, ?, ?, 1024, 1024, ?)",
        (character_id, image_rel, media.sha256_file(image_src), ts),
    ).lastrowid
    voice_id = conn.execute(
        "INSERT INTO character_voices (character_id, path, sha256, duration_s, source, created_at)"
        " VALUES (?, ?, ?, 2.5, 'upload', ?)",
        (character_id, voice_rel, media.sha256_file(voice_src), ts),
    ).lastrowid
    conn.execute(
        "UPDATE characters SET main_image_id = ?, voice_id = ? WHERE id = ?",
        (image_id, voice_id, character_id),
    )
    conn.commit()
    return World(series_id, episode_id, character_id, image_rel, voice_rel, asset(tmp_path, "take.mp4"))


def add_snippet(
    conn: sqlite3.Connection, world: World, position: int, *, duration: int = 5, continue_prev: bool = False
) -> int:
    ts = db.now()
    sid = conn.execute(
        "INSERT INTO snippets (episode_id, position, body, target_duration_s, continue_from_previous,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (world.episode_id, position, f"body {position}", duration, int(continue_prev), ts, ts),
    ).lastrowid
    conn.execute(
        "INSERT INTO snippet_cast (snippet_id, character_id, position) VALUES (?, ?, 0)",
        (sid, world.character_id),
    )
    # Style overridden (same text as the series preamble) so takes record a mixed set of flags.
    prompt.update_parts(conn, sid, {"style": {"text": STYLE, "overridden": True}})
    conn.commit()
    return sid


@dataclass
class FakeFal:
    """Scripted fal: each request walks its own copy of `statuses` per poll (last one repeats)."""

    video_src: Path
    submit_error: Exception | None = None
    statuses: list[falapi.FalStatus] = field(default_factory=lambda: [falapi.FalStatus("completed")])
    result_failures: int = 0  # result()/download fails this many times first
    cancel_error: Exception | None = None
    seed: int = 4242
    calls: list[tuple[str, Any]] = field(default_factory=list)
    _n: int = 0
    _scripts: dict[str, list[falapi.FalStatus]] = field(default_factory=dict)

    async def upload_file(self, path: Path | str) -> str:
        self.calls.append(("upload", Path(path).name))
        return f"https://fal.test/files/{Path(path).name}"

    async def submit(self, endpoint: str, arguments: dict[str, Any]) -> str:
        self.calls.append(("submit", arguments))
        if self.submit_error:
            raise self.submit_error
        self._n += 1
        return f"req-{self._n}"

    async def status(self, endpoint: str, request_id: str, *, with_logs: bool = False) -> falapi.FalStatus:
        self.calls.append(("status", request_id))
        script = self._scripts.setdefault(request_id, list(self.statuses))
        return script.pop(0) if len(script) > 1 else script[0]

    async def result(self, endpoint: str, request_id: str) -> dict[str, Any]:
        self.calls.append(("result", request_id))
        if self.result_failures > 0:
            self.result_failures -= 1
            raise RuntimeError("fal result unavailable")
        return {"video": {"url": f"https://fal.test/out/{request_id}.mp4"}, "seed": self.seed}

    async def cancel(self, endpoint: str, request_id: str) -> None:
        self.calls.append(("cancel", request_id))
        if self.cancel_error:
            raise self.cancel_error

    async def fetch_to(self, url: str, dest: Path) -> None:
        self.calls.append(("fetch", url))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.video_src, dest)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("upload_file", "submit", "status", "result", "cancel"):
            monkeypatch.setattr(falapi, name, getattr(self, name))
        monkeypatch.setattr(falapi, "is_configured", lambda: True)
        monkeypatch.setattr(generation, "fetch_to", self.fetch_to)


async def fake_stems(task: str, args: dict[str, Any]) -> dict[str, Any]:
    assert task == "stems"
    for key in ("vocals_out", "fx_out"):
        Path(args[key]).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_ASSETS["voice"], args[key])
    return {"vocals_out": args["vocals_out"], "fx_out": args["fx_out"]}


def setup(monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path, **fal_kwargs: Any) -> FakeFal:
    fal = FakeFal(world.video_src, **fal_kwargs)
    fal.install(monkeypatch)
    monkeypatch.setattr(generation, "POLL_INITIAL_S", 0.01)
    monkeypatch.setattr(generation, "POLL_MAX_S", 0.02)
    monkeypatch.setattr(generation, "DOWNLOAD_RETRY_DELAY_S", 0.01)
    _ASSETS["voice"] = asset(tmp_path, "voice.wav")
    monkeypatch.setattr(jobs, "run_heavy", fake_stems)
    return fal


def take_row(conn: sqlite3.Connection, take_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone()


async def wait_take(take_id: int, *statuses: str, timeout: float = 10.0, settled: bool = True) -> sqlite3.Row:
    """Wait for a take status; `settled` also waits for its current job to finish."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        conn = db.connect()
        try:
            t = take_row(conn, take_id)
        finally:
            conn.close()
        if t["status"] in statuses and not (settled and _job_active(t["job_id"])):
            return t
        if loop.time() > deadline:
            raise AssertionError(f"take {take_id} stuck in {t['status']}, wanted {statuses}")
        await asyncio.sleep(0.02)


def _job_active(job_id: int | None) -> bool:
    if job_id is None:
        return False
    job = jobs.get(job_id)
    return job is not None and job["status"] in ("queued", "running")
