from __future__ import annotations

import re

import numpy as np
import pytest

from frootio import captions
from frootio.captions import CaptionWord, TakeCaptions


def test_caption_lines_keep_only_canonical_dialogue() -> None:
    slice_ = """Mango's bakery. Lemon and Kiwi at the counter.
[Mango]: Lemon, your cake is finally ready.
[Kiwi]: *checking watch* Is it chocolate?
<Kiwi exits>
Kiwi: not canonical, prose
[Lemon] [still crying]: also prose
[PHOTO: a framed picture]
-----
[Mango]: *flatly* _Lemon_ ordered the lemon one. *beat* Obviously.
[Lemon]: *gasps*
[Ms. Plum]:    Tea?   """
    assert captions.caption_lines(slice_) == [
        "Lemon, your cake is finally ready.",
        "Is it chocolate?",
        "Lemon ordered the lemon one. Obviously.",
        "Tea?",
    ]
    assert captions.caption_lines("just prose\n\n[Kiwi]: *only a direction*") == []


def test_line_words_attach_punctuation_tokens() -> None:
    assert captions.line_words("Mr. Quince — he wanted") == ["Mr.", "Quince —", "he", "wanted"]
    assert captions.line_words("... well") == ["... well"]
    assert captions.line_words("—") == []


def _w(text: str, start: float, end: float, line: int = 0) -> CaptionWord:
    return CaptionWord(text, start, end, line)


def test_group_words_one_to_three_words() -> None:
    words = [_w(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate("a b c d e f g".split())]
    groups = captions.group_words(words)
    assert [len(g) for g in groups] == [3, 3, 1]
    # punctuation, a long pause, a new line and long words all break groups
    words = [
        _w("Hi,", 0.0, 0.2),
        _w("you", 0.25, 0.4),
        _w("there", 1.5, 1.7),
        _w("now", 1.75, 1.9, line=1),
        _w("extraordinary", 2.0, 2.4, line=1),
        _w("ok", 2.45, 2.5, line=1),
    ]
    groups = captions.group_words(words)
    assert [[w.text for w in g] for g in groups] == [
        ["Hi,"],
        ["you"],
        ["there"],
        ["now"],
        ["extraordinary"],
        ["ok"],
    ]
    assert all(1 <= len(g) <= 3 for g in groups)


def _events(ass: str) -> list[tuple[float, float, str]]:
    out = []
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        _, start, end, _style, _name, _ml, _mr, _mv, _eff, text = line.split(",", 9)

        def t(s: str) -> float:
            h, m, sec = s.split(":")
            return int(h) * 3600 + int(m) * 60 + float(sec)

        out.append((t(start), t(end), text))
    return out


def _plain(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


def test_ass_offsets_across_trims_and_drops_trimmed_words() -> None:
    take1 = TakeCaptions(
        words=[
            {"word": "early", "start": 0.1, "end": 0.4, "line": 0},  # before trim-in: dropped
            {"word": "hello", "start": 1.0, "end": 1.4, "line": 0},
            {"word": "world", "start": 1.5, "end": 2.5, "line": 0},
            {"word": "late", "start": 4.6, "end": 4.9, "line": 0},  # after trim-out: dropped
        ],
        trim_in=0.5,
        trim_out=4.5,
        offset=0.0,
    )
    take2 = TakeCaptions(
        words=[
            {"word": "again", "start": 0.3, "end": 0.6, "line": 0},
            {"word": "edge", "start": 2.9, "end": 3.4, "line": 0},
        ],  # straddles trim-out: clipped
        trim_in=0.2,
        trim_out=3.0,
        offset=4.0,  # take1 trimmed duration = 4.0
    )
    events = _events(captions.build_ass([take1, take2]))
    texts = [_plain(e[2]) for e in events]
    assert "EARLY" not in " ".join(texts) and "LATE" not in " ".join(texts)
    starts = {(_plain(text), round(s, 2)) for s, _, text in events}
    # timeline = offset - trim_in + word time; one event per active word within a group
    assert ("HELLO WORLD", 0.5) in starts and ("HELLO WORLD", 1.0) in starts
    assert ("AGAIN", 4.1) in starts
    assert ("EDGE", 6.7) in starts
    # nothing extends past the end of its take on the timeline
    for _s, e, text in events:
        if "EDGE" in text:
            assert e <= 6.8 + 1e-6
        if "WORLD" in _plain(text):
            assert e <= 4.0 + 1e-6
    # the active word is the yellow, popping one; each event highlights exactly one word
    for _, _, text in events:
        assert text.count("\\1c&H0000E6FF&") == 1
        assert "\\t(0," in text and "\\fscx118" in text


def test_ass_header_matches_output_and_style() -> None:
    ass = captions.build_ass([])
    assert "PlayResX: 768" in ass and "PlayResY: 1344" in ass
    style = next(line for line in ass.splitlines() if line.startswith("Style: Shorts"))
    fields = style.split(",")
    assert fields[7] == "-1"  # bold
    assert int(fields[16]) >= 4  # outline
    assert fields[18] == "2"  # bottom centre
    assert int(fields[21]) >= 300  # lifted into the lower-middle safe area


def test_ass_events_hand_over_without_overlap() -> None:
    take = TakeCaptions(
        words=[
            {"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25, "line": 0}
            for i, w in enumerate("one two three four".split())
        ],
        trim_in=0.0,
        trim_out=5.0,
        offset=0.0,
    )
    events = captions.caption_events([take])
    assert len(events) == 4
    for (s1, e1, _), (s2, _, _) in zip(events, events[1:], strict=False):
        assert e1 <= s2 + 1e-9 and s1 < e1


# --- fallback spreading -----------------------------------------------------------------


def test_merge_keeps_good_alignment() -> None:
    lines = [["hi", "there"]]
    words, method = captions.merge_alignment(lines, [[(0.5, 0.8), (0.8, 1.2)]], [], 3.0)
    assert method == "aligned"
    assert [(w["start"], w["end"]) for w in words] == [(0.5, 0.8), (0.8, 1.2)]


def test_fallback_spreads_failed_alignment_over_speech_regions() -> None:
    lines = [["a", "b"], ["c", "d"]]
    regions = [(1.0, 2.0), (3.0, 4.0)]
    words, method = captions.merge_alignment(lines, [None, None], regions, 5.0)
    assert method == "fallback"
    spans = [(w["start"], w["end"]) for w in words]
    assert spans == [(1.0, 1.5), (1.5, 2.0), (3.0, 3.5), (3.5, 4.0)]
    assert [w["line"] for w in words] == [0, 0, 1, 1]


def test_fallback_replaces_near_zero_line_within_its_gap() -> None:
    lines = [["first", "line"], ["adlib", "garbled", "words"], ["last"]]
    aligned = [
        [(0.2, 0.5), (0.5, 0.9)],
        [(5.0, 5.0), (5.0, 5.0), (5.0, 5.01)],  # near-zero total duration
        [(4.0, 4.5)],
    ]
    regions = [(0.2, 0.9), (1.5, 3.0), (4.0, 4.5)]
    words, method = captions.merge_alignment(lines, aligned, regions, 6.0)
    assert method == "mixed"
    mid = [w for w in words if w["line"] == 1]
    # spread over the speech between line 0's end (0.9) and line 2's start (4.0) -> region 1.5..3.0
    assert [(w["start"], w["end"]) for w in mid] == [(1.5, 2.0), (2.0, 2.5), (2.5, 3.0)]
    assert [w["word"] for w in words] == ["first", "line", "adlib", "garbled", "words", "last"]


def test_fallback_without_speech_uses_the_window() -> None:
    words, _ = captions.merge_alignment([["x", "y"]], [None], [], 2.0)
    assert [(w["start"], w["end"]) for w in words] == [(0.0, 1.0), (1.0, 2.0)]


def test_partially_unmatched_line_falls_back() -> None:
    words, method = captions.merge_alignment([["a", "b"]], [[(0.1, 0.4), None]], [(1.0, 2.0)], 3.0)
    assert method == "fallback"
    assert words[0]["start"] == 1.0


def test_squashed_first_word_stretches_to_speech_onset() -> None:
    # stable-ts pinned "Wait" to 0.0-0.1s although speech starts at 0.41
    lines = [["Wait...", "did", "I"], ["Kiwi,", "you"]]
    aligned = [[(0.0, 0.1), (0.94, 1.5), (1.5, 1.7)], [(2.0, 2.02), (2.6, 2.9)]]
    words, method = captions.merge_alignment(lines, aligned, [(0.41, 1.01), (1.2, 1.8), (2.3, 3.0)], 4.0)
    assert method == "aligned"
    spans = [(w["start"], w["end"]) for w in words]
    # line 2's squashed first word may not reach back before line 1's end (1.7)
    assert spans == [(0.41, 0.94), (0.94, 1.5), (1.5, 1.7), (2.3, 2.6), (2.6, 2.9)]


def test_map_result_words_handles_retokenisation() -> None:
    lines = [["Mr.", "Quince —", "he"], ["Is", "it"]]
    result = [
        (" Mr", 0.0, 0.2),
        (".", 0.2, 0.25),
        (" Quin", 0.3, 0.5),
        ("ce", 0.5, 0.7),
        (" —", 0.7, 0.7),
        (" he", 0.8, 0.9),
        (" I", 1.0, 1.1),
        ("s", 1.1, 1.2),
        (" it", 1.3, 1.4),
    ]
    mapped = captions.map_result_words(lines, result)
    # punctuation-only result tokens carry no characters to match, so "Mr." takes " Mr"'s span
    assert mapped == [[(0.0, 0.2), (0.3, 0.7), (0.8, 0.9)], [(1.0, 1.2), (1.3, 1.4)]]
    missing = captions.map_result_words([["zebra"]], [(" hello", 0.0, 1.0)])
    assert missing == [[None]]


def test_speech_regions_energy_vad() -> None:
    sr = captions.SAMPLE_RATE
    t = np.arange(sr * 3) / sr
    audio = np.zeros_like(t, dtype=np.float32)
    burst = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    audio[int(0.5 * sr) : int(1.0 * sr)] = burst[int(0.5 * sr) : int(1.0 * sr)]
    audio[int(2.0 * sr) : int(2.6 * sr)] = burst[int(2.0 * sr) : int(2.6 * sr)]
    audio += np.random.default_rng(0).normal(0, 1e-4, audio.shape).astype(np.float32)
    regions = captions.speech_regions(audio)
    assert len(regions) == 2
    (a0, a1), (b0, b1) = regions
    assert a0 == pytest.approx(0.45, abs=0.05) and a1 == pytest.approx(1.05, abs=0.05)
    assert b0 == pytest.approx(1.95, abs=0.05) and b1 == pytest.approx(2.65, abs=0.05)
    assert captions.speech_regions(np.zeros(sr, dtype=np.float32)) == []


def test_align_words_falls_back_when_whisper_fails(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    sr = captions.SAMPLE_RATE
    audio = np.zeros(sr * 2, dtype=np.float32)
    audio[sr // 2 : sr] = 0.3 * np.sin(2 * np.pi * 220 * np.arange(sr // 2) / sr)
    monkeypatch.setattr(captions, "load_audio", lambda path, sr=sr: audio)

    def boom(*a, **k):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(captions, "_whisper_align", boom)
    out = captions.align_words("unused.wav", ["one two"])
    assert out["method"] == "fallback" and "model exploded" in out["error"]
    assert [w["word"] for w in out["words"]] == ["one", "two"]
    assert 0.4 <= out["words"][0]["start"] < out["words"][1]["end"] <= 1.1
