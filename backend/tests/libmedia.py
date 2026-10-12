"""Tiny real media files (made with ffmpeg) for library tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def png(path: Path, color: str = "red", size: str = "4x6") -> bytes:
    _ffmpeg("-f", "lavfi", "-i", f"color={color}:s={size}", "-frames:v", "1", str(path))
    return path.read_bytes()


def tone(path: Path, seconds: float, freq: int = 440) -> Path:
    """A sine tone; the container follows the extension (wav, mp3, m4a, ...)."""
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", str(path))
    return path
