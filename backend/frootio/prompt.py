"""Five-part snippet prompt: style, legend, location (derived), body, tail.

Derived parts are either inherited (rebuilt from their source on every build)
or overridden (frozen user text). When a part is overridden, the inherited text
at that moment is kept as a snapshot; `source_changed` is true when the current
inherited text differs from it. The ref list is always built from the current
cast and location, even when the legend is overridden.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

from . import config, db

DERIVED = ("style", "legend", "location")
PARTS = ("style", "legend", "location", "body", "tail")
JOIN = "\n\n"


@dataclass
class PromptPart:
    text: str
    overridden: bool
    source_changed: bool


@dataclass
class RefItem:
    kind: Literal["image", "audio"]
    path: str  # relpath under media root
    sha: str
    label: str


@dataclass
class BuiltPrompt:
    parts: dict[str, PromptPart]  # keys: style, legend, location, body, tail
    joined: str
    refs: list[RefItem]
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts": {k: asdict(v) for k, v in self.parts.items()},
            "joined": self.joined,
            "char_count": len(self.joined),
            "max_chars": config.PROMPT_MAX_CHARS,
            "refs": [asdict(r) for r in self.refs],
            "errors": self.errors,
        }


def _sentence(text: str) -> str:
    return " ".join(text.split()).rstrip(".")


def _cast(conn: sqlite3.Connection, snippet_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.id, c.name, c.description, ci.path AS image_path, ci.sha256 AS image_sha,"
        " cv.path AS voice_path, cv.sha256 AS voice_sha"
        " FROM snippet_cast sc JOIN characters c ON c.id = sc.character_id"
        " LEFT JOIN character_images ci ON ci.id = c.main_image_id"
        " LEFT JOIN character_voices cv ON cv.id = c.voice_id"
        " WHERE sc.snippet_id = ? ORDER BY sc.position, c.id",
        (snippet_id,),
    ).fetchall()


def _location_images(conn: sqlite3.Connection, location_id: int | None) -> list[sqlite3.Row]:
    if location_id is None:
        return []
    return conn.execute(
        "SELECT path, sha256 FROM location_images WHERE location_id = ? AND ref_order IS NOT NULL"
        " AND hidden = 0 ORDER BY ref_order, id",
        (location_id,),
    ).fetchall()


def refs_and_legend(conn: sqlite3.Connection, snippet: sqlite3.Row) -> tuple[list[RefItem], str]:
    """Ref list (character main images, location images, voices) and the inherited legend."""
    cast = _cast(conn, snippet["id"])
    loc = (
        conn.execute("SELECT id, name FROM locations WHERE id = ?", (snippet["location_id"],)).fetchone()
        if snippet["location_id"] is not None
        else None
    )
    refs: list[RefItem] = []
    sentences: list[str] = []
    n_image = 0
    for c in cast:
        if c["image_path"] is None:
            continue
        n_image += 1
        refs.append(RefItem("image", c["image_path"], c["image_sha"], c["name"]))
        desc = _sentence(c["description"])
        sentences.append(
            f"Image {n_image} is {c['name']}: {desc}." if desc else f"Image {n_image} is {c['name']}."
        )
    if loc is not None:
        for img in _location_images(conn, loc["id"]):
            n_image += 1
            refs.append(RefItem("image", img["path"], img["sha256"], loc["name"]))
            sentences.append(f"Image {n_image} is the location {loc['name']}.")
    n_audio = 0
    for c in cast:
        if c["voice_path"] is None:
            continue
        n_audio += 1
        refs.append(RefItem("audio", c["voice_path"], c["voice_sha"], f"{c['name']}'s voice"))
        sentences.append(f"Audio {n_audio} is {c['name']}'s voice.")
    return refs, " ".join(sentences)


def inherited(conn: sqlite3.Connection, snippet: sqlite3.Row) -> dict[str, str]:
    """Current inherited text of the three derived parts."""
    style = conn.execute(
        "SELECT s.style_preamble FROM episodes e JOIN series s ON s.id = e.series_id WHERE e.id = ?",
        (snippet["episode_id"],),
    ).fetchone()
    loc = (
        conn.execute("SELECT description FROM locations WHERE id = ?", (snippet["location_id"],)).fetchone()
        if snippet["location_id"] is not None
        else None
    )
    _, legend = refs_and_legend(conn, snippet)
    return {
        "style": style["style_preamble"] if style else "",
        "legend": legend,
        "location": loc["description"] if loc else "",
    }


def ref_errors(refs: list[RefItem]) -> list[str]:
    images = sum(1 for r in refs if r.kind == "image")
    audio = sum(1 for r in refs if r.kind == "audio")
    errors = []
    if images > config.MAX_REF_IMAGES:
        errors.append(f"Too many reference images: {images} (fal allows {config.MAX_REF_IMAGES})")
    if audio > config.MAX_REF_AUDIO:
        errors.append(f"Too many reference audio clips: {audio} (fal allows {config.MAX_REF_AUDIO})")
    if len(refs) > config.MAX_REF_FILES:
        errors.append(f"Too many reference files: {len(refs)} (fal allows {config.MAX_REF_FILES})")
    return errors


def join_parts(texts: dict[str, str]) -> str:
    return JOIN.join(t.strip() for t in (texts[k] for k in PARTS) if t.strip())


def build_from_row(conn: sqlite3.Connection, snippet: sqlite3.Row) -> BuiltPrompt:
    refs, legend = refs_and_legend(conn, snippet)
    current = inherited(conn, snippet)
    current["legend"] = legend
    parts: dict[str, PromptPart] = {}
    for name in DERIVED:
        if snippet[f"{name}_overridden"]:
            snap = snippet[f"{name}_source_snapshot"]
            parts[name] = PromptPart(
                snippet[f"{name}_override"], True, snap is not None and snap != current[name]
            )
        else:
            parts[name] = PromptPart(current[name], False, False)
    parts["body"] = PromptPart(snippet["body"], False, False)
    parts["tail"] = PromptPart(snippet["tail"], False, False)
    joined = join_parts({k: v.text for k, v in parts.items()})
    errors = ref_errors(refs)
    if len(joined) > config.PROMPT_MAX_CHARS:
        errors.append(f"Prompt is {len(joined)} characters (fal allows {config.PROMPT_MAX_CHARS})")
    return BuiltPrompt(parts, joined, refs, errors)


def build_prompt(conn: sqlite3.Connection, snippet_id: int) -> BuiltPrompt:
    snippet = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if snippet is None:
        raise LookupError(f"snippet {snippet_id} not found")
    return build_from_row(conn, snippet)


def update_parts(conn: sqlite3.Connection, snippet_id: int, parts: dict[str, dict[str, Any]]) -> None:
    """Partial update of prompt parts (does not commit).

    Derived parts: `overridden` (default true) with `text` freezes the text; the
    inherited text is snapshotted when a part goes from inherited to overridden.
    `overridden: false` resets the part to inherited. Body and tail take `text`.
    """
    snippet = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if snippet is None:
        raise LookupError(f"snippet {snippet_id} not found")
    unknown = set(parts) - set(PARTS)
    if unknown:
        raise ValueError(f"unknown prompt parts: {sorted(unknown)}")
    current = inherited(conn, snippet) if any(k in DERIVED for k in parts) else {}
    sets: list[str] = []
    params: list[Any] = []
    for name, spec in parts.items():
        text = spec.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError(f"{name}.text must be a string")
        if name in ("body", "tail"):
            if text is None:
                raise ValueError(f"{name}.text is required")
            sets.append(f"{name} = ?")
            params.append(text)
            continue
        if spec.get("overridden", True):
            if text is None:
                raise ValueError(f"{name}.text is required to override")
            sets += [f"{name}_overridden = 1", f"{name}_override = ?"]
            params.append(text)
            if not snippet[f"{name}_overridden"]:
                sets.append(f"{name}_source_snapshot = ?")
                params.append(current[name])
        else:
            sets += [f"{name}_overridden = 0", f"{name}_override = ''", f"{name}_source_snapshot = NULL"]
    if not sets:
        return
    sets.append("updated_at = ?")
    params += [db.now(), snippet_id]
    conn.execute(f"UPDATE snippets SET {', '.join(sets)} WHERE id = ?", params)
