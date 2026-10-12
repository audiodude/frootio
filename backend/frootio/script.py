"""Script grammar: canonical parse, lint warnings, length estimate, versions.

Canonical grammar (the only form parsed as dialogue):

- Dialogue: `[Speaker]: line`, one line per utterance. `Speaker` resolves
  case-insensitively against the series cast's names and aliases.
- Delivery directions: `*direction*`, only inside a dialogue line.
- `-----` is a hard break for splitting.
- Everything else (prose, `<shot notes>`, headings, `[PHOTO: ...]`, blank
  lines) is prose.

Length estimate: dialogue at 2.5 words/s (directions excluded) plus 1s per
direction, plus 1s per non-blank prose line. Blank lines and hard breaks are 0s.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from . import db

WORDS_PER_SECOND = 2.5
SECONDS_PER_DIRECTION = 1.0
SECONDS_PER_PROSE_LINE = 1.0

LineKind = Literal["dialogue", "prose", "blank", "break"]

_CANONICAL = re.compile(r"^\s*\[([^\[\]:]+)\]\s*:(.*)$")
_DIRECTION = re.compile(r"\*([^*]+)\*")
_HARD_BREAK = re.compile(r"^\s*-{5,}\s*$")
# Dialogue-looking but not canonical (checked after stripping a leading bullet):
#   `[Lemon] [still crying]: ...`   (extra bracket/paren group before the colon)
_BRACKET_EXTRA = re.compile(r"^\[([^\[\]:]+)\]\s*(?:[\[(][^\[\]()]*[\])]\s*)+:\s*\S")
#   `Kiwi: ...`, `Ms. Plum [softly]: ...`, `Plum Prentice (softly): ...`
_BARE_TAG = re.compile(r"^([A-Za-z][\w.'’\- ]{0,40}?)\s*(?:[\[(][^\[\]()]*[\])]\s*)*:\s*\S")
#   `[Kiwi] You know...` (no colon; tag must resolve)
_BRACKET_NO_COLON = re.compile(r"^\[([^\[\]:]+)\]\s*(?:[\[(][^\[\]()]*[\])]\s*)*[^\s:\[(]")
#   `Lemon [screams] Then WHO...` (no colon; name must resolve)
_NAME_DIRECTION = re.compile(r"^([A-Za-z][\w.'’\- ]{0,40}?)\s*[\[(][^\[\]()]*[\])]\s*[^\s:]")
_BULLET = re.compile(r"^\s*(?:[●•·▪◦]|[-–>](?=\s))?\s*")
_TITLE_WORD = re.compile(r"^[A-Z][\w.'’\-]*$")


def norm(tag: str) -> str:
    """Normalized speaker tag / alias: lower(trim)."""
    return " ".join(tag.split()).lower()


def count_words(text: str) -> int:
    return sum(1 for tok in text.split() if any(ch.isalnum() for ch in tok))


@dataclass(frozen=True)
class Speaker:
    character_id: int
    name: str


SpeakerMap = dict[str, Speaker]


@dataclass
class Line:
    number: int  # 1-based
    text: str
    kind: LineKind
    estimate_s: float = 0.0
    speaker_tag: str | None = None
    speaker: Speaker | None = None
    spoken: str = ""  # dialogue text without directions
    directions: list[str] = field(default_factory=list)


@dataclass
class Warning:
    line: int
    kind: Literal["noncanonical", "unresolved"]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "kind": self.kind, "message": self.message}


@dataclass
class Parsed:
    lines: list[Line]
    warnings: list[Warning]

    @property
    def estimate_s(self) -> float:
        return sum(ln.estimate_s for ln in self.lines)


def speaker_map(conn: sqlite3.Connection, series_id: int) -> SpeakerMap:
    """Names and aliases of the series' (non-archived) cast, keyed by normalized tag."""
    out: SpeakerMap = {}
    chars = conn.execute(
        "SELECT c.id, c.name FROM series_cast sc JOIN characters c ON c.id = sc.character_id"
        " WHERE sc.series_id = ? AND c.archived = 0 ORDER BY c.id",
        (series_id,),
    ).fetchall()
    for c in chars:
        out.setdefault(norm(c["name"]), Speaker(c["id"], c["name"]))
    for c in chars:
        for a in conn.execute(
            "SELECT alias FROM character_aliases WHERE character_id = ? ORDER BY id", (c["id"],)
        ):
            out.setdefault(norm(a["alias"]), Speaker(c["id"], c["name"]))
    return out


def episode_speaker_map(conn: sqlite3.Connection, episode_id: int) -> SpeakerMap:
    r = conn.execute("SELECT series_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if r is None:
        raise LookupError(f"episode {episode_id} not found")
    return speaker_map(conn, r["series_id"])


def _looks_like_dialogue(text: str, speakers: SpeakerMap) -> bool:
    stripped = _BULLET.sub("", text, count=1)
    if stripped != text.lstrip() and _CANONICAL.match(stripped):
        return True  # `● [Kiwi]: ...`
    if _BRACKET_EXTRA.match(stripped):
        return True
    for pattern in (_BRACKET_NO_COLON, _NAME_DIRECTION):
        m = pattern.match(stripped)
        if m and norm(m.group(1)) in speakers:
            return True
    m = _BARE_TAG.match(stripped)
    if not m:
        return False
    tag = m.group(1).strip()
    if norm(tag) in speakers:
        return True
    words = tag.split()
    if not 1 <= len(words) <= 3 or not all(_TITLE_WORD.match(w) for w in words):
        return False
    # Multi-word ALL-CAPS ("ON HER SCREEN:", "CLOSE UP:") reads as a shot note, not a cue.
    return not (len(words) > 1 and all(w.isupper() for w in words))


def parse_line(number: int, text: str, speakers: SpeakerMap) -> tuple[Line, Warning | None]:
    if not text.strip():
        return Line(number, text, "blank"), None
    if _HARD_BREAK.match(text):
        return Line(number, text, "break"), None
    m = _CANONICAL.match(text)
    if m:
        tag = m.group(1).strip()
        utterance = m.group(2)
        directions = [d.strip() for d in _DIRECTION.findall(utterance)]
        spoken = " ".join(_DIRECTION.sub(" ", utterance).split())
        speaker = speakers.get(norm(tag))
        line = Line(
            number,
            text,
            "dialogue",
            estimate_s=count_words(spoken) / WORDS_PER_SECOND + SECONDS_PER_DIRECTION * len(directions),
            speaker_tag=tag,
            speaker=speaker,
            spoken=spoken,
            directions=directions,
        )
        warning = None
        if speaker is None:
            warning = Warning(
                number,
                "unresolved",
                f'Speaker "[{tag}]" does not match any cast character name or alias',
            )
        return line, warning
    line = Line(number, text, "prose", estimate_s=SECONDS_PER_PROSE_LINE)
    if _looks_like_dialogue(text, speakers):
        return line, Warning(
            number,
            "noncanonical",
            'Looks like dialogue but is not canonical; parsed as prose. Use "[Speaker]: line"'
            " with *directions* inside the line",
        )
    return line, None


def split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse(text: str, speakers: SpeakerMap) -> Parsed:
    lines: list[Line] = []
    warnings: list[Warning] = []
    for i, raw in enumerate(split_lines(text), start=1):
        line, warning = parse_line(i, raw, speakers)
        lines.append(line)
        if warning is not None:
            warnings.append(warning)
    return Parsed(lines, warnings)


def estimate(text: str, speakers: SpeakerMap | None = None) -> float:
    """Length estimate in seconds (speaker resolution doesn't affect it)."""
    return parse(text, speakers or {}).estimate_s


def caption_lines(slice_text: str) -> list[str]:
    """Dialogue lines of a script slice with `[Speaker]:` and `*directions*` removed."""
    return [ln.spoken for ln in parse(slice_text, {}).lines if ln.kind == "dialogue" and ln.spoken]


def lint_report(parsed: Parsed) -> dict[str, Any]:
    dialogue_s = sum(ln.estimate_s for ln in parsed.lines if ln.kind == "dialogue")
    return {
        "warnings": [w.as_dict() for w in parsed.warnings],
        "estimate_s": round(parsed.estimate_s, 2),
        "dialogue_s": round(dialogue_s, 2),
        "prose_s": round(parsed.estimate_s - dialogue_s, 2),
        "dialogue_lines": sum(1 for ln in parsed.lines if ln.kind == "dialogue"),
    }


# --- versions -------------------------------------------------------------------


def latest_version(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM script_versions WHERE episode_id = ? ORDER BY version DESC LIMIT 1", (episode_id,)
    ).fetchone()


def save_version(
    conn: sqlite3.Connection, episode_id: int, text: str, source: str = "edit"
) -> sqlite3.Row | None:
    """Store `text` as the next version unless it equals the latest. Commits.

    Returns the new row, or None when the text is unchanged.
    """
    latest = latest_version(conn, episode_id)
    if latest is not None and latest["text"] == text:
        return None
    version = (latest["version"] if latest is not None else 0) + 1
    cur = conn.execute(
        "INSERT INTO script_versions (episode_id, version, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (episode_id, version, text, source, db.now()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM script_versions WHERE id = ?", (cur.lastrowid,)).fetchone()
