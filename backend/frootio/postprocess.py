"""Post-processing per take, run as the `postprocess` job after download:
probe (size, duration), poster frame, and stem separation (vocals + fx) in the
heavy subprocess. Any failure marks the take `processing_failed`; Retry re-runs
this job on the same video without a new fal call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import db, generation, jobs, media, mediaprobe

log = logging.getLogger(__name__)


@jobs.handler("postprocess")
async def postprocess(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    take_id = int(payload["take_id"])
    take = generation.load_take(take_id)
    if take["status"] != "processing":
        return {"skipped": take["status"]}
    paths = generation.take_media_paths(take)
    try:
        if take["video_path"] is None:
            raise mediaprobe.MediaError("take has no downloaded video")
        video = media.abs_path(take["video_path"])
        info = await asyncio.to_thread(mediaprobe.probe, video)
        if not info.duration_s or not info.width:
            raise mediaprobe.MediaError(f"{take['video_path']} has no readable video stream")
        poster = await asyncio.to_thread(mediaprobe.extract_frame, video, info.duration_s / 2, "jpg")
        media.write_file(paths["poster"], poster)
        generation.update_take(
            take_id,
            poster_path=paths["poster"],
            video_duration_s=info.duration_s,
            width=info.width,
            height=info.height,
        )
        if not info.has_audio:
            raise mediaprobe.MediaError("video has no audio track to separate")
        await jobs.run_heavy(
            "stems",
            {
                "input": str(video),
                "vocals_out": str(media.abs_path(paths["vocals"])),
                "fx_out": str(media.abs_path(paths["fx"])),
            },
        )
        for key in ("vocals", "fx"):
            if not media.abs_path(paths[key]).is_file():
                raise mediaprobe.MediaError(f"stem separation wrote no {key} file")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        generation.fail_take(take_id, "processing_failed", f"Post-processing failed: {exc}")
    generation.update_take(
        take_id, status="done", error=None, vocals_path=paths["vocals"], fx_path=paths["fx"]
    )
    return {"take_id": take_id, "poster_path": paths["poster"], "completed_at": db.now()}
