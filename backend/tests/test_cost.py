from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frootio import cost, db, spend

from .genkit import make_world


def test_h3_cost_formula() -> None:
    # Under the 4096 included reference tokens: duration only, billed on requested seconds.
    assert cost.h3_cost(5, 1368) == pytest.approx(0.40)
    assert cost.h3_cost(15, 4096) == pytest.approx(1.20)
    # 6000 tokens: 1904 extra at $0.02 / 1000, prorated.
    assert cost.h3_cost(10, 6000) == pytest.approx(0.8 + 1904 * 0.02 / 1000)


@pytest.mark.parametrize(
    ("w", "h", "cls", "tokens"),
    [
        (1024, 1024, "1:1", 1024),
        (768, 1344, "9:16", 1824),  # 0.571 -> nearest 9:16
        (1344, 768, "16:9", 1824),
        (1200, 900, "4:3", 1376),
        (900, 1200, "3:4", 1376),
        (1000, 400, "5:2", 2560),
        (400, 1000, "2:5", 2560),
        (2000, 1000, "16:9", 1824),  # 2:1 is closer to 16:9 than 5:2 on a log scale
        (1100, 1000, "1:1", 1024),
    ],
)
def test_image_tokens_by_nearest_aspect_class(w: int, h: int, cls: str, tokens: int) -> None:
    assert cost.aspect_class(w, h) == cls
    assert cost.image_tokens(w, h) == tokens


def test_audio_tokens_round_up() -> None:
    assert cost.audio_tokens(4.3) == 344
    assert cost.audio_tokens(2.0) == 160
    assert cost.audio_tokens(2.501) == 201


def test_ref_tokens_probe_real_files(conn: sqlite3.Connection, tmp_path: Path) -> None:
    w = make_world(conn, tmp_path)
    refs = [
        {"kind": "image", "path": w.image_rel, "sha": "a"},
        {"kind": "audio", "path": w.voice_rel, "sha": "b"},
    ]
    assert cost.ref_token_counts(refs) == [1024, 200]  # 1024x1024 png, 2.5s wav
    est = cost.estimate(refs, 5, n=3)
    assert est.ref_tokens == 1224 and est.per_take_usd == pytest.approx(0.40)
    assert est.total_usd == pytest.approx(1.20)


def test_ref_tokens_missing_file_raises(env: Path) -> None:
    with pytest.raises(Exception, match="missing media file"):
        cost.ref_tokens([{"kind": "image", "path": "characters/x/images/nope.png", "sha": "z"}])


def test_cap_counts_video_spend_and_in_flight_only(conn: sqlite3.Connection, tmp_path: Path) -> None:
    w = make_world(conn, tmp_path)
    spend.log(conn, "video", 10.0, episode_id=w.episode_id)
    spend.log(conn, "image", 50.0, episode_id=w.episode_id)  # other kinds never count
    ts = db.now()
    sid = conn.execute(
        "INSERT INTO snippets (episode_id, created_at, updated_at) VALUES (?, ?, ?)", (w.episode_id, ts, ts)
    ).lastrowid
    for status, est in [("queued", 2.0), ("running", 3.0), ("failed", 7.0), ("done", 9.0)]:
        conn.execute(
            "INSERT INTO takes (snippet_id, episode_id, status, requested_duration_s, estimated_cost_usd,"
            " created_at, updated_at) VALUES (?, ?, ?, 5, ?, ?, ?)",
            (sid, w.episode_id, status, est, ts, ts),
        )
    conn.commit()
    ok = cost.cap_status(conn, w.episode_id, 5.0)
    assert (ok.video_spend_usd, ok.in_flight_usd, ok.projected_usd) == (10.0, 5.0, 20.0)
    assert not ok.exceeded  # exactly at the cap is allowed
    over = cost.cap_status(conn, w.episode_id, 5.01)
    assert over.exceeded and "$20.00" in over.message()
