"""Overdub lines: Chatterbox job (fal mocked), placement in the render, caption alignment."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from frootio import assembly, config, db, falapi, jobs, media, overdub, paidgen

from .libmedia import tone
from .test_assembly import _max_db, fake_heavy, make_assembly  # noqa: F401  (fixture)
from .test_paidgen import FakeFal


def _voiced_character(conn: sqlite3.Connection, tmp_path: Path, name: str = "Kiwi") -> int:
    ts = db.now()
    cid = conn.execute(
        "INSERT INTO characters (name, slug, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, name.lower(), ts, ts),
    ).lastrowid
    rel = media.store_file(f"characters/{name.lower()}/voice", tone(tmp_path / "sample.wav", 3.0, 220))
    vid = conn.execute(
        "INSERT INTO character_voices (character_id, path, sha256, duration_s, source, created_at)"
        " VALUES (?, ?, 'x', 3.0, 'upload', ?)",
        (cid, rel, ts),
    ).lastrowid
    conn.execute("UPDATE characters SET voice_id = ? WHERE id = ?", (vid, cid))
    conn.commit()
    return cid


@pytest.fixture
def fal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeFal:
    monkeypatch.setenv("FAL_KEY", "test")
    line = tone(tmp_path / "line.wav", 0.6, 1000).read_bytes()
    f = FakeFal({"audio": {"url": "u-line"}})
    for name in ("submit", "status", "result", "upload_file"):
        monkeypatch.setattr(falapi, name, getattr(f, name))

    async def download(url: str) -> bytes:
        assert url == "u-line"
        return line

    monkeypatch.setattr(paidgen, "download", download)
    monkeypatch.setattr(paidgen, "POLL_START_S", 0.0)
    return f


async def _generate(conn, take_id: int, character_id: int, **kw: Any) -> dict[str, Any]:
    o = overdub.create(conn, take_id, character_id=character_id, text="THE OVEN IS ON FIRE!", **kw)
    job = jobs.get(conn.execute("SELECT job_id FROM overdubs WHERE id = ?", (o["id"],)).fetchone()[0])
    await overdub._tts_job(job["id"], job["payload"])
    return overdub.serialize(overdub._get(conn, o["id"]))


async def test_line_is_cloned_from_voice_sample_stored_and_billed_once(conn, fal, tmp_path: Path) -> None:
    ep = make_assembly(conn)
    kiwi = _voiced_character(conn, tmp_path)
    o = await _generate(conn, ep["takes"][0], kiwi, offset_s=0.5, exaggeration=0.9)

    ((endpoint, args),) = fal.submitted
    assert endpoint == config.CHATTERBOX_ENDPOINT
    assert args["text"] == "THE OVEN IS ON FIRE!" and args["exaggeration"] == 0.9
    assert args["audio_url"].endswith(".wav") and "voice" not in args  # cloned, not a preset
    assert o["status"] == "done" and o["duration_s"] == pytest.approx(0.6, abs=0.02)
    assert "/overdubs/" in o["audio_url"]
    rows = conn.execute("SELECT kind, amount_usd, episode_id, character_id, take_id FROM spend").fetchall()
    assert [tuple(r) for r in rows] == [
        (
            "tts",
            pytest.approx(paidgen.tts_cost("THE OVEN IS ON FIRE!")),
            ep["episode_id"],
            kiwi,
            ep["takes"][0],
        )
    ]


def test_create_validation(conn, fal, tmp_path: Path) -> None:
    ep = make_assembly(conn)
    take = ep["takes"][0]
    ts = db.now()
    mute = conn.execute(
        "INSERT INTO characters (name, slug, created_at, updated_at) VALUES ('Mute', 'mute', ?, ?)", (ts, ts)
    ).lastrowid
    with pytest.raises(overdub.OverdubError) as exc:
        overdub.create(conn, take, character_id=mute, text="hi")
    assert exc.value.status_code == 409 and "voice sample" in exc.value.detail
    kiwi = _voiced_character(conn, tmp_path)
    for kw in ({"text": "   "}, {"text": "x" * 301}, {"text": "hi", "offset_s": -1}):
        with pytest.raises(overdub.OverdubError) as exc:
            overdub.create(conn, take, character_id=kiwi, **kw)
        assert exc.value.status_code == 422
    assert fal.submitted == []


def test_line_plays_at_its_offset_within_trims_even_on_a_stripped_take(tmp_path: Path) -> None:
    def clip(i: int, **kw: Any) -> assembly.Clip:
        v, voc, fx = (tmp_path / f"{i}.mp4", tmp_path / f"{i}.vocals.wav", tmp_path / f"{i}.fx.wav")
        lavfi = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i"]
        subprocess.run([*lavfi, "testsrc=size=96x168:rate=24:duration=3", str(v)], check=True)
        subprocess.run([*lavfi, "sine=frequency=440:duration=3", str(voc)], check=True)
        subprocess.run([*lavfi, "sine=frequency=880:duration=3", str(fx)], check=True)
        return assembly.Clip(v, voc, fx, **kw)

    line = overdub.Placed(tone(tmp_path / "line.wav", 0.5, 1000), offset=1.5, volume=1.0, duration=0.5)
    gone = overdub.Placed(tmp_path / "line.wav", offset=0.1, volume=1.0, duration=0.5)  # trimmed away
    clips = [
        clip(0, trim_in=1.0, trim_out=3.0, muted=True, overdubs=(line, gone)),
        clip(1, trim_in=0, trim_out=2),
    ]
    music_path = tone(tmp_path / "music.wav", 10, 150)
    out = tmp_path / "out.mp4"
    cmd = assembly.ffmpeg_command(
        clips,
        out,
        quality="preview",
        fx_volume=1.0,
        music=assembly.Music(music_path, 0, 0.0, 12),  # silent music: checks the input index only
        ass_file=None,
    )
    subprocess.run([c for c in cmd if c not in ("-progress", "pipe:1")], check=True, capture_output=True)
    # Stripped take: silent except the line, which starts 0.5s into the trimmed clip.
    assert _max_db(out, 0.05, 0.35) < -60
    assert _max_db(out, 0.55, 0.4) > -30
    assert _max_db(out, 1.1, 0.8) < -60
    assert _max_db(out, 2.2, 1.5) > -20  # next take keeps its audio


def _band_db(path: Path, start: float, dur: float, freq: int) -> float:
    """Peak level of a narrow band around `freq` (tells the voice tone from the fx/line tones)."""
    import re

    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", str(path),
         "-af", f"bandpass=f={freq}:width_type=q:w=20,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr  # fmt: skip
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", out)
    assert m, out
    return float(m.group(1))


def test_punch_in_silences_take_voice_only_under_the_line(tmp_path: Path) -> None:
    v, voc, fx = (tmp_path / "v.mp4", tmp_path / "v.vocals.wav", tmp_path / "v.fx.wav")
    lavfi = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i"]
    subprocess.run([*lavfi, "testsrc=size=96x168:rate=24:duration=4", str(v)], check=True)
    subprocess.run([*lavfi, "sine=frequency=440:duration=4", str(voc)], check=True)  # take voice
    subprocess.run([*lavfi, "sine=frequency=2500:duration=4", str(fx)], check=True)  # foley
    line = overdub.Placed(tone(tmp_path / "line.wav", 1.0, 1000), 2.0, 1.0, 1.0, replace_voice=True)
    out = tmp_path / "out.mp4"
    cmd = assembly.ffmpeg_command(
        [assembly.Clip(v, voc, fx, 0.5, 4.0, overdubs=(line,))],
        out, quality="preview", fx_volume=1.0, music=None, ass_file=None,
    )  # fmt: skip
    subprocess.run([c for c in cmd if c not in ("-progress", "pipe:1")], check=True, capture_output=True)
    # Line spans take 2.0-3.0s = output 1.5-2.5s (trim_in 0.5).
    assert _band_db(out, 0.3, 1.0, 440) > -25  # voice before the punch
    # Voice silenced under the line (~-21 dB when layered; the floor here is the line's own leakage).
    assert _band_db(out, 1.6, 0.8, 440) < -40
    assert _band_db(out, 2.7, 0.7, 440) > -25  # voice back after it
    assert _band_db(out, 1.6, 0.8, 1000) > -25  # the line plays
    assert _band_db(out, 1.6, 0.8, 2500) > -25  # fx untouched


async def test_alignment_hears_the_line_and_cache_key_follows_placement(
    conn,
    fal,
    fake_heavy,  # noqa: F811
    tmp_path: Path,
) -> None:
    ep = make_assembly(conn)
    take = ep["takes"][0]
    epx = assembly.episode_context(conn, ep["episode_id"])
    before = assembly.load_items(conn, epx)[0].caption_hash
    o = await _generate(conn, take, _voiced_character(conn, tmp_path), offset_s=0.5)
    with_line = assembly.load_items(conn, epx)[0]
    assert with_line.caption_hash != before

    await assembly.align_take(take, with_line.lines, with_line.caption_hash, ep["episode_id"])
    (call,) = fake_heavy
    assert Path(call["vocals"]).name == "vocals+overdubs.wav"

    overdub.update(conn, o["id"], offset_s=1.0)
    moved = assembly.load_items(conn, epx)[0].caption_hash
    assert moved not in (before, with_line.caption_hash)
    overdub.update(conn, o["id"], replace_voice=True)  # punch-in changes what alignment hears
    assert assembly.load_items(conn, epx)[0].caption_hash != moved
    overdub.archive(conn, o["id"])
    assert assembly.load_items(conn, epx)[0].caption_hash == before
