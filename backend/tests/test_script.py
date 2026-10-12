from __future__ import annotations

import sqlite3

import pytest

from frootio import script

from .conftest import make_episode
from .library_fixtures import add_character, kiwi_and_lemon_cast


@pytest.fixture
def speakers(conn: sqlite3.Connection) -> script.SpeakerMap:
    series_id, _ = make_episode(conn)
    kiwi_and_lemon_cast(conn, series_id)
    return script.speaker_map(conn, series_id)


def kinds(parsed: script.Parsed) -> list[str]:
    return [ln.kind for ln in parsed.lines]


def test_canonical_dialogue_resolves_names_and_aliases_case_insensitively(speakers) -> None:
    p = script.parse(
        "[Kiwi]: *checking watch* Is it chocolate?\n[lemon lee]: Oh my god...\n[  MS. PLUM ]: Hm.",
        speakers,
    )
    assert kinds(p) == ["dialogue"] * 3
    kiwi, lemon, plum = (ln.speaker for ln in p.lines)
    assert (kiwi.name, lemon.name, plum.name) == ("Kiwi Kowalski", "Lemon Lee", "Plum Prentice")
    assert p.lines[0].directions == ["checking watch"]
    assert p.lines[0].spoken == "Is it chocolate?"
    assert p.warnings == []


def test_everything_else_is_prose(speakers) -> None:
    text = (
        "Mango's bakery. Mango slides a tray over.\n"
        "<close up of Lemon, stunned>\n"
        "[PHOTO: Quince holding a trophy]\n"
        "[Bakery back room, same night]\n"
        "*Kiwi sighs*\n"
        "\n"
        "-----"
    )
    p = script.parse(text, speakers)
    assert kinds(p) == ["prose", "prose", "prose", "prose", "prose", "blank", "break"]
    assert p.warnings == []


@pytest.mark.parametrize(
    "line",
    [
        "Kiwi: Hey, you look great.",
        "Ms. Plum [softly]: I'll take that.",
        "[Lemon] [still crying]: Why?",
    ],
)
def test_lint_flags_the_spec_examples(speakers, line: str) -> None:
    p = script.parse(line, speakers)
    assert kinds(p) == ["prose"]
    assert [(w.line, w.kind) for w in p.warnings] == [(1, "noncanonical")]


@pytest.mark.parametrize(
    "line",
    [
        "● [Kiwi] Did you bring the map?",
        "  [Lemon] *laughing* I told you so",
        "Ms. Plum [from doorway] The attic is STRICTLY closed.",
        "Lemon [screams] Then WHO ate it?!",
        "Ms. Plum[directly behind her]: Mr. Quince never allowed pets in this building.",
        "● [Kiwi]: bulleted canonical line",
    ],
)
def test_lint_flags_old_script_cue_forms(speakers, line: str) -> None:
    p = script.parse(line, speakers)
    assert [w.kind for w in p.warnings] == ["noncanonical"]


@pytest.mark.parametrize(
    "line",
    [
        'Kiwi types: "THE MAP IS FAKE."',
        'ON HER SCREEN: Kiwi\'s message ("THE MAP")',
        "0:00-0:04: wide shot of the office",
        "He listens. His expression changes: worry.",
    ],
)
def test_lint_leaves_ordinary_prose_alone(speakers, line: str) -> None:
    assert script.parse(line, speakers).warnings == []


def test_lint_flags_unknown_cue_words_and_unresolved_tags(speakers) -> None:
    p = script.parse("Bob: hi there\n[Bob]: hello\n[Kiwi Kowalski]: fine", speakers)
    assert [(w.line, w.kind) for w in p.warnings] == [(1, "noncanonical"), (2, "unresolved")]
    assert p.lines[1].kind == "dialogue" and p.lines[1].speaker is None


def test_archived_characters_do_not_resolve(conn: sqlite3.Connection) -> None:
    series_id, _ = make_episode(conn)
    add_character(conn, series_id, "Kiwi", archived=True)
    p = script.parse("[Kiwi]: hi", script.speaker_map(conn, series_id))
    assert [w.kind for w in p.warnings] == ["unresolved"]


def test_length_estimate() -> None:
    # 5 words / 2.5 = 2s; 3 words + 1 direction = 1.2 + 1; prose 1s; blank/break 0.
    text = "[A]: one two three four five\n[B]: *sadly* one two three\nShe leaves.\n\n-----\n"
    p = script.parse(text, {})
    assert [round(ln.estimate_s, 2) for ln in p.lines] == [2.0, 2.2, 1.0, 0.0, 0.0, 0.0]
    assert script.estimate(text) == pytest.approx(5.2)


def test_estimate_ignores_punctuation_only_tokens_and_counts_each_direction() -> None:
    assert script.estimate("[A]: *a* well — so ... *b* fine") == pytest.approx(3 / 2.5 + 2)


def test_caption_lines_strip_tags_and_directions() -> None:
    assert script.caption_lines("Prose.\n[A]: *flatly* Hello there.\n[B]: Hi!") == ["Hello there.", "Hi!"]


def test_versions_only_on_change(conn: sqlite3.Connection) -> None:
    _, ep = make_episode(conn)
    v1 = script.save_version(conn, ep, "one")
    assert v1["version"] == 1
    assert script.save_version(conn, ep, "one") is None
    v2 = script.save_version(conn, ep, "two")
    assert v2["version"] == 2 and script.latest_version(conn, ep)["text"] == "two"
