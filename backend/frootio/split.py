"""Splitting a script into snippets.

Level 0 (no LLM): greedy packing of consecutive lines into snippets of up to
15s (new snippet when the next line would exceed 15s or add a 4th speaker;
`-----` is a hard break; a single line over 15s gets its own 15s snippet with a
warning). Applied immediately: current snippets are archived.

Level 2 (OpenRouter): a `split_l2` job asks the model for strict JSON, validates
it (one repair retry) and stores a pending preview in `split_proposals`;
accepting it archives the current snippets.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import config, db, jobs, openrouter, script, snippets, spend
from .script import Line, Parsed, SpeakerMap

EPS = 1e-9
MAX_TOKENS = 16000
SETTINGS_MODEL_KEY = "openrouter_model"


# --- level 0 ------------------------------------------------------------------


@dataclass
class Chunk:
    lines: list[Line] = field(default_factory=list)
    estimate_s: float = 0.0
    speakers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _speaker_key(line: Line) -> str | None:
    if line.kind != "dialogue":
        return None
    if line.speaker is not None:
        return f"c{line.speaker.character_id}"
    return f"t:{script.norm(line.speaker_tag or '')}"


def chunk_lines(parsed: Parsed) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur = Chunk()

    def flush() -> None:
        nonlocal cur
        while cur.lines and cur.lines[-1].kind == "blank":
            cur.lines.pop()
        if cur.lines:
            chunks.append(cur)
        cur = Chunk()

    limit = config.SNIPPET_MAX_SECONDS
    for line in parsed.lines:
        if line.kind == "break":
            flush()
            continue
        if line.kind == "blank":
            if cur.lines:
                cur.lines.append(line)
            continue
        key = _speaker_key(line)
        if line.estimate_s > limit + EPS:
            flush()
            cur = Chunk(
                [line],
                line.estimate_s,
                [key] if key else [],
                [
                    f"Line {line.number} is estimated at {line.estimate_s:.1f}s, over the {limit}s limit;"
                    f" it has its own snippet at {limit}s. Shorten it in the script."
                ],
            )
            flush()
            continue
        new_speaker = key is not None and key not in cur.speakers
        if cur.lines and (
            cur.estimate_s + line.estimate_s > limit + EPS
            or (new_speaker and len(cur.speakers) >= config.MAX_SPEAKERS_PER_SNIPPET)
        ):
            flush()
            new_speaker = key is not None
        cur.lines.append(line)
        cur.estimate_s += line.estimate_s
        if new_speaker and key:
            cur.speakers.append(key)
    flush()
    return chunks


def level0(parsed: Parsed, default_location_id: int | None) -> list[snippets.NewSnippet]:
    out = []
    for ch in chunk_lines(parsed):
        text = "\n".join(ln.text for ln in ch.lines)
        cast: list[int] = []
        for ln in ch.lines:
            if ln.speaker is not None and ln.speaker.character_id not in cast:
                cast.append(ln.speaker.character_id)
        out.append(
            snippets.NewSnippet(
                body=text,
                script_slice=text,
                cast_ids=cast,
                location_id=default_location_id,
                target_duration_s=snippets.clamp_duration(ch.estimate_s),
                warnings=ch.warnings,
            )
        )
    return out


# --- shared -------------------------------------------------------------------


class SplitError(ValueError):
    pass


def _episode(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise LookupError(f"episode {episode_id} not found")
    return ep


def _script_version(conn: sqlite3.Connection, episode_id: int, version: int | None) -> sqlite3.Row:
    if version is None:
        row = script.latest_version(conn, episode_id)
    else:
        row = conn.execute(
            "SELECT * FROM script_versions WHERE episode_id = ? AND version = ?", (episode_id, version)
        ).fetchone()
    if row is None:
        raise SplitError(
            "no saved script to split" if version is None else f"script version {version} not found"
        )
    if not row["text"].strip():
        raise SplitError("the script is empty")
    return row


def proposal_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["result"] = db.loads(row["result"], [])
    out["warnings"] = db.loads(row["warnings"], [])
    job_status = None
    if row["job_id"] is not None:
        j = conn.execute("SELECT status FROM jobs WHERE id = ?", (row["job_id"],)).fetchone()
        job_status = j["status"] if j else None
    out["job_status"] = job_status
    out["running"] = row["status"] == "pending" and job_status in ("queued", "running")
    sv = conn.execute(
        "SELECT version FROM script_versions WHERE id = ?", (row["script_version_id"],)
    ).fetchone()
    out["script_version"] = sv["version"] if sv else None
    return out


def run_level0(conn: sqlite3.Connection, episode_id: int, version: int | None = None) -> dict[str, Any]:
    """Split the saved script (latest or given version) and replace the board. Commits."""
    ep = _episode(conn, episode_id)
    sv = _script_version(conn, episode_id, version)
    parsed = script.parse(sv["text"], script.speaker_map(conn, ep["series_id"]))
    new = level0(parsed, ep["default_location_id"])
    total = sum(n.target_duration_s for n in new)
    length = snippets.episode_length(total)
    warnings = [w for n in new for w in n.warnings] + (
        [length["length_warning"]] if length["length_warning"] else []
    )
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO split_proposals (episode_id, script_version_id, level, result, estimated_total_s,"
        " warnings, status, created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?, 'accepted', ?, ?)",
        (
            episode_id,
            sv["id"],
            db.dumps([_new_dict(conn, n) for n in new]),
            total,
            db.dumps(warnings),
            ts,
            ts,
        ),
    )
    ids = snippets.replace_board(conn, episode_id, new, origin="split0", split_proposal_id=cur.lastrowid)
    conn.commit()
    snippets.publish(episode_id, split_proposal_id=cur.lastrowid)
    return {
        "proposal_id": cur.lastrowid,
        "snippet_ids": ids,
        "warnings": warnings,
        "script_estimate_s": round(parsed.estimate_s, 2),
        **length,
    }


def _new_dict(conn: sqlite3.Connection, n: snippets.NewSnippet) -> dict[str, Any]:
    names = (
        {
            r["id"]: r["name"]
            for r in conn.execute(
                f"SELECT id, name FROM characters WHERE id IN ({','.join('?' * len(n.cast_ids))})", n.cast_ids
            )
        }
        if n.cast_ids
        else {}
    )
    loc = (
        conn.execute("SELECT id, name FROM locations WHERE id = ?", (n.location_id,)).fetchone()
        if n.location_id is not None
        else None
    )
    return {
        "script_slice": n.script_slice,
        "body": n.body,
        "cast": [{"id": cid, "name": names.get(cid, "?")} for cid in n.cast_ids],
        "location": {"id": loc["id"], "name": loc["name"]} if loc else None,
        "target_duration_s": n.target_duration_s,
        "estimate_s": round(script.estimate(n.script_slice), 2),
        "warnings": n.warnings,
    }


# --- level 2 ------------------------------------------------------------------


def get_model(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_MODEL_KEY,)).fetchone()
    return db.loads(r["value"]) if r else config.settings().openrouter_model


def remember_model(conn: sqlite3.Connection, model: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTINGS_MODEL_KEY, db.dumps(model), db.now()),
    )


@dataclass
class Context:
    """What the model is told about, and what its output is validated against."""

    speakers: SpeakerMap
    cast: list[dict[str, Any]]  # id, name, aliases, description, has_voice
    locations: list[dict[str, Any]]  # id, name, description
    default_location_id: int | None


def context(conn: sqlite3.Connection, episode_id: int) -> Context:
    ep = _episode(conn, episode_id)
    cast = []
    for c in conn.execute(
        "SELECT c.id, c.name, c.description, c.voice_id FROM series_cast sc"
        " JOIN characters c ON c.id = sc.character_id"
        " WHERE sc.series_id = ? AND c.archived = 0 ORDER BY c.id",
        (ep["series_id"],),
    ):
        aliases = [
            a[0]
            for a in conn.execute(
                "SELECT alias FROM character_aliases WHERE character_id = ? ORDER BY id", (c["id"],)
            )
        ]
        cast.append(
            {
                "id": c["id"],
                "name": c["name"],
                "aliases": aliases,
                "description": c["description"],
                "has_voice": c["voice_id"] is not None,
            }
        )
    locations = db.rows(
        conn.execute(
            "SELECT l.id, l.name, l.description FROM series_locations sl"
            " JOIN locations l ON l.id = sl.location_id"
            " WHERE sl.series_id = ? AND l.archived = 0 ORDER BY l.name",
            (ep["series_id"],),
        ).fetchall()
    )
    default_id = ep["default_location_id"]
    if default_id is not None and all(loc["id"] != default_id for loc in locations):
        r = conn.execute(
            "SELECT id, name, description FROM locations WHERE id = ?", (ep["default_location_id"],)
        ).fetchone()
        if r:
            locations.append(dict(r))
    return Context(script.speaker_map(conn, ep["series_id"]), cast, locations, ep["default_location_id"])


SYSTEM_PROMPT = """You split a short vertical-video drama script into snippets for an AI video model \
(one model call per snippet). Each snippet is a multi-shot prompt of at most 15 seconds.

Rules:
- Max-length packing: fill each snippet toward 15 seconds; use as few snippets as possible.
- At most 15 seconds and at most 3 speaking characters per snippet.
- The whole episode targets 30-45 seconds, but keep every line of dialogue: never cut, merge away \
or paraphrase dialogue to hit the target. Keep the script's order.
- Speaking pace is about 2.5 words per second; a delivery direction adds about 1 second; \
an action beat adds about 1 second.
- script_slice: the script lines this snippet covers, in canonical grammar: each dialogue line as \
`[Speaker]: line` using the character's exact name, delivery directions as `*direction*` inside the \
dialogue line, anything else as plain prose lines. Keep the dialogue wording exactly as written \
(only fix the speaker tag and direction format). Slices together must cover the whole script in order.
- cast: names of every character who appears in the snippet (speaking or not), exact names from the cast list.
- location: the exact name of one location from the locations list, or null if none fits or none are given.
- duration_s: integer seconds, 5 to 15, the snippet's length.
- body: the video prompt for the snippet as a timestamped multi-shot sequence, one shot per line, e.g. \
`0-4s: Medium shot ... Kiwi Kowalski says "..."`. Timestamps run from 0 to duration_s. Refer to \
characters by their exact names. Describe framing, action and delivery. Include every dialogue line \
of the slice verbatim in double quotes, attributed to its speaker. No music cues.

Output ONLY a JSON object, no prose and no code fences, of this shape:
{"snippets": [{"script_slice": "...", "cast": ["Name", ...], "location": "Name" or null, \
"duration_s": 12, "body": "0-4s: ...\\n4-9s: ..."}]}"""


def _user_message(ctx: Context, text: str) -> str:
    default_loc = next((loc["name"] for loc in ctx.locations if loc["id"] == ctx.default_location_id), None)
    payload = {
        "cast": [{k: c[k] for k in ("name", "aliases", "description", "has_voice")} for c in ctx.cast],
        "locations": [{k: loc[k] for k in ("name", "description")} for loc in ctx.locations],
        "default_location": default_loc,
    }
    return (
        "Cast and locations (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + "\n\nScript (speaker tags may be aliases or non-canonical; map them to cast names):\n"
        + "<script>\n"
        + text
        + "\n</script>"
    )


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def extract_json(content: str) -> Any:
    text = content.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _norm_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")))


def validate_l2(
    data: Any, ctx: Context, script_text: str
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(proposed snippets, errors, warnings). Any error makes the output invalid."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("snippets"), list) or not data["snippets"]:
        return [], ['output must be a JSON object with a non-empty "snippets" array'], []
    by_name = {script.norm(c["name"]): c for c in ctx.cast}
    by_tag = {
        tag: by_name[script.norm(sp.name)]
        for tag, sp in ctx.speakers.items()
        if script.norm(sp.name) in by_name
    }
    locs = {script.norm(loc["name"]): loc for loc in ctx.locations}
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data["snippets"], start=1):
        where = f"snippet {i}"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        slice_text, body = item.get("script_slice"), item.get("body")
        cast, loc, dur = item.get("cast", []), item.get("location"), item.get("duration_s")
        if not isinstance(slice_text, str) or not slice_text.strip():
            errors.append(f"{where}: script_slice must be a non-empty string")
            slice_text = ""
        if not isinstance(body, str) or not body.strip():
            errors.append(f"{where}: body must be a non-empty string")
            body = ""
        if isinstance(dur, float) and dur.is_integer():
            dur = int(dur)
        if (
            not isinstance(dur, int)
            or isinstance(dur, bool)
            or not (config.SNIPPET_MIN_SECONDS <= dur <= config.SNIPPET_MAX_SECONDS)
        ):
            errors.append(f"{where}: duration_s must be an integer from 5 to 15, got {dur!r}")
            dur = config.SNIPPET_MAX_SECONDS
        cast_out: list[dict[str, Any]] = []
        if not isinstance(cast, list) or not all(isinstance(n, str) for n in cast):
            errors.append(f"{where}: cast must be a list of character names")
            cast = []
        for name in cast:
            c = by_name.get(script.norm(name)) or by_tag.get(script.norm(name))
            if c is None:
                errors.append(f"{where}: unknown cast member {name!r}; use exact names from the cast list")
            elif all(x["id"] != c["id"] for x in cast_out):
                cast_out.append({"id": c["id"], "name": c["name"]})
        loc_out = None
        if loc is not None:
            if not isinstance(loc, str):
                errors.append(f"{where}: location must be a location name or null")
            elif loc.strip():
                hit = locs.get(script.norm(loc))
                if hit is None:
                    errors.append(
                        f"{where}: unknown location {loc!r}; use an exact name from the list or null"
                    )
                else:
                    loc_out = {"id": hit["id"], "name": hit["name"]}
        if loc_out is None and ctx.default_location_id is not None:
            hit = next((x for x in ctx.locations if x["id"] == ctx.default_location_id), None)
            if hit is not None and loc is None:
                loc_out = {"id": hit["id"], "name": hit["name"]}
        parsed = script.parse(slice_text, ctx.speakers)
        speakers: list[str] = []
        for ln in parsed.lines:
            if ln.kind != "dialogue":
                continue
            if ln.speaker is None:
                errors.append(f"{where}: speaker tag [{ln.speaker_tag}] is not a cast name")
                continue
            if ln.speaker.name not in speakers:
                speakers.append(ln.speaker.name)
            if all(x["id"] != ln.speaker.character_id for x in cast_out):
                cast_out.append({"id": ln.speaker.character_id, "name": ln.speaker.name})
        if len(speakers) > config.MAX_SPEAKERS_PER_SNIPPET:
            errors.append(f"{where}: {len(speakers)} speakers ({', '.join(speakers)}); at most 3 per snippet")
        est = parsed.estimate_s
        snippet_warnings = []
        if est > config.SNIPPET_MAX_SECONDS + EPS:
            snippet_warnings.append(f"Slice is estimated at {est:.1f}s, over 15s")
        out.append(
            {
                "script_slice": slice_text,
                "body": body,
                "cast": cast_out,
                "location": loc_out,
                "target_duration_s": dur,
                "estimate_s": round(est, 2),
                "warnings": snippet_warnings,
            }
        )
        warnings += [f"Snippet {i}: {w}" for w in snippet_warnings]
    # Every canonical dialogue line of the script should survive into some slice.
    covered = " | ".join(
        _norm_words(ln.spoken)
        for s in out
        for ln in script.parse(s["script_slice"], {}).lines
        if ln.kind == "dialogue"
    )
    for ln in script.parse(script_text, ctx.speakers).lines:
        if ln.kind == "dialogue" and _norm_words(ln.spoken) and _norm_words(ln.spoken) not in covered:
            warnings.append(f"Script line {ln.number} is missing from the split: {ln.text.strip()[:80]}")
    return out, errors, warnings


async def _ask(
    messages: list[dict[str, Any]],
    model: str,
    conn: sqlite3.Connection,
    episode_id: int,
    note: str,
    costs: list[float] | None = None,
) -> openrouter.ChatResult:
    res = await openrouter.chat(messages, model=model, max_tokens=MAX_TOKENS, temperature=0.3)
    spend.log(conn, "llm", res.cost_usd, episode_id=episode_id, note=f"{note} ({model})")
    if costs is not None:
        costs.append(res.cost_usd)
    return res


async def propose_level2(
    conn: sqlite3.Connection, episode_id: int, script_text: str, model: str, costs: list[float]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ask the model for a split; validate; one repair retry. Returns (snippets, warnings).

    Each call's cost is logged to spend and appended to `costs`.
    """
    ctx = context(conn, episode_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_message(ctx, script_text)},
    ]
    problems: list[str] = []
    for attempt in range(2):
        res = await _ask(
            messages, model, conn, episode_id, "split L2" if attempt == 0 else "split L2 repair", costs
        )
        try:
            data = extract_json(res.content)
        except json.JSONDecodeError as exc:
            problems = [f"output is not valid JSON: {exc}"]
        else:
            result, problems, warnings = validate_l2(data, ctx, script_text)
            if not problems:
                return result, warnings
        messages += [
            {"role": "assistant", "content": res.content},
            {
                "role": "user",
                "content": "Your output is invalid:\n- "
                + "\n- ".join(problems)
                + "\nReturn the corrected JSON object only.",
            },
        ]
    raise SplitError("model output invalid after one repair: " + "; ".join(problems[:10]))


def start_level2(
    conn: sqlite3.Connection, episode_id: int, version: int | None = None, model: str | None = None
) -> int:
    """Create a pending proposal and enqueue its `split_l2` job. Returns the proposal id. Commits."""
    _episode(conn, episode_id)
    sv = _script_version(conn, episode_id, version)
    model = (model or "").strip() or get_model(conn)
    remember_model(conn, model)
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO split_proposals (episode_id, script_version_id, level, model, status, created_at,"
        " updated_at) VALUES (?, ?, 2, ?, 'pending', ?, ?)",
        (episode_id, sv["id"], model, ts, ts),
    )
    proposal_id = cur.lastrowid
    conn.commit()
    job_id = jobs.enqueue(conn, "split_l2", {"proposal_id": proposal_id})
    conn.execute("UPDATE split_proposals SET job_id = ? WHERE id = ?", (job_id, proposal_id))
    conn.commit()
    snippets.publish(episode_id, split_proposal_id=proposal_id)
    return proposal_id


def _fail(conn: sqlite3.Connection, proposal_id: int, episode_id: int, error: str, cost: float = 0.0) -> None:
    conn.execute(
        "UPDATE split_proposals SET status = 'failed', error = ?, cost_usd = cost_usd + ?, updated_at = ?"
        " WHERE id = ?",
        (error, cost, db.now(), proposal_id),
    )
    conn.commit()
    snippets.publish(episode_id, split_proposal_id=proposal_id)


@jobs.handler("split_l2")
async def split_l2_job(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    proposal_id = int(payload["proposal_id"])
    job = jobs.get(job_id)
    conn = db.connect()
    try:
        p = conn.execute("SELECT * FROM split_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if p is None:
            raise LookupError(f"split proposal {proposal_id} not found")
        if p["status"] != "pending" or (p["result"] not in ("", "[]")):
            return {"proposal_id": proposal_id, "status": p["status"]}
        if job is not None and job["attempts"] > 1:
            # Interrupted by a restart mid-call: don't pay for the same split twice.
            _fail(conn, proposal_id, p["episode_id"], "interrupted by a restart; run Split again")
            return {"proposal_id": proposal_id, "status": "failed"}
        sv = conn.execute(
            "SELECT text FROM script_versions WHERE id = ?", (p["script_version_id"],)
        ).fetchone()
        costs: list[float] = []
        try:
            result, warnings = await propose_level2(conn, p["episode_id"], sv["text"], p["model"], costs)
        except (SplitError, openrouter.OpenRouterError) as exc:
            _fail(conn, proposal_id, p["episode_id"], str(exc), sum(costs))
            raise
        cost = sum(costs)
        total = sum(s["target_duration_s"] for s in result)
        length = snippets.episode_length(total)
        if length["length_warning"]:
            warnings.append(length["length_warning"])
        conn.execute(
            "UPDATE split_proposals SET result = ?, estimated_total_s = ?, warnings = ?, cost_usd = ?,"
            " updated_at = ? WHERE id = ? AND status = 'pending'",
            (db.dumps(result), total, db.dumps(warnings), cost, db.now(), proposal_id),
        )
        conn.commit()
        snippets.publish(p["episode_id"], split_proposal_id=proposal_id)
        return {"proposal_id": proposal_id, "snippets": len(result), "cost_usd": cost}
    finally:
        conn.close()


def accept(conn: sqlite3.Connection, proposal_id: int) -> list[int]:
    """Archive the current snippets and put the proposal's snippets on the board. Commits."""
    p = conn.execute("SELECT * FROM split_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if p is None:
        raise LookupError(f"split proposal {proposal_id} not found")
    result = db.loads(p["result"], [])
    if p["status"] != "pending" or not result:
        raise SplitError(f"proposal is {p['status']}" + ("" if result else " and has no result yet"))
    new = []
    for s in result:
        loc = s.get("location")
        loc_id = loc["id"] if loc else None
        if (
            loc_id is not None
            and conn.execute("SELECT 1 FROM locations WHERE id = ?", (loc_id,)).fetchone() is None
        ):
            loc_id = None
        cast_ids = [
            c["id"]
            for c in s["cast"]
            if conn.execute("SELECT 1 FROM characters WHERE id = ?", (c["id"],)).fetchone() is not None
        ]
        new.append(
            snippets.NewSnippet(
                body=s["body"],
                script_slice=s["script_slice"],
                cast_ids=cast_ids,
                location_id=loc_id,
                target_duration_s=int(s["target_duration_s"]),
                warnings=list(s.get("warnings", [])),
            )
        )
    ids = snippets.replace_board(conn, p["episode_id"], new, origin="split2", split_proposal_id=proposal_id)
    conn.execute(
        "UPDATE split_proposals SET status = 'accepted', updated_at = ? WHERE id = ?", (db.now(), proposal_id)
    )
    conn.commit()
    snippets.publish(p["episode_id"], split_proposal_id=proposal_id)
    return ids


def reject(conn: sqlite3.Connection, proposal_id: int) -> None:
    p = conn.execute("SELECT * FROM split_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if p is None:
        raise LookupError(f"split proposal {proposal_id} not found")
    if p["status"] != "pending":
        raise SplitError(f"proposal is {p['status']}")
    if p["job_id"] is not None:
        jobs.cancel(p["job_id"])
    conn.execute(
        "UPDATE split_proposals SET status = 'rejected', updated_at = ? WHERE id = ?", (db.now(), proposal_id)
    )
    conn.commit()
    snippets.publish(p["episode_id"], split_proposal_id=proposal_id)


# --- per-snippet rewrite -----------------------------------------------------

REWRITE_PROMPT = """You rewrite the body of one snippet prompt for an AI video model (one call, at most \
15 seconds, vertical video). The body is a timestamped multi-shot sequence, one shot per line, e.g. \
`0-4s: Medium shot ... Kiwi Kowalski says "..."`, with timestamps from 0 to the snippet duration. Refer to \
characters by their exact names. Describe framing, action and delivery. Include every dialogue line of \
the script slice verbatim in double quotes, attributed to its speaker. No music cues. Output only the \
body text, no preamble and no code fences."""


async def rewrite_body(
    conn: sqlite3.Connection, snippet_id: int, model: str | None = None, instructions: str = ""
) -> dict[str, Any]:
    s = snippets.get(conn, snippet_id)
    if s is None:
        raise LookupError(f"snippet {snippet_id} not found")
    model = (model or "").strip() or get_model(conn)
    cast = db.rows(
        conn.execute(
            "SELECT c.name, c.description FROM snippet_cast sc JOIN characters c ON c.id = sc.character_id"
            " WHERE sc.snippet_id = ? ORDER BY sc.position",
            (snippet_id,),
        ).fetchall()
    )
    loc = (
        conn.execute("SELECT name, description FROM locations WHERE id = ?", (s["location_id"],)).fetchone()
        if s["location_id"] is not None
        else None
    )
    info = {
        "duration_s": s["target_duration_s"],
        "cast": cast,
        "location": dict(loc) if loc else None,
    }
    user = (
        "Snippet (JSON):\n"
        + json.dumps(info, ensure_ascii=False, indent=1)
        + f"\n\nScript slice:\n<slice>\n{s['script_slice']}\n</slice>"
        + f"\n\nCurrent body:\n<body>\n{s['body']}\n</body>"
        + (f"\n\nInstructions: {instructions.strip()}" if instructions.strip() else "")
    )
    res = await _ask(
        [{"role": "system", "content": REWRITE_PROMPT}, {"role": "user", "content": user}],
        model,
        conn,
        s["episode_id"],
        f"rewrite body snippet {snippet_id}",
    )
    remember_model(conn, model)
    conn.commit()
    body = res.content.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1).strip()
    if not body:
        raise SplitError("model returned an empty body")
    return {"body": body, "cost_usd": res.cost_usd, "model": model}
