from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from frootio import db, jobs, openrouter, script, snippets, split

from .conftest import make_episode
from .library_fixtures import add_location, kiwi_and_lemon_cast

W5 = "a b c d e"  # 5 words = 2.0s


def line(speaker: str, words: int) -> str:
    return f"[{speaker}]: " + " ".join(["w"] * words)


def chunks(text: str, speakers: script.SpeakerMap | None = None) -> list[list[int]]:
    return [[ln.number for ln in c.lines] for c in split.chunk_lines(script.parse(text, speakers or {}))]


def test_packs_up_to_exactly_15s_then_breaks() -> None:
    # 3 lines of 12.5 words? use 5s lines: 12.5 words isn't whole, so 10 words = 4s and 5 words = 2s.
    text = "\n".join([line("A", 10)] * 3 + [line("A", 5)] + [line("A", 5)])  # 4+4+4+2 = 14, +2 = 16
    assert chunks(text) == [[1, 2, 3, 4], [5]]
    exact = "\n".join([line("A", 10)] * 3 + [line("A", 5), "Prose beat."])  # 14 + 1 = 15 exactly
    assert chunks(exact) == [[1, 2, 3, 4, 5]]


def test_fourth_speaker_starts_a_new_snippet() -> None:
    text = "\n".join([line("A", 1), line("B", 1), line("C", 1), line("A", 1), line("D", 1), line("B", 1)])
    assert chunks(text) == [[1, 2, 3, 4], [5, 6]]


def test_unresolved_and_resolved_speakers_both_count(conn: sqlite3.Connection) -> None:
    series_id, _ = make_episode(conn)
    kiwi_and_lemon_cast(conn, series_id)
    sp = script.speaker_map(conn, series_id)
    text = "[Kiwi]: hi\n[Kiwi Kowalski]: hi\n[Lemon]: hi\n[Mango]: hi\n[Nobody]: hi"
    assert chunks(text, sp) == [[1, 2, 3, 4], [5]]


def test_hard_break_and_blank_lines() -> None:
    text = f"[A]: {W5}\n\n[B]: {W5}\n-----\n\n[A]: {W5}\n\n"
    # Blank line 2 stays inside the first snippet; trailing blanks are trimmed; the break splits.
    assert chunks(text) == [[1, 2, 3], [6]]


def test_oversize_line_gets_own_snippet_with_warning() -> None:
    text = f"[A]: {W5}\n{line('B', 40)}\n[A]: {W5}"  # 40 words = 16s
    cs = split.chunk_lines(script.parse(text, {}))
    assert [[ln.number for ln in c.lines] for c in cs] == [[1], [2], [3]]
    assert cs[1].warnings and "Line 2" in cs[1].warnings[0]
    new = split.level0(script.parse(text, {}), None)
    assert [n.target_duration_s for n in new] == [5, 15, 5]
    assert new[1].warnings == cs[1].warnings and new[0].warnings == []


def test_target_duration_is_ceil_clamped() -> None:
    assert snippets.clamp_duration(0.4) == 5
    assert snippets.clamp_duration(7.2) == 8
    assert snippets.clamp_duration(8.0) == 8
    assert snippets.clamp_duration(15.0) == 15
    assert snippets.clamp_duration(22) == 15


def test_run_level0_replaces_board(conn: sqlite3.Connection) -> None:
    series_id, ep = make_episode(conn)
    cast = kiwi_and_lemon_cast(conn, series_id)
    office = add_location(conn, series_id, "Law office", description="A cramped office")
    conn.execute("UPDATE episodes SET default_location_id = ? WHERE id = ?", (office, ep))
    old = snippets.insert(conn, ep, snippets.NewSnippet(body="old"))
    conn.commit()
    text = (
        "Bakery. Mango slides a tray over.\n"
        "[Mango]: Lemon, your cake is finally ready.\n"
        "[Lemon]: Oh my god...\n"
        "\n"
        "[Kiwi]: *checking watch* Is it chocolate?\n"
        "-----\n"
        "[Kiwi]: Hey!"
    )
    script.save_version(conn, ep, text)
    out = split.run_level0(conn, ep)
    assert conn.execute("SELECT archived FROM snippets WHERE id = ?", (old,)).fetchone()[0] == 1
    act = snippets.active(conn, ep)
    assert [s["id"] for s in act] == out["snippet_ids"] and len(act) == 2
    first = act[0]
    assert first["script_slice"] == "\n".join(text.split("\n")[:5])
    assert first["body"] == first["script_slice"]
    assert first["location_id"] == office and first["origin"] == "split0"
    # 1 + 7/2.5 + 3/2.5(... is 2 tokens+...) -> computed by the estimator, ceil + clamp
    assert first["target_duration_s"] == snippets.clamp_duration(script.estimate(first["script_slice"]))
    cast_ids = [
        r[0]
        for r in conn.execute(
            "SELECT character_id FROM snippet_cast WHERE snippet_id = ? ORDER BY position", (first["id"],)
        )
    ]
    assert cast_ids == [cast["mango"], cast["lemon"], cast["kiwi"]]
    assert out["estimated_total_s"] == sum(s["target_duration_s"] for s in act)
    assert out["length_warning"] and "30-45s" in out["length_warning"]
    prop = conn.execute("SELECT * FROM split_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    assert prop["level"] == 0 and prop["status"] == "accepted"


def test_level0_needs_a_saved_script(conn: sqlite3.Connection) -> None:
    _, ep = make_episode(conn)
    with pytest.raises(split.SplitError, match="no saved script"):
        split.run_level0(conn, ep)


# --- level 2 --------------------------------------------------------------------


@pytest.fixture
def l2(conn: sqlite3.Connection):
    series_id, ep = make_episode(conn)
    cast = kiwi_and_lemon_cast(conn, series_id)
    office = add_location(conn, series_id, "Law office", description="A cramped office")
    text = "Kiwi: hey there\n[Lemon]: *flatly* Hi Kiwi.\n[Mango]: Sit down, both of you."
    script.save_version(conn, ep, text)
    return {"ep": ep, "cast": cast, "office": office, "text": text, "ctx": split.context(conn, ep)}


def good_output(**over) -> dict:
    snip = {
        "script_slice": (
            "[Kiwi Kowalski]: hey there\n[Lemon]: *flatly* Hi Kiwi.\n[Mango]: Sit down, both of you."
        ),
        "cast": ["Kiwi Kowalski", "Lemon Lee", "Mango"],
        "location": "law office",
        "duration_s": 8,
        "body": '0-3s: Kiwi Kowalski says "hey there"\n3-8s: ...',
    }
    snip.update(over)
    return {"snippets": [snip]}


def test_validate_accepts_good_output(l2) -> None:
    result, errors, warnings = split.validate_l2(good_output(), l2["ctx"], l2["text"])
    assert errors == [] and warnings == []
    (s,) = result
    assert [c["name"] for c in s["cast"]] == ["Kiwi Kowalski", "Lemon Lee", "Mango"]
    assert s["location"] == {"id": l2["office"], "name": "Law office"}
    assert s["target_duration_s"] == 8


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"duration_s": 16}, "duration_s"),
        ({"duration_s": "8"}, "duration_s"),
        ({"cast": ["Grape"]}, "unknown cast member"),
        ({"location": "Moon"}, "unknown location"),
        ({"body": ""}, "body"),
        ({"script_slice": "[Grape]: hi"}, "not a cast name"),
        (
            {"script_slice": "[Kiwi]: a\n[Lemon]: b\n[Mango]: c\n[Plum]: d"},
            "at most 3 per snippet",
        ),
    ],
)
def test_validate_rejects(l2, over: dict, message: str) -> None:
    _, errors, _ = split.validate_l2(good_output(**over), l2["ctx"], l2["text"])
    assert any(message in e for e in errors), errors


def test_validate_rejects_wrong_shape(l2) -> None:
    for data in ([], {"snippets": []}, {"nope": 1}):
        assert split.validate_l2(data, l2["ctx"], l2["text"])[1]


def test_validate_warns_on_missing_dialogue_and_adds_speakers_to_cast(l2) -> None:
    out = good_output(script_slice="[Lemon]: *flatly* Hi Kiwi.", cast=[])
    result, errors, warnings = split.validate_l2(out, l2["ctx"], l2["text"])
    assert errors == []
    assert [c["name"] for c in result[0]["cast"]] == ["Lemon Lee"]
    assert any("Script line 3" in w for w in warnings)


def test_extract_json_handles_fences() -> None:
    assert split.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert split.extract_json('Here you go: {"a": 1} hope it helps') == {"a": 1}


def fake_chat(replies: list[str], seen: list):
    async def chat(messages, **kwargs):
        seen.append((messages, kwargs))
        return openrouter.ChatResult(replies.pop(0), 0.01, kwargs.get("model") or "m", {}, {})

    return chat


async def run_job(conn: sqlite3.Connection, proposal_id: int) -> None:
    job_id = conn.execute("SELECT job_id FROM split_proposals WHERE id = ?", (proposal_id,)).fetchone()[0]
    conn.execute("UPDATE jobs SET status = 'running', attempts = 1 WHERE id = ?", (job_id,))
    conn.commit()
    await split.split_l2_job(job_id, {"proposal_id": proposal_id})


async def test_level2_repairs_once_logs_spend_and_accepts(conn, l2, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(
        split.openrouter, "chat", fake_chat(["not json at all", json.dumps(good_output())], seen)
    )
    old = snippets.insert(conn, l2["ep"], snippets.NewSnippet(body="old"))
    conn.commit()
    pid = split.start_level2(conn, l2["ep"], model="test/model")
    await run_job(conn, pid)
    assert len(seen) == 2
    repair = seen[1][0]
    assert repair[-1]["role"] == "user" and "not valid JSON" in repair[-1]["content"]
    assert seen[0][1]["model"] == "test/model"
    user_msg = seen[0][0][1]["content"]
    assert "Lemon Lee" in user_msg and "Law office" in user_msg and l2["text"] in user_msg
    p = split.proposal_dict(
        conn, conn.execute("SELECT * FROM split_proposals WHERE id = ?", (pid,)).fetchone()
    )
    assert p["status"] == "pending" and len(p["result"]) == 1 and p["cost_usd"] == pytest.approx(0.02)
    spent = conn.execute("SELECT kind, amount_usd, episode_id FROM spend").fetchall()
    assert [(r[0], r[2]) for r in spent] == [("llm", l2["ep"])] * 2
    # board untouched until accepted
    assert [s["id"] for s in snippets.active(conn, l2["ep"])] == [old]
    ids = split.accept(conn, pid)
    act = snippets.active(conn, l2["ep"])
    assert [s["id"] for s in act] == ids and act[0]["origin"] == "split2"
    assert act[0]["target_duration_s"] == 8 and act[0]["location_id"] == l2["office"]
    assert split.get_model(conn) == "test/model"
    with pytest.raises(split.SplitError):
        split.accept(conn, pid)


async def test_level2_fails_after_second_invalid_output(conn, l2, monkeypatch) -> None:
    bad = json.dumps(good_output(duration_s=40))
    monkeypatch.setattr(split.openrouter, "chat", fake_chat([bad, bad], []))
    pid = split.start_level2(conn, l2["ep"])
    with pytest.raises(split.SplitError, match="invalid after one repair"):
        await run_job(conn, pid)
    p = conn.execute("SELECT status, error, cost_usd FROM split_proposals WHERE id = ?", (pid,)).fetchone()
    assert p["status"] == "failed" and "duration_s" in p["error"] and p["cost_usd"] == pytest.approx(0.02)


async def test_level2_resumed_job_does_not_call_again(conn, l2, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(split.openrouter, "chat", fake_chat([], seen))
    pid = split.start_level2(conn, l2["ep"])
    job_id = conn.execute("SELECT job_id FROM split_proposals WHERE id = ?", (pid,)).fetchone()[0]
    conn.execute("UPDATE jobs SET status = 'running', attempts = 2 WHERE id = ?", (job_id,))
    conn.commit()
    await split.split_l2_job(job_id, {"proposal_id": pid})
    assert seen == []
    assert conn.execute("SELECT status FROM split_proposals WHERE id = ?", (pid,)).fetchone()[0] == "failed"


def test_reject_cancels_job(conn, l2) -> None:
    pid = split.start_level2(conn, l2["ep"])
    split.reject(conn, pid)
    row = conn.execute("SELECT status, job_id FROM split_proposals WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "rejected"
    assert jobs.get(row["job_id"])["status"] == "cancelled"


async def test_rewrite_body_returns_text_and_logs_spend(conn, l2, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(split.openrouter, "chat", fake_chat(["```\n0-5s: New body\n```"], seen))
    sid = snippets.insert(
        conn,
        l2["ep"],
        snippets.NewSnippet(body="old body", script_slice="[Lemon]: Hi", cast_ids=[l2["cast"]["lemon"]]),
    )
    conn.commit()
    out = await split.rewrite_body(conn, sid, instructions="more close-ups")
    assert out["body"] == "0-5s: New body"
    assert "more close-ups" in seen[0][0][1]["content"] and "Lemon Lee" in seen[0][0][1]["content"]
    assert conn.execute("SELECT body FROM snippets WHERE id = ?", (sid,)).fetchone()[0] == "old body"
    assert conn.execute("SELECT COUNT(*) FROM spend WHERE kind = 'llm'").fetchone()[0] == 1


def test_db_path_is_temp(env: Path) -> None:
    assert str(db.connect().execute("PRAGMA database_list").fetchone()[2]).startswith(str(env))
