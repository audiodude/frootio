"""ffprobe/ffmpeg helpers for takes: stream info, single-frame extraction.

Blocking (subprocess.run); call through `asyncio.to_thread` from the event loop.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    width: int | None
    height: int | None
    duration_s: float | None
    fps: float | None
    has_audio: bool


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        tail = " | ".join(proc.stderr.decode(errors="replace").strip().splitlines()[-3:])
        raise MediaError(f"{cmd[0]} exited {proc.returncode}: {tail}")
    return proc


def probe(path: Path) -> MediaInfo:
    """Dimensions of the first video/image stream, duration, frame rate.

    Duration is the video stream's when there is one: H3 audio can run past the last
    frame, and trims/start frames must stay within the frames. Audio-only files use the
    container duration.
    """
    if not path.is_file():
        raise MediaError(f"missing media file: {path}")
    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ]
    )  # fmt: skip
    data = json.loads(proc.stdout or b"{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = _float(video.get("duration")) if video is not None else None
    if duration is None:
        duration = _float((data.get("format") or {}).get("duration"))
    if duration is None and streams:
        duration = max((d for s in streams if (d := _float(s.get("duration"))) is not None), default=None)
    fps = None
    if video is not None:
        for key in ("avg_frame_rate", "r_frame_rate"):
            rate = video.get(key)
            if rate and rate != "0/0":
                fps = float(Fraction(rate))
                if fps > 0:
                    break
                fps = None
    return MediaInfo(
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        duration_s=duration,
        fps=fps,
        has_audio=has_audio,
    )


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def extract_frame(video: Path, at_s: float, ext: str = "png", *, quality: int = 3) -> bytes:
    """Encoded bytes of the frame on screen at `at_s` (the last frame starting before it).

    Seeking to exactly `at_s` would return the first frame at or after it, which
    for a trim-out at the very end of the video is no frame at all. The frame a
    viewer sees at `at_s` starts within one frame interval before it.
    """
    info = probe(video)
    if info.fps is None:
        raise MediaError(f"no video stream in {video}")
    seek = max(0.0, at_s - 1.0 / info.fps)
    if info.duration_s is not None:
        # 1.5 frames back from the end so float rounding can't seek past the last frame.
        seek = min(seek, max(0.0, info.duration_s - 1.5 / info.fps))
    with tempfile.TemporaryDirectory(prefix="frootio-frame-") as tmp:
        out = Path(tmp) / f"frame.{ext}"
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{seek:.4f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
        ]
        if ext in ("jpg", "jpeg"):
            cmd += ["-q:v", str(quality)]
        _run([*cmd, str(out)])
        if not out.is_file() or out.stat().st_size == 0:
            raise MediaError(f"ffmpeg produced no frame at {at_s:.3f}s from {video}")
        return out.read_bytes()
