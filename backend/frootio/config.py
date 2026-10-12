"""Environment-driven settings and fixed constants (rates, caps, limits).

Settings are read from the environment on every call so tests can override
FROOTIO_* variables per test without import-order tricks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_MEDIA_ROOT = "~/frootio/media"
DEFAULT_DB = "~/.local/share/frootio/frootio.db"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    media_root: Path
    db_path: Path
    fal_key: str | None
    openrouter_api_key: str | None
    openrouter_model: str
    frontend_dist: Path


def settings() -> Settings:
    env = os.environ
    return Settings(
        host=env.get("FROOTIO_HOST", DEFAULT_HOST),
        port=int(env.get("FROOTIO_PORT", DEFAULT_PORT)),
        media_root=Path(env.get("FROOTIO_MEDIA_ROOT", DEFAULT_MEDIA_ROOT)).expanduser(),
        db_path=Path(env.get("FROOTIO_DB", DEFAULT_DB)).expanduser(),
        fal_key=env.get("FAL_KEY") or None,
        openrouter_api_key=env.get("OPENROUTER_API_KEY") or None,
        openrouter_model=env.get("FROOTIO_OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        frontend_dist=FRONTEND_DIST,
    )


# --- Video model (fal) --------------------------------------------------------
H3_ENDPOINT = "minimax/h3-max/reference-to-video"
H3_RESOLUTION = "768P"
H3_ASPECT_RATIO = "9:16"
H3_USD_PER_SECOND = 0.08  # billed on the requested duration
H3_INCLUDED_REF_TOKENS = 4096
H3_USD_PER_1K_EXTRA_REF_TOKENS = 0.02
# Image reference tokens by aspect ratio (w:h); other shapes need fal's full table.
H3_IMAGE_TOKENS_BY_ASPECT = {
    "1:1": 1024,
    "4:3": 1376,
    "3:4": 1376,
    "16:9": 1824,
    "9:16": 1824,
    "5:2": 2560,
    "2:5": 2560,
}
H3_AUDIO_TOKENS_PER_SECOND = 80
PROMPT_MAX_CHARS = 50_000
MAX_REF_IMAGES = 9
MAX_REF_AUDIO = 3
MAX_REF_FILES = 12
SNIPPET_MIN_SECONDS = 5
SNIPPET_MAX_SECONDS = 15
MAX_SPEAKERS_PER_SNIPPET = 3
EPISODE_TARGET_MIN_SECONDS = 30
EPISODE_TARGET_MAX_SECONDS = 45
FIXED_TAIL = "No music. Dialogue, foley and ambience only."
SLOW_TAKE_SECONDS = 30 * 60
DOWNLOAD_MAX_TRIES = 3

# --- Other paid calls (fixed per-call rates copied from fal pricing pages) -----
NANO_BANANA_ENDPOINT = "fal-ai/nano-banana"
NANO_BANANA_EDIT_ENDPOINT = "fal-ai/nano-banana/edit"
NANO_BANANA_USD_PER_IMAGE = 0.039  # fal.ai/models/fal-ai/nano-banana (same for /edit)
CHATTERBOX_ENDPOINT = "resemble-ai/chatterboxhd/text-to-speech"
CHATTERBOX_USD_PER_1K_CHARS = 0.04  # fal.ai/models/resemble-ai/chatterboxhd/text-to-speech

# --- Characters ----------------------------------------------------------------
VOICE_SAMPLE_MIN_SECONDS = 2.0
VOICE_SAMPLE_MAX_SECONDS = 5.0

# --- Spend ---------------------------------------------------------------------
DEFAULT_SPEND_CAP_USD = 20.0

# --- Backups -------------------------------------------------------------------
BACKUP_DIRNAME = ".frootio-backup"
BACKUP_KEEP = 14
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60

# --- Jobs ----------------------------------------------------------------------
JOB_MAX_CONCURRENT = 32
JOB_POLL_INTERVAL_SECONDS = 1.0
