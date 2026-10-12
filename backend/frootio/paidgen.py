"""Paid library generation jobs: nano-banana ref images and Chatterbox HD voice samples.

Both run as jobs (`image_gen`, `tts_gen`) with payload `{owner, owner_id, ...}`.
The fal request id is saved right after submitting; a resumed job polls it
instead of submitting (and paying) again. Costs come from the fixed per-call
rates in `config` and are logged once per job against the character/location.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from . import config, db, events, falapi, jobs, media, spend, voice
from .library import add_image, image_ext

log = logging.getLogger(__name__)

IMAGE_JOB = "image_gen"
TTS_JOB = "tts_gen"
CHATTERBOX_VOICES = ("Aurora", "Blade", "Britney", "Carl", "Cliff", "Richard", "Rico", "Siobhan", "Vicky")
NANO_BANANA_ASPECTS = ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16")
MAX_IMAGES_PER_CALL = 4  # fal num_images maximum
POLL_START_S = 1.0
POLL_MAX_S = 5.0


class FalJobError(RuntimeError):
    pass


def image_cost(n_images: int) -> float:
    return round(n_images * config.NANO_BANANA_USD_PER_IMAGE, 6)


def tts_cost(text: str) -> float:
    return round(len(text) / 1000 * config.CHATTERBOX_USD_PER_1K_CHARS, 6)


async def run_fal(
    job_id: int, endpoint: str, make_arguments: Callable[[], Awaitable[dict[str, Any]]]
) -> tuple[str, dict[str, Any]]:
    """Submit once (or resume by saved request id), poll with backoff, return (request_id, result)."""
    request_id = (jobs.get(job_id) or {}).get("fal_request_id")
    if not request_id:
        arguments = await make_arguments()
        request_id = await falapi.submit(endpoint, arguments)
        jobs.set_fal_request_id(job_id, request_id)
    delay = POLL_START_S
    while True:
        st = await falapi.status(endpoint, request_id)
        if st.state == "completed":
            if st.error:
                raise FalJobError(f"fal {st.error_type or 'error'}: {st.error}")
            break
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, POLL_MAX_S)
    try:
        return request_id, await falapi.result(endpoint, request_id)
    except falapi.FalClientHTTPError as exc:
        raise FalJobError(f"fal error: {exc}") from exc


async def download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def _log_spend_once(
    job_id: int, kind: spend.Kind, amount: float, owner: str, owner_id: int, note: str
) -> None:
    tag = f"(job {job_id})"
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM spend WHERE note LIKE ?", (f"%{tag}",)).fetchone():
            return
        spend.log(
            conn,
            kind,
            amount,
            character_id=owner_id if owner == "character" else None,
            location_id=owner_id if owner == "location" else None,
            note=f"{note} {tag}",
        )
    finally:
        conn.close()


def _publish(owner: str, owner_id: int) -> None:
    events.publish({"type": "library", "owner": owner, "id": owner_id})


@jobs.handler(IMAGE_JOB)
async def image_gen(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    owner, owner_id = payload["owner"], payload["owner_id"]
    input_paths: list[str] = payload.get("input_paths") or []
    endpoint = config.NANO_BANANA_EDIT_ENDPOINT if input_paths else config.NANO_BANANA_ENDPOINT

    async def make_arguments() -> dict[str, Any]:
        args: dict[str, Any] = {
            "prompt": payload["prompt"],
            "num_images": payload["num_images"],
            "output_format": "png",
        }
        if payload.get("aspect_ratio"):
            args["aspect_ratio"] = payload["aspect_ratio"]
        if input_paths:
            args["image_urls"] = [await falapi.upload_file(media.abs_path(p)) for p in input_paths]
        return args

    request_id, result = await run_fal(job_id, endpoint, make_arguments)
    images = result.get("images") or []
    _log_spend_once(job_id, "image", image_cost(len(images)), owner, owner_id, f"{endpoint} x{len(images)}")
    image_ids: list[int] = []
    for img in images:
        data = await download(img["url"])
        ext = image_ext(img.get("file_name"), img.get("content_type"), data)
        conn = db.connect()
        try:
            image_ids.append(
                await asyncio.to_thread(
                    add_image,
                    conn,
                    owner,
                    owner_id,
                    data,
                    ext,
                    source="generated",
                    gen_prompt=payload["prompt"],
                    gen_model=endpoint,
                    gen_input_paths=input_paths,
                    gen_request_id=request_id,
                )
            )
            conn.commit()
        finally:
            conn.close()
    _publish(owner, owner_id)
    return {"image_ids": image_ids, "request_id": request_id, "cost_usd": image_cost(len(images))}


@jobs.handler(TTS_JOB)
async def tts_gen(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    owner_id = payload["owner_id"]
    text, preset = payload["text"], payload["voice"]

    async def make_arguments() -> dict[str, Any]:
        args: dict[str, Any] = {"text": text, "voice": preset}
        if payload.get("exaggeration") is not None:
            args["exaggeration"] = payload["exaggeration"]
        return args

    request_id, result = await run_fal(job_id, config.CHATTERBOX_ENDPOINT, make_arguments)
    _log_spend_once(job_id, "tts", tts_cost(text), "character", owner_id, f"chatterbox {preset}")
    data = await download(result["audio"]["url"])

    def save() -> dict[str, Any]:
        conn = db.connect()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "tts.audio"
                src.write_bytes(data)
                out = voice.ingest(
                    conn,
                    owner_id,
                    src,
                    "chatterbox",
                    tts_voice=preset,
                    tts_text=text,
                    gen_request_id=request_id,
                )
            conn.commit()
            return out
        finally:
            conn.close()

    out = await asyncio.to_thread(save)
    _publish("character", owner_id)
    return {**out, "request_id": request_id, "cost_usd": tts_cost(text)}
