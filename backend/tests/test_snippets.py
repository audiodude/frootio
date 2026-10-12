from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frootio import continuity, db, prompt, snippets
from frootio.app import create_app

from .conftest import make_episode
from .library_fixtures import add_location, add_take, kiwi_and_lemon_cast

SAMPLE_SCRIPT = """\
Mango's bakery, early morning. Flour everywhere.
[Mango]: *dusting off apron* The oven has been making that noise all week.
[Kiwi]: Maybe it's haunted.
[Lemon]: *rolling eyes* It's not haunted, Kiwi. It's old.
<close up of the oven door rattling>
[Kiwi]: Old AND haunted.

-----

Ms. Plum appears in the doorway holding a clipboard.
[Ms. Plum]: *crisply* Inspection is at noon. Every tray, spotless.
[Mango]: Noon? That's three hours!
[Ms. Plum]: Two hours and fifty minutes.
[Lemon]: We can do it. *grabs a sponge* Kiwi, the windows.

-----

The back room. Kiwi wipes a window and finds a note taped behind it.
[Kiwi]: *reading* "Check under the third floorboard."
[Lemon]: That is absolutely not our business.
[Kiwi]: It is now our business.
[Lemon]: *sighs* Fine. Quickly.

-----

Noon. Ms. Plum runs a finger along the counter.
[Ms. Plum]: *inspecting* Acceptable.
[Mango]: Just acceptable?
[Ms. Plum]: From me, that is a standing ovation.
[Kiwi]: *whispering to Lemon* Floorboard. Tonight.
"""


def board(conn: sqlite3.Connection, ep: int) -> list[int]:
    return [s["id"] for s in snippets.active(conn, ep)]


@pytest.fixture
def three(conn: sqlite3.Connection) -> dict:
    _, ep = make_episode(conn)
    ids = [snippets.insert(conn, ep, snippets.NewSnippet(body=f"s{i}")) for i in range(3)]
    conn.commit()
    return {"ep": ep, "ids": ids}


# --- board ---------------------------------------------------------------------


def test_archive_restore_to_end_and_reorder(conn, three) -> None:
    ep, (a, b, c) = three["ep"], three["ids"]
    snippets.archive(conn, a)
    assert board(conn, ep) == [b, c]
    snippets.restore(conn, a)
    assert board(conn, ep) == [b, c, a]
    snippets.reorder(conn, ep, [c, a, b])
    assert board(conn, ep) == [c, a, b]
    with pytest.raises(ValueError):
        snippets.reorder(conn, ep, [c, a])
    with pytest.raises(ValueError):
        snippets.reorder(conn, ep, [c, a, b, b])


def test_copy_copies_prompt_and_metadata(conn, three) -> None:
    series_id = conn.execute("SELECT series_id FROM episodes WHERE id = ?", (three["ep"],)).fetchone()[0]
    cast = kiwi_and_lemon_cast(conn, series_id)
    loc = add_location(conn, series_id, "Kitchen")
    src = three["ids"][0]
    snippets.set_cast(conn, src, [cast["lemon"], cast["kiwi"]])
    conn.execute(
        "UPDATE snippets SET location_id = ?, target_duration_s = 9, script_slice = 'x', tail = 'T'"
        " WHERE id = ?",
        (loc, src),
    )
    prompt.update_parts(conn, src, {"legend": {"text": "frozen"}})
    snippets.archive(conn, src)
    new = snippets.copy(conn, src)
    conn.commit()
    a, b = prompt.build_prompt(conn, src), prompt.build_prompt(conn, new)
    assert a.joined == b.joined and b.parts["legend"].overridden
    s = snippets.get(conn, new)
    assert (s["origin"], s["copied_from_snippet_id"], s["archived"]) == ("copy", src, 0)
    assert board(conn, three["ep"])[-1] == new
    assert [c["id"] for c in snippets.serialize(conn, s)["cast"]] == [cast["lemon"], cast["kiwi"]]


# --- continuity ----------------------------------------------------------------


def test_predecessor_and_first(conn, three) -> None:
    a, b, c = three["ids"]
    assert continuity.predecessor(conn, a) is None
    assert continuity.predecessor(conn, c)["id"] == b
    snippets.archive(conn, b)
    assert continuity.predecessor(conn, c)["id"] == a


def test_blocked_when_previous_has_no_selected_take(conn, three) -> None:
    a, b, _ = three["ids"]
    conn.execute("UPDATE snippets SET continue_from_previous = 1 WHERE id = ?", (b,))
    assert continuity.blocked_reason(conn, b) == continuity.NO_SELECTED_TAKE
    assert continuity.expected_start(conn, b) is None
    ta = add_take(conn, a, trim_out_s=None, video_duration_s=10.4)
    assert continuity.blocked_reason(conn, b) is None
    assert continuity.expected_start(conn, b) == (ta, 10.4)


def test_stale_cases(conn, three) -> None:
    a, b, c = three["ids"]
    conn.execute("UPDATE snippets SET continue_from_previous = 1 WHERE id IN (?, ?)", (b, c))
    ta = add_take(conn, a, trim_out_s=9.5)
    tb = add_take(conn, b, start_frame=(ta, 9.5))
    assert not continuity.is_stale(conn, b)
    # trim-out of the predecessor's selected take changes
    conn.execute("UPDATE takes SET trim_out_s = 9.2 WHERE id = ?", (ta,))
    assert continuity.is_stale(conn, b)
    conn.execute("UPDATE takes SET trim_out_s = 9.5 WHERE id = ?", (ta,))
    assert not continuity.is_stale(conn, b)
    # predecessor's selected take changes
    ta2 = add_take(conn, a, trim_out_s=9.5)
    assert continuity.is_stale(conn, b)
    conn.execute("UPDATE snippets SET selected_take_id = ? WHERE id = ?", (ta, a))
    # no cascade: c started from b's selected take, which hasn't changed
    add_take(conn, c, start_frame=(tb, 10.0))
    assert not continuity.is_stale(conn, c)
    # reordering gives b a different predecessor
    snippets.reorder(conn, three["ep"], [c, b, a])
    assert continuity.is_stale(conn, b)
    # c is now first: toggle has no effect, never stale
    assert not continuity.is_stale(conn, c) and continuity.expected_start(conn, c) is None
    assert ta2


def test_take_without_start_frame_is_stale_when_continuing(conn, three) -> None:
    a, b, _ = three["ids"]
    add_take(conn, a)
    add_take(conn, b)
    assert not continuity.is_stale(conn, b)  # toggle off
    conn.execute("UPDATE snippets SET continue_from_previous = 1 WHERE id = ?", (b,))
    assert continuity.is_stale(conn, b)


# --- HTTP API --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    with TestClient(create_app(dist)) as c:
        yield c


@pytest.fixture
def api_ep(client):
    conn = db.connect()
    try:
        series_id, ep = make_episode(conn)
        cast = kiwi_and_lemon_cast(conn, series_id)
        loc = add_location(conn, series_id, "Law office", description="A cramped office", images=1)
    finally:
        conn.close()
    return {"ep": ep, "cast": cast, "loc": loc, "series": series_id}


def test_api_episode_get_patch(client, api_ep) -> None:
    ep = api_ep["ep"]
    r = client.get(f"/api/episodes/{ep}").json()
    assert r["series"]["slug"] == "test-series" and len(r["cast"]) == 7 and r["spend"]["total_usd"] == 0
    r = client.patch(f"/api/episodes/{ep}", json={"default_location_id": api_ep["loc"], "take_count": 3})
    assert (
        r.status_code == 200
        and r.json()["default_location_id"] == api_ep["loc"]
        and r.json()["take_count"] == 3
    )
    assert client.patch(f"/api/episodes/{ep}", json={"take_count": 0}).status_code == 422
    assert client.patch(f"/api/episodes/{ep}", json={"default_location_id": 999}).status_code == 400
    assert client.get("/api/episodes/999").status_code == 404


def test_api_script_versions_and_lint(client, api_ep) -> None:
    ep = api_ep["ep"]
    assert client.get(f"/api/episodes/{ep}/script").json()["version"] is None
    r = client.post(f"/api/episodes/{ep}/script", json={"text": "Kiwi: hi\n[Lemon]: hey"}).json()
    assert r["created"] and r["version"] == 1 and [w["line"] for w in r["warnings"]] == [1]
    assert (
        client.post(f"/api/episodes/{ep}/script", json={"text": "Kiwi: hi\n[Lemon]: hey"}).json()[
            "created"
        ]
        is False
    )
    client.post(f"/api/episodes/{ep}/script", json={"text": "[Kiwi]: hi"})
    assert [v["version"] for v in client.get(f"/api/episodes/{ep}/script/versions").json()] == [2, 1]
    assert client.get(f"/api/episodes/{ep}/script?version=1").json()["text"].startswith("Kiwi")
    lint = client.post(f"/api/episodes/{ep}/script/lint", json={"text": "[Grape]: hi"}).json()
    assert lint["warnings"][0]["kind"] == "unresolved"


def test_api_level0_on_sample_script_and_board_ops(client, api_ep) -> None:
    ep = api_ep["ep"]
    client.patch(f"/api/episodes/{ep}", json={"default_location_id": api_ep["loc"]})
    client.post(f"/api/episodes/{ep}/script", json={"text": SAMPLE_SCRIPT})
    r = client.post(f"/api/episodes/{ep}/split", json={"level": 0})
    assert r.status_code == 200, r.text
    out = r.json()
    b = client.get(f"/api/episodes/{ep}/snippets").json()
    assert len(b["active"]) == len(out["snippet_ids"]) >= 3
    assert all(5 <= s["target_duration_s"] <= 15 for s in b["active"])
    assert all(len([c for c in s["cast"]]) <= 3 for s in b["active"])
    assert all(s["location_id"] == api_ep["loc"] for s in b["active"])
    first, second = b["active"][0], b["active"][1]
    assert first["is_first"] and not second["is_first"]
    # continue toggle disabled on first; on second it's blocked without a selected take
    assert (
        client.patch(f"/api/snippets/{first['id']}", json={"continue_from_previous": True}).status_code == 400
    )
    s2 = client.patch(f"/api/snippets/{second['id']}", json={"continue_from_previous": True}).json()
    assert s2["continue_from_previous"] and "no selected take" in s2["blocked_reason"]
    assert client.patch(f"/api/snippets/{second['id']}", json={"target_duration_s": 16}).status_code == 422
    # prompt + parts
    p = client.get(f"/api/snippets/{first['id']}/prompt").json()
    assert p["parts"]["legend"]["text"].startswith("Image 1 is ")
    assert p["char_count"] == len(p["joined"]) and p["errors"] == []
    p = client.put(
        f"/api/snippets/{first['id']}/parts",
        json={"parts": {"location": {"text": "Mine", "overridden": True}}},
    ).json()
    assert p["parts"]["location"] == {"text": "Mine", "overridden": True, "source_changed": False}
    p = client.put(
        f"/api/snippets/{first['id']}/parts", json={"parts": {"location": {"overridden": False}}}
    ).json()
    assert p["parts"]["location"]["text"] == "A cramped office" and not p["parts"]["location"]["overridden"]
    # add, reorder, archive (DELETE), restore, copy
    new = client.post(f"/api/episodes/{ep}/snippets").json()
    assert new["origin"] == "manual" and new["script_slice"] == "" and new["location_id"] == api_ep["loc"]
    ids = [s["id"] for s in client.get(f"/api/episodes/{ep}/snippets").json()["active"]]
    order = [ids[-1], *ids[:-1]]
    assert [
        s["id"]
        for s in client.put(f"/api/episodes/{ep}/snippets/order", json={"ids": order}).json()["active"]
    ] == order
    assert client.delete(f"/api/snippets/{new['id']}").json()["archived"] is True
    assert client.post(f"/api/snippets/{new['id']}/restore").json()["archived"] is False
    copy = client.post(f"/api/snippets/{first['id']}/copy").json()
    assert copy["origin"] == "copy"
    # re-split archives everything
    client.post(f"/api/episodes/{ep}/split", json={"level": 0})
    b2 = client.get(f"/api/episodes/{ep}/snippets").json()
    assert {s["id"] for s in b2["archived"]} >= set(ids) | {copy["id"]}


def test_api_split_errors(client, api_ep) -> None:
    ep = api_ep["ep"]
    assert client.post(f"/api/episodes/{ep}/split", json={"level": 0}).status_code == 400
    assert client.post(f"/api/episodes/{ep}/split", json={"level": 1}).status_code == 422
