from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frootio import assembly, db, jobs, media
from frootio.app import create_app

from .conftest import make_episode

SLICE = "[Kiwi]: Hello there friend.\n<prose>\n[Lemon]: *sighs* Oh no."


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def _make_take_media(paths: dict[str, str], duration: float, freq: int) -> None:
    for key in ("video", "vocals", "fx"):
        media.abs_path(paths[key]).parent.mkdir(parents=True, exist_ok=True)
    d = str(duration)
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=192x336:rate=24:duration={d}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:duration={d}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-shortest",
        str(media.abs_path(paths["video"])),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:duration={d}",
        "-ar",
        "24000",
        str(media.abs_path(paths["vocals"])),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=amplitude=0.02:duration={d}",
        "-ar",
        "24000",
        str(media.abs_path(paths["fx"])),
    )


def make_assembly(conn, durations=(2.0, 1.5), *, with_stems=True) -> dict[str, Any]:
    series_id, episode_id = make_episode(conn)
    ts = db.now()
    out: dict[str, Any] = {"series_id": series_id, "episode_id": episode_id, "snippets": [], "takes": []}
    for pos, dur in enumerate(durations):
        sid = conn.execute(
            "INSERT INTO snippets (episode_id, position, script_slice, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (episode_id, pos, SLICE, ts, ts),
        ).lastrowid
        tid = conn.execute(
            "INSERT INTO takes (snippet_id, episode_id, status, requested_duration_s, created_at, updated_at)"
            " VALUES (?, ?, 'done', 5, ?, ?)",
            (sid, episode_id, ts, ts),
        ).lastrowid
        paths = media.take_paths("test-series", 1, sid, tid)
        _make_take_media(paths, dur, 300 + 100 * pos)
        conn.execute(
            "UPDATE takes SET video_path = ?, vocals_path = ?, fx_path = ? WHERE id = ?",
            (
                paths["video"],
                paths["vocals"] if with_stems else None,
                paths["fx"] if with_stems else None,
                tid,
            ),
        )
        conn.execute("UPDATE snippets SET selected_take_id = ? WHERE id = ?", (tid, sid))
        out["snippets"].append(sid)
        out["takes"].append(tid)
    conn.commit()
    return out


def add_music(conn, series_id: int, duration: float = 8.0) -> int:
    rel = "series/test-series/music/track.mp3"
    media.abs_path(rel).parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=150:duration={duration}", str(media.abs_path(rel)))
    tid = conn.execute(
        "INSERT INTO music_tracks (series_id, name, path, created_at) VALUES (?, 'track', ?, ?)",
        (series_id, rel, db.now()),
    ).lastrowid
    conn.commit()
    return tid


def fake_words(lines: list[str]) -> list[dict[str, Any]]:
    words, t = [], 0.1
    for li, line in enumerate(lines):
        for w in line.split():
            words.append({"word": w, "start": round(t, 3), "end": round(t + 0.2, 3), "line": li})
            t += 0.25
    return words


@pytest.fixture
def fake_heavy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def run_heavy(task: str, args: dict[str, Any]) -> dict[str, Any]:
        assert task == "align"
        calls.append(args)
        return {"words": fake_words(args["lines"]), "method": "aligned", "device": "cpu", "error": None}

    monkeypatch.setattr(jobs, "run_heavy", run_heavy)
    return calls


def ffprobe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def wait_job(job_id: int, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        assert job is not None
        if job["status"] in jobs.TERMINAL:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# --- pure graph ----------------------------------------------------------------------


def _clips(n: int = 2) -> list[assembly.Clip]:
    return [
        assembly.Clip(
            Path(f"/m/v{i}.mp4"), Path(f"/m/v{i}.vocals.wav"), Path(f"/m/v{i}.fx.wav"), 0.25 * i, 4.0 + i
        )
        for i in range(n)
    ]


def test_graph_two_takes_without_music_or_captions() -> None:
    inputs, graph = assembly.build_graph(_clips(), fx_volume=0.8, music=None, ass_file=None)
    assert inputs.count("-i") == 6 and "-ss" not in inputs
    chains = graph.split(";")
    assert "[0:v]trim=start=0:end=4,setpts=PTS-STARTPTS" in chains[0]
    assert "[3:v]trim=start=0.25:end=5," in graph
    # vocals and fx are trimmed exactly like their video
    assert "[4:a]atrim=start=0.25:end=5," in graph and "[5:a]atrim=start=0.25:end=5," in graph
    assert "[v0][voc0][fx0][v1][voc1][fx1]concat=n=2:v=1:a=2[vcat][voccat][fxcat]" in chains
    assert "[fxcat]volume=0.8[fxv]" in chains
    assert "[voccat][fxv]amix=inputs=2:duration=first:normalize=0[amixed]" in chains
    assert "sidechaincompress" not in graph and "ass=" not in graph
    assert chains[-1] == "[vcat]null[vout]"


def test_graph_two_takes_with_music_and_captions() -> None:
    music = assembly.Music(Path("/m/music.mp3"), offset=12.5, volume=0.4, duck_db=10)
    inputs, graph = assembly.build_graph(
        _clips(), fx_volume=1.0, music=music, ass_file="captions.ass", out_size=(432, 756)
    )
    assert inputs[-4:] == ["-ss", "12.5", "-i", "/m/music.mp3"]
    chains = graph.split(";")
    assert "[voccat]asplit=2[vocmix][vocsc]" in chains
    assert any(c.startswith("[6:a]") and c.endswith("volume=0.4[mus]") for c in chains)
    duck = next(c for c in chains if "sidechaincompress" in c)
    assert duck.startswith("[mus][vocsc]sidechaincompress=threshold=0.01:ratio=2:")
    assert "[vocmix][fxv][ducked]amix=inputs=3:duration=first:normalize=0[amixed]" in chains
    assert chains[-1] == "[vcat]ass=filename=captions.ass,scale=432:756[vout]"


def test_duck_amount_maps_to_ratio() -> None:
    assert assembly.duck_params(0)["ratio"] == 1.0
    assert assembly.duck_params(10)["ratio"] == 2.0
    assert assembly.duck_params(15)["ratio"] == 4.0
    assert assembly.duck_params(30)["ratio"] == 20.0
    ratios = [assembly.duck_params(d)["ratio"] for d in range(0, 25)]
    assert ratios == sorted(ratios)


def test_graph_rejects_empty() -> None:
    with pytest.raises(ValueError):
        assembly.build_graph([], fx_volume=1, music=None, ass_file=None)


def _max_db(path: Path, start: float, dur: float) -> float:
    import re
    import subprocess

    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr  # fmt: skip
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", out)
    assert m, out
    return float(m.group(1))


def test_muted_take_renders_silent_while_neighbours_keep_audio(tmp_path: Path) -> None:
    import subprocess

    def make(i: int) -> assembly.Clip:
        v, voc, fx = (tmp_path / f"{i}.mp4", tmp_path / f"{i}.vocals.wav", tmp_path / f"{i}.fx.wav")
        lavfi = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i"]
        subprocess.run([*lavfi, "testsrc=size=96x168:rate=24:duration=2", str(v)], check=True)
        subprocess.run([*lavfi, "sine=frequency=440:duration=2", str(voc)], check=True)
        subprocess.run([*lavfi, "sine=frequency=880:duration=2", str(fx)], check=True)
        return assembly.Clip(v, voc, fx, 0.0, 2.0, muted=(i == 1))

    out = tmp_path / "out.mp4"
    cmd = assembly.ffmpeg_command(
        [make(0), make(1), make(2)], out, quality="preview", fx_volume=1.0, music=None, ass_file=None
    )
    subprocess.run([c for c in cmd if c not in ("-progress", "pipe:1")], check=True, capture_output=True)
    assert _max_db(out, 0.3, 1.4) > -20
    assert _max_db(out, 2.3, 1.4) < -60
    assert _max_db(out, 4.3, 1.4) > -20


# --- spec, hash, cache invalidation --------------------------------------------------------


def _store_alignments(conn, ep: dict[str, Any], words_fn=fake_words) -> None:
    for item in assembly.load_items(conn, assembly.episode_context(conn, ep["episode_id"])):
        assembly.store_alignment(
            conn, item.take["id"], item.caption_hash, "\n".join(item.lines), words_fn(item.lines), "aligned"
        )


def test_spec_requires_alignment_and_hash_tracks_inputs(conn) -> None:
    ep = make_assembly(conn)
    eid = ep["episode_id"]
    spec = assembly.build_spec(conn, eid)
    assert spec.hash is None and len(spec.missing_alignments) == 2
    assert spec.total_duration == pytest.approx(3.5, abs=0.05)
    off = assembly.build_spec(conn, eid, captions_on=False)
    assert off.hash is not None and not off.missing_alignments

    _store_alignments(conn, ep)
    h0 = assembly.build_spec(conn, eid).hash
    assert h0 is not None and h0 == assembly.build_spec(conn, eid).hash and h0 != off.hash

    conn.execute("UPDATE takes SET trim_out_s = 1.8 WHERE id = ?", (ep["takes"][0],))
    conn.commit()
    h1 = assembly.build_spec(conn, eid).hash
    assert h1 != h0

    conn.execute("UPDATE episodes SET fx_volume = 0.5 WHERE id = ?", (eid,))
    conn.commit()
    h2 = assembly.build_spec(conn, eid).hash
    assert h2 != h1

    track = add_music(conn, ep["series_id"])
    conn.execute("UPDATE episodes SET music_track_id = ? WHERE id = ?", (track, eid))
    conn.commit()
    h3 = assembly.build_spec(conn, eid).hash
    assert h3 != h2

    # new caption text (edited script slice) needs a new alignment, so no hash until aligned
    conn.execute(
        "UPDATE snippets SET script_slice = '[Kiwi]: Different words.' WHERE id = ?", (ep["snippets"][0],)
    )
    conn.commit()
    assert assembly.build_spec(conn, eid).hash is None
    _store_alignments(conn, ep, lambda lines: [dict(w, start=w["start"] + 0.05) for w in fake_words(lines)])
    assert assembly.build_spec(conn, eid).hash not in (None, h3)


def test_spec_reports_blocking_problems(conn) -> None:
    ep = make_assembly(conn, with_stems=False)
    conn.execute("UPDATE snippets SET selected_take_id = NULL WHERE id = ?", (ep["snippets"][1],))
    conn.commit()
    with pytest.raises(assembly.NotReady) as err:
        assembly.build_spec(conn, ep["episode_id"])
    text = " | ".join(err.value.problems)
    assert "snippet 1" in text and "vocals stem missing" in text and "fx stem missing" in text
    assert "snippet 2" in text and "no selected take" in text


def test_effective_trims_clamp() -> None:
    assert assembly.effective_trims({"trim_in_s": 0.5, "trim_out_s": None}, 4.0) == (0.5, 4.0)
    assert assembly.effective_trims({"trim_in_s": 0, "trim_out_s": 9.0}, 4.0) == (0.0, 4.0)
    assert assembly.effective_trims({"trim_in_s": 5, "trim_out_s": None}, 4.0) == (4.0, 4.0)


def test_peaks_cached_until_stem_changes(conn) -> None:
    ep = make_assembly(conn, durations=(1.0,))
    item = assembly.load_items(conn, assembly.episode_context(conn, ep["episode_id"]))[0]
    rel = assembly.ensure_peaks(conn, item.take, item.paths)
    data = json.loads(media.abs_path(rel).read_text())
    assert data["per_second"] == assembly.PEAKS_PER_SECOND
    assert len(data["peaks"]) == pytest.approx(50, abs=1) and max(data["peaks"]) == 1.0
    mtime = media.abs_path(rel).stat().st_mtime_ns
    item.take["peaks_path"] = rel
    assert assembly.ensure_peaks(conn, item.take, item.paths) == rel
    assert media.abs_path(rel).stat().st_mtime_ns == mtime
    assert conn.execute("SELECT peaks_path FROM takes WHERE id = ?", (item.take["id"],)).fetchone()[0] == rel


# --- HTTP API ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    with TestClient(create_app(dist)) as c:
        yield c


def test_prepare_peaks_alignment_and_missing_report(client, conn, fake_heavy) -> None:
    ep = make_assembly(conn)
    eid = ep["episode_id"]
    ts = db.now()
    lonely = conn.execute(
        "INSERT INTO snippets (episode_id, position, created_at, updated_at) VALUES (?, 5, ?, ?)",
        (eid, ts, ts),
    ).lastrowid
    conn.commit()
    r = client.post(f"/api/episodes/{eid}/assembly/prepare").json()
    assert r["missing_selected_takes"] == [3] and r["missing_stems"] == []
    jobs_ids = [i["align_job_id"] for i in r["items"][:2]]
    assert all(jobs_ids) and r["items"][2]["snippet_id"] == lonely
    for j in jobs_ids:
        assert wait_job(j)["status"] == "done"
    assert [c["lines"] for c in fake_heavy] == [["Hello there friend.", "Oh no."]] * 2

    state = client.get(f"/api/episodes/{eid}/assembly").json()
    first = state["items"][0]
    assert first["captions"]["status"] == "ready"
    assert [w["word"] for w in first["captions"]["words"]] == ["Hello", "there", "friend.", "Oh", "no."]
    assert first["take"]["peaks_url"].endswith(".peaks.json")
    assert state["items"][2]["problems"] == ["no selected take"]

    # cached per take + caption text: a second prepare queues nothing
    r2 = client.post(f"/api/episodes/{eid}/assembly/prepare").json()
    assert [i["align_job_id"] for i in r2["items"]] == [None, None, None]
    assert len(fake_heavy) == 2


def test_trim_validation(client, conn) -> None:
    ep = make_assembly(conn)
    tid = ep["takes"][0]
    url = f"/api/takes/{tid}/trim"
    assert client.patch(url, json={"trim_in_s": 0.3, "trim_out_s": 1.7}).json()["trim_out_s"] == 1.7
    assert client.patch(url, json={"trim_in_s": 1.0, "trim_out_s": 1.1}).status_code == 422
    assert client.patch(url, json={"trim_in_s": 0, "trim_out_s": 9}).status_code == 422
    assert client.patch(url, json={"trim_in_s": -1, "trim_out_s": None}).status_code == 422
    r = client.patch(url, json={"trim_in_s": 0.2, "trim_out_s": None})
    assert r.status_code == 200 and r.json()["trim_out_s"] is None
    assert client.patch("/api/takes/999/trim", json={"trim_in_s": 0}).status_code == 404
    row = conn.execute("SELECT trim_in_s, trim_out_s FROM takes WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == (0.2, None)


def test_settings_patch_validation(client, conn) -> None:
    ep = make_assembly(conn, durations=(1.0,))
    eid = ep["episode_id"]
    track = add_music(conn, ep["series_id"], duration=5.0)
    other_series = conn.execute(
        "INSERT INTO series (name, slug, created_at, updated_at) VALUES ('O', 'o', ?, ?)",
        (db.now(), db.now()),
    ).lastrowid
    foreign = conn.execute(
        "INSERT INTO music_tracks (series_id, name, path, created_at) VALUES (?, 'x', 'x.mp3', ?)",
        (other_series, db.now()),
    ).lastrowid
    conn.commit()
    url = f"/api/episodes/{eid}/assembly/settings"
    s = client.patch(
        url,
        json={
            "music_track_id": track,
            "music_offset_s": 2,
            "music_volume": 0.5,
            "music_duck_db": 9,
            "fx_volume": 0.7,
            "captions_on": False,
        },
    ).json()
    assert s == {
        "music_track_id": track,
        "music_offset_s": 2.0,
        "music_volume": 0.5,
        "music_duck_db": 9.0,
        "fx_volume": 0.7,
        "captions_on": False,
    }
    assert client.patch(url, json={"music_track_id": foreign}).status_code == 422
    assert client.patch(url, json={"music_offset_s": 6}).status_code == 422
    assert client.patch(url, json={"music_volume": 3}).status_code == 422
    assert client.patch(url, json={"fx_volume": None}).status_code == 422
    assert client.patch(url, json={"music_track_id": None}).json()["music_track_id"] is None
    assert client.patch("/api/episodes/999/assembly/settings", json={}).status_code == 404


def test_preview_renders_then_reuses_until_inputs_change(client, conn, fake_heavy) -> None:
    ep = make_assembly(conn)
    eid = ep["episode_id"]
    track = add_music(conn, ep["series_id"])
    client.patch(f"/api/episodes/{eid}/assembly/settings", json={"music_track_id": track})
    r = client.post(f"/api/episodes/{eid}/assembly/preview").json()
    assert r["reused"] is False
    job = wait_job(r["job_id"])
    assert job["status"] == "done", job["error"]
    assert len(fake_heavy) == 2  # alignment ran inside the preview job
    state = client.get(f"/api/episodes/{eid}/assembly").json()["preview"]
    assert state["fresh"] is True
    path = media.abs_path(state["path"])
    info = ffprobe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (assembly.PREVIEW_W, assembly.PREVIEW_H)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(3.5, abs=0.15)

    assert client.post(f"/api/episodes/{eid}/assembly/preview").json() == {
        "reused": True,
        "job_id": None,
        "url": state["url"],
    }

    client.patch(f"/api/takes/{ep['takes'][1]}/trim", json={"trim_in_s": 0.5, "trim_out_s": None})
    assert client.get(f"/api/episodes/{eid}/assembly").json()["preview"]["fresh"] is False
    r = client.post(f"/api/episodes/{eid}/assembly/preview").json()
    assert r["reused"] is False and wait_job(r["job_id"])["status"] == "done"
    assert float(ffprobe(path)["format"]["duration"]) == pytest.approx(3.0, abs=0.15)

    r = client.patch(f"/api/takes/{ep['takes'][0]}/audio", json={"muted": True})
    assert r.status_code == 200
    state = client.get(f"/api/episodes/{eid}/assembly").json()
    assert state["preview"]["fresh"] is False and state["items"][0]["take"]["audio_muted"] is True
    r = client.post(f"/api/episodes/{eid}/assembly/preview").json()
    assert r["reused"] is False and wait_job(r["job_id"])["status"] == "done"

    client.patch(f"/api/episodes/{eid}/assembly/settings", json={"captions_on": False})
    assert client.get(f"/api/episodes/{eid}/assembly").json()["preview"]["fresh"] is False


def test_preview_and_export_blocked_when_not_ready(client, conn) -> None:
    ep = make_assembly(conn, with_stems=False)
    r = client.post(f"/api/episodes/{ep['episode_id']}/assembly/preview")
    assert r.status_code == 409 and "vocals stem missing" in " ".join(r.json()["detail"]["problems"])
    assert client.post(f"/api/episodes/{ep['episode_id']}/assembly/export").status_code == 409


def test_export_renders_768p_and_records_it(client, conn, fake_heavy) -> None:
    ep = make_assembly(conn, durations=(1.2, 1.0))
    eid = ep["episode_id"]
    r = client.post(f"/api/episodes/{eid}/assembly/export", json={"captions_on": True})
    assert r.status_code == 201
    job = wait_job(r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    exports = client.get(f"/api/episodes/{eid}/assembly/exports").json()
    assert len(exports) == 1 and exports[0]["status"] == "done"
    assert exports[0]["path"].startswith("series/test-series/episodes/1/exports/")
    assert exports[0]["settings"]["takes"][0]["take_id"] == ep["takes"][0]
    info = ffprobe(media.abs_path(exports[0]["path"]))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (768, 1344)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
