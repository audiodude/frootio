from __future__ import annotations

import sqlite3

import pytest

from frootio import config, prompt, snippets

from .conftest import make_episode
from .library_fixtures import add_character, add_location


@pytest.fixture
def setup(conn: sqlite3.Connection) -> dict:
    series_id, ep = make_episode(conn)
    conn.execute("UPDATE series SET style_preamble = 'Pixar-like fruit drama.' WHERE id = ?", (series_id,))
    kiwi = add_character(conn, series_id, "Kiwi Kowalski", description="a lanky kiwi.", voice=True)
    lemon = add_character(conn, series_id, "Lemon Lee", description="a sweet lemon", voice=True)
    mute = add_character(conn, series_id, "Mango", description="")
    loc = add_location(conn, series_id, "Law office", description="A cramped office.", images=2)
    sid = snippets.insert(
        conn,
        ep,
        snippets.NewSnippet(body="0-5s: Kiwi talks.", cast_ids=[kiwi, lemon, mute], location_id=loc),
    )
    conn.commit()
    return {"series": series_id, "ep": ep, "sid": sid, "kiwi": kiwi, "lemon": lemon, "mute": mute, "loc": loc}


def test_inherited_parts_legend_order_and_joined(conn, setup) -> None:
    b = prompt.build_prompt(conn, setup["sid"])
    assert b.parts["style"].text == "Pixar-like fruit drama."
    assert b.parts["legend"].text == (
        "Image 1 is Kiwi Kowalski: a lanky kiwi. Image 2 is Lemon Lee: a sweet lemon. "
        "Image 3 is Mango. Image 4 is the location Law office. Image 5 is the location Law office. "
        "Audio 1 is Kiwi Kowalski's voice. Audio 2 is Lemon Lee's voice."
    )
    assert b.parts["location"].text == "A cramped office."
    assert b.parts["tail"].text == config.FIXED_TAIL
    assert not any(p.overridden or p.source_changed for p in b.parts.values())
    assert [(r.kind, r.label) for r in b.refs] == [
        ("image", "Kiwi Kowalski"),
        ("image", "Lemon Lee"),
        ("image", "Mango"),
        ("image", "Law office"),
        ("image", "Law office"),
        ("audio", "Kiwi Kowalski's voice"),
        ("audio", "Lemon Lee's voice"),
    ]
    assert b.joined == "\n\n".join([b.parts[k].text for k in ("style", "legend", "location", "body", "tail")])
    assert b.errors == []


def test_empty_parts_are_skipped_in_join(conn, setup) -> None:
    conn.execute("UPDATE series SET style_preamble = '' WHERE id = ?", (setup["series"],))
    conn.commit()
    b = prompt.build_prompt(conn, setup["sid"])
    assert b.joined.startswith("Image 1 is Kiwi Kowalski")


def test_override_freezes_and_source_changed_then_reset(conn, setup) -> None:
    sid = setup["sid"]
    prompt.update_parts(conn, sid, {"style": {"text": "My style.", "overridden": True}})
    conn.commit()
    b = prompt.build_prompt(conn, sid)
    assert b.parts["style"].text == "My style." and b.parts["style"].overridden
    assert not b.parts["style"].source_changed
    # editing an overridden part again keeps the original snapshot
    prompt.update_parts(conn, sid, {"style": {"text": "My style v2."}})
    conn.execute("UPDATE series SET style_preamble = 'Claymation.' WHERE id = ?", (setup["series"],))
    conn.commit()
    b = prompt.build_prompt(conn, sid)
    assert b.parts["style"].text == "My style v2." and b.parts["style"].source_changed
    # source changes back -> badge clears
    conn.execute(
        "UPDATE series SET style_preamble = 'Pixar-like fruit drama.' WHERE id = ?", (setup["series"],)
    )
    conn.commit()
    assert not prompt.build_prompt(conn, sid).parts["style"].source_changed
    prompt.update_parts(conn, sid, {"style": {"overridden": False}})
    conn.commit()
    b = prompt.build_prompt(conn, sid)
    assert b.parts["style"].text == "Pixar-like fruit drama." and not b.parts["style"].overridden


def test_overridden_legend_still_uses_current_refs(conn, setup) -> None:
    sid = setup["sid"]
    prompt.update_parts(conn, sid, {"legend": {"text": "Image 1 is Kiwi."}})
    snippets.set_cast(conn, sid, [setup["lemon"]])
    conn.commit()
    b = prompt.build_prompt(conn, sid)
    assert b.parts["legend"].text == "Image 1 is Kiwi." and b.parts["legend"].source_changed
    assert [r.label for r in b.refs if r.kind == "image"][0] == "Lemon Lee"


def test_location_description_change_flags_overridden_location(conn, setup) -> None:
    sid = setup["sid"]
    prompt.update_parts(conn, sid, {"location": {"text": "A messy office."}, "body": {"text": "new body"}})
    conn.execute("UPDATE locations SET description = 'A huge office.' WHERE id = ?", (setup["loc"],))
    conn.commit()
    b = prompt.build_prompt(conn, sid)
    assert b.parts["location"].source_changed and b.parts["body"].text == "new body"


def test_load_take_style_all_overridden(conn, setup) -> None:
    sid = setup["sid"]
    parts = {k: {"text": f"{k} text", "overridden": True} for k in prompt.PARTS}
    prompt.update_parts(conn, sid, parts)
    conn.commit()
    b = prompt.build_prompt(conn, sid)
    assert b.joined == "\n\n".join(f"{k} text" for k in prompt.PARTS)
    assert all(b.parts[k].overridden for k in prompt.DERIVED)


def test_update_parts_validation(conn, setup) -> None:
    with pytest.raises(ValueError, match="unknown"):
        prompt.update_parts(conn, setup["sid"], {"nope": {"text": "x"}})
    with pytest.raises(ValueError, match="required"):
        prompt.update_parts(conn, setup["sid"], {"legend": {"overridden": True}})


def test_ref_limits(conn, setup) -> None:
    sid = setup["sid"]
    cast = [setup["kiwi"], setup["lemon"]]
    for i in range(6):
        cast.append(add_character(conn, setup["series"], f"Extra {i}", voice=(i < 2)))
    snippets.set_cast(conn, sid, cast)  # 8 char images + 2 loc = 10 images; 4 audio; 14 files
    conn.commit()
    errors = prompt.build_prompt(conn, sid).errors
    assert any("images: 10" in e for e in errors)
    assert any("audio clips: 4" in e for e in errors)
    assert any("files: 14" in e for e in errors)
    snippets.set_cast(conn, sid, cast[:3] + cast[4:])  # 7+2 = 9 images, 3 audio, 12 files: exactly at limits
    conn.commit()
    assert prompt.build_prompt(conn, sid).errors == []


def test_prompt_length_limit(conn, setup) -> None:
    sid = setup["sid"]
    b = prompt.build_prompt(conn, sid)
    room = config.PROMPT_MAX_CHARS - (len(b.joined) - len(b.parts["body"].text))
    prompt.update_parts(conn, sid, {"body": {"text": "x" * room}})
    assert prompt.build_prompt(conn, sid).errors == []
    prompt.update_parts(conn, sid, {"body": {"text": "x" * (room + 1)}})
    assert any("50000" in e for e in prompt.build_prompt(conn, sid).errors)


def test_character_without_image_has_no_image_entry(conn, setup) -> None:
    noimg = add_character(conn, setup["series"], "Ghost", image=False, voice=True)
    snippets.set_cast(conn, setup["sid"], [noimg, setup["kiwi"]])
    conn.commit()
    b = prompt.build_prompt(conn, setup["sid"])
    assert b.parts["legend"].text.startswith("Image 1 is Kiwi Kowalski")
    assert "Audio 1 is Ghost's voice. Audio 2 is Kiwi Kowalski's voice." in b.parts["legend"].text
