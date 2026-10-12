"""Characters: CRUD, aliases, archive, images (upload/generate/set main), voice samples."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from .. import config, db, jobs, media, voice
from ..library import add_image, character_json, image_ext, image_json, rename_character, set_aliases
from ..paidgen import (
    CHATTERBOX_VOICES,
    IMAGE_JOB,
    MAX_IMAGES_PER_CALL,
    NANO_BANANA_ASPECTS,
    TTS_JOB,
    tts_cost,
)
from .library_common import generate_images, owner_jobs, raise_http

router = APIRouter(prefix="/characters", tags=["characters"])


class CharacterCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    aliases: list[str] = []


class CharacterPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    archived: bool | None = None


class AliasesIn(BaseModel):
    aliases: list[str]


class MainImageIn(BaseModel):
    image_id: int


class GenerateImagesIn(BaseModel):
    prompt: str = Field(min_length=1)
    num_images: int = Field(default=2, ge=1, le=4)
    input_paths: list[str] = []
    aspect_ratio: str | None = None


class VoiceClipIn(BaseModel):
    start_s: float
    end_s: float
    source_path: str | None = None  # staged upload/TTS source or an existing voice sample
    take_id: int | None = None  # clip from this take's vocals stem


class VoicePointerIn(BaseModel):
    voice_id: int | None


class TtsIn(BaseModel):
    voice: str
    text: str = Field(min_length=1, max_length=500)
    exaggeration: float | None = Field(default=None, ge=0.25, le=2)


def _get(conn, character_id: int):
    c = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if c is None:
        raise HTTPException(404, "character not found")
    return c


def _detail(conn, character_id: int) -> dict[str, Any]:
    out = character_json(conn, _get(conn, character_id))
    out["images"] = [
        image_json(r)
        for r in conn.execute(
            "SELECT * FROM character_images WHERE character_id = ? ORDER BY id DESC", (character_id,)
        )
    ]
    out["voices"] = db.rows(
        conn.execute(
            "SELECT * FROM character_voices WHERE character_id = ? ORDER BY id DESC", (character_id,)
        ).fetchall()
    )
    out["spend_usd"] = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE character_id = ?", (character_id,)
    ).fetchone()[0]
    return out


def _touch(conn, character_id: int) -> None:
    conn.execute("UPDATE characters SET updated_at = ? WHERE id = ?", (db.now(), character_id))


# --- voice helpers not tied to one character (declared before /{character_id}) ---


@router.get("/voice/takes")
def vocal_takes(conn: db.Conn) -> list[dict[str, Any]]:
    """Takes that have a separated vocals stem (clip sources for voice samples)."""
    return db.rows(
        conn.execute(
            "SELECT t.id, t.snippet_id, t.episode_id, t.vocals_path, t.video_path, t.poster_path,"
            " t.video_duration_s, t.created_at, e.number AS episode_number, s.name AS series_name"
            " FROM takes t JOIN episodes e ON e.id = t.episode_id JOIN series s ON s.id = e.series_id"
            " WHERE t.vocals_path IS NOT NULL ORDER BY t.id DESC"
        ).fetchall()
    )


@router.get("/voice/peaks")
def voice_peaks(conn: db.Conn, path: str = Query(...)) -> dict[str, Any]:
    """Waveform peaks for the voice clipper (staged sources, voice samples, take vocals)."""
    try:
        src = media.abs_path(path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not voice.is_clip_source(conn, path) or not src.is_file():
        raise HTTPException(404, "not a clip source")
    with raise_http():
        return {"path": path, **voice.peaks(src)}


@router.get("/generation-options")
def generation_options() -> dict[str, Any]:
    """Chatterbox presets, nano-banana options and the per-call rates used for estimates."""
    return {
        "voices": list(CHATTERBOX_VOICES),
        "usd_per_1k_chars": config.CHATTERBOX_USD_PER_1K_CHARS,
        "nano_banana_usd_per_image": config.NANO_BANANA_USD_PER_IMAGE,
        "aspect_ratios": list(NANO_BANANA_ASPECTS),
        "max_images": MAX_IMAGES_PER_CALL,
        "min_s": config.VOICE_SAMPLE_MIN_SECONDS,
        "max_s": config.VOICE_SAMPLE_MAX_SECONDS,
    }


# --- characters --------------------------------------------------------------------


@router.get("")
def list_characters(conn: db.Conn, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM characters" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY name"
    return [character_json(conn, c) for c in conn.execute(sql).fetchall()]


@router.post("", status_code=201)
def create_character(body: CharacterCreate, conn: db.Conn) -> dict[str, Any]:
    name = " ".join(body.name.split())
    ts = db.now()
    slug = db.unique_slug(conn, "characters", media.slugify(name))
    cid = conn.execute(
        "INSERT INTO characters (name, slug, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, slug, body.description, ts, ts),
    ).lastrowid
    with raise_http(conn):
        set_aliases(conn, cid, body.aliases)
    conn.commit()
    return _detail(conn, cid)


@router.get("/{character_id}")
def get_character(character_id: int, conn: db.Conn) -> dict[str, Any]:
    return _detail(conn, character_id)


@router.patch("/{character_id}")
def patch_character(character_id: int, body: CharacterPatch, conn: db.Conn) -> dict[str, Any]:
    _get(conn, character_id)
    with raise_http(conn):
        if body.name is not None:
            rename_character(conn, character_id, body.name)
        if body.description is not None:
            conn.execute(
                "UPDATE characters SET description = ? WHERE id = ?", (body.description, character_id)
            )
        if body.archived is not None:
            conn.execute(
                "UPDATE characters SET archived = ? WHERE id = ?", (int(body.archived), character_id)
            )
        _touch(conn, character_id)
    conn.commit()
    return _detail(conn, character_id)


@router.put("/{character_id}/aliases")
def put_aliases(character_id: int, body: AliasesIn, conn: db.Conn) -> dict[str, Any]:
    _get(conn, character_id)
    with raise_http(conn):
        set_aliases(conn, character_id, body.aliases)
        _touch(conn, character_id)
    conn.commit()
    return _detail(conn, character_id)


# --- images ------------------------------------------------------------------------


@router.post("/{character_id}/images", status_code=201)
def upload_images(
    character_id: int, conn: db.Conn, files: Annotated[list[UploadFile], File()]
) -> dict[str, Any]:
    c = _get(conn, character_id)
    with raise_http(conn):
        ids = [_add_upload(conn, character_id, f) for f in files]
        if c["main_image_id"] is None and ids:
            conn.execute("UPDATE characters SET main_image_id = ? WHERE id = ?", (ids[0], character_id))
        _touch(conn, character_id)
    conn.commit()
    return {"image_ids": ids, "character": _detail(conn, character_id)}


@router.put("/{character_id}/main-image")
def set_main_image(character_id: int, body: MainImageIn, conn: db.Conn) -> dict[str, Any]:
    _get(conn, character_id)
    if not conn.execute(
        "SELECT 1 FROM character_images WHERE id = ? AND character_id = ?", (body.image_id, character_id)
    ).fetchone():
        raise HTTPException(404, "image not found for this character")
    conn.execute("UPDATE characters SET main_image_id = ? WHERE id = ?", (body.image_id, character_id))
    _touch(conn, character_id)
    conn.commit()
    return _detail(conn, character_id)


@router.post("/{character_id}/images/generate", status_code=202)
def generate(character_id: int, body: GenerateImagesIn, conn: db.Conn) -> dict[str, Any]:
    _get(conn, character_id)
    return generate_images(
        conn, "character", character_id, body.prompt, body.num_images, body.input_paths, body.aspect_ratio
    )


@router.get("/{character_id}/jobs")
def character_jobs(character_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    _get(conn, character_id)
    return owner_jobs(conn, "character", character_id, (IMAGE_JOB, TTS_JOB))


# --- voice -------------------------------------------------------------------------


@router.post("/{character_id}/voice/upload")
def upload_voice(character_id: int, conn: db.Conn, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Any audio/video file; converted to wav. 2-5s saves; >5s returns a clipper source."""
    _get(conn, character_id)
    with raise_http(conn), tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ("upload" + Path(file.filename or "").suffix[:10])
        src.write_bytes(file.file.read())
        out = voice.ingest(conn, character_id, src, "upload")
    conn.commit()
    return {**out, "character": _detail(conn, character_id)}


@router.post("/{character_id}/voice/clip")
def clip_voice(character_id: int, body: VoiceClipIn, conn: db.Conn) -> dict[str, Any]:
    """Save [start_s, end_s] (2-5s) of a staged source or a take's vocals stem as the voice sample."""
    _get(conn, character_id)
    if (body.source_path is None) == (body.take_id is None):
        raise HTTPException(422, "give exactly one of source_path or take_id")
    meta: dict[str, Any] = {}
    if body.take_id is not None:
        take = conn.execute("SELECT vocals_path FROM takes WHERE id = ?", (body.take_id,)).fetchone()
        if take is None or not take["vocals_path"]:
            raise HTTPException(404, "take has no vocals stem")
        rel, source = take["vocals_path"], "take_clip"
        meta["source_take_id"] = body.take_id
    else:
        rel = body.source_path
        try:
            media.abs_path(rel)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not voice.is_clip_source(conn, rel):
            raise HTTPException(422, "source_path is not a voice clip source")
        source, meta = _clip_source_meta(conn, rel)
    with raise_http(conn):
        voice_id = voice.clip(conn, character_id, rel, body.start_s, body.end_s, source, **meta)
        _touch(conn, character_id)
    conn.commit()
    return {"voice_id": voice_id, "character": _detail(conn, character_id)}


def _clip_source_meta(conn, rel: str) -> tuple[str, dict[str, Any]]:
    """Provenance of a clipper source: a Chatterbox result, an earlier sample, or an upload."""
    job = conn.execute(
        "SELECT payload, fal_request_id FROM jobs"
        " WHERE kind = ? AND json_extract(result, '$.source.path') = ?"
        " ORDER BY id DESC LIMIT 1",
        (TTS_JOB, rel),
    ).fetchone()
    if job is not None:
        p = db.loads(job["payload"], {})
        return "chatterbox", {
            "tts_voice": p.get("voice"),
            "tts_text": p.get("text"),
            "gen_request_id": job[1],
        }
    prior = conn.execute(
        "SELECT source, source_take_id, tts_voice, tts_text FROM character_voices WHERE path = ? LIMIT 1",
        (rel,),
    ).fetchone()
    if prior is None:
        return "upload", {}
    if prior["source"] == "chatterbox":
        return "chatterbox", {"tts_voice": prior["tts_voice"], "tts_text": prior["tts_text"]}
    if prior["source"] == "take_clip":
        return "take_clip", {"source_take_id": prior["source_take_id"]}
    return prior["source"], {}


@router.put("/{character_id}/voice")
def set_voice(character_id: int, body: VoicePointerIn, conn: db.Conn) -> dict[str, Any]:
    """Move the voice pointer to an earlier sample (or clear it: text-only in prompts)."""
    _get(conn, character_id)
    if (
        body.voice_id is not None
        and not conn.execute(
            "SELECT 1 FROM character_voices WHERE id = ? AND character_id = ?", (body.voice_id, character_id)
        ).fetchone()
    ):
        raise HTTPException(404, "voice sample not found for this character")
    conn.execute("UPDATE characters SET voice_id = ? WHERE id = ?", (body.voice_id, character_id))
    _touch(conn, character_id)
    conn.commit()
    return _detail(conn, character_id)


@router.post("/{character_id}/voice/generate", status_code=202)
def generate_voice(character_id: int, body: TtsIn, conn: db.Conn) -> dict[str, Any]:
    _get(conn, character_id)
    if body.voice not in CHATTERBOX_VOICES:
        raise HTTPException(422, f"unknown Chatterbox voice {body.voice!r}")
    job_id = jobs.enqueue(
        conn,
        TTS_JOB,
        {
            "owner": "character",
            "owner_id": character_id,
            "voice": body.voice,
            "text": body.text,
            "exaggeration": body.exaggeration,
        },
    )
    return {"job_id": job_id, "estimated_cost_usd": tts_cost(body.text)}


def _add_upload(conn, character_id: int, f: UploadFile) -> int:
    data = f.file.read()
    return add_image(conn, "character", character_id, data, image_ext(f.filename, f.content_type, data))
