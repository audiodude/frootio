from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from frootio import mediaprobe


@pytest.fixture
def clip_with_long_audio(tmp_path: Path) -> Path:
    """Like H3 output: 24 fps video of 5.0s whose audio track runs to 5.2s."""
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=96x168:rate=24:duration=5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5.2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out),
        ],
        check=True,
    )  # fmt: skip
    return out


def test_duration_is_video_stream_not_container(clip_with_long_audio: Path) -> None:
    info = mediaprobe.probe(clip_with_long_audio)
    assert info.duration_s == pytest.approx(5.0, abs=0.01)


def test_frame_at_video_end_exists(clip_with_long_audio: Path) -> None:
    # Continuity extracts at the previous take's trim-out, which defaults to its duration.
    info = mediaprobe.probe(clip_with_long_audio)
    assert mediaprobe.extract_frame(clip_with_long_audio, info.duration_s)
