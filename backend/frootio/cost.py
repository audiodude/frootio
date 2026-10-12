"""H3 cost formula, reference-token counting, estimates and the episode spend cap.

fal returns no cost, so every take's cost comes from fal's published formula:

    cost = duration * $0.08 + max(0, ref_tokens - 4096) * $0.02 / 1000

billed on the *requested* duration. Image tokens depend on the image's aspect
ratio class (nearest of the classes in `config.H3_IMAGE_TOKENS_BY_ASPECT`,
using the file's real dimensions); reference audio is ~80 tokens/s of its real
duration. Only the reference lists count; the continuity start frame is the
first frame (`image_url`), not a reference.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config, media, mediaprobe, spend

IN_FLIGHT_STATUSES = ("queued", "running")


def _ratio(cls: str) -> float:
    w, h = cls.split(":")
    return int(w) / int(h)


def aspect_class(width: int, height: int) -> str:
    """Nearest listed aspect class (compared on a log scale, so 2:1 is as far from 1:1 as 1:2)."""
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid image size {width}x{height}")
    r = math.log(width / height)
    return min(config.H3_IMAGE_TOKENS_BY_ASPECT, key=lambda c: abs(math.log(_ratio(c)) - r))


def image_tokens(width: int, height: int) -> int:
    return config.H3_IMAGE_TOKENS_BY_ASPECT[aspect_class(width, height)]


def audio_tokens(duration_s: float) -> int:
    return math.ceil(duration_s * config.H3_AUDIO_TOKENS_PER_SECOND)


def h3_cost(duration_s: float, ref_tokens: int) -> float:
    extra = max(0, ref_tokens - config.H3_INCLUDED_REF_TOKENS)
    usd = duration_s * config.H3_USD_PER_SECOND + extra * config.H3_USD_PER_1K_EXTRA_REF_TOKENS / 1000
    return round(usd, 6)


def _get(item: Any, name: str) -> Any:
    return item[name] if isinstance(item, dict) else getattr(item, name)


@lru_cache(maxsize=1024)
def _file_tokens(abs_path: str, sha: str, kind: str) -> int:
    # Cached per content hash: ref files are content-addressed and never change.
    info = mediaprobe.probe(Path(abs_path))
    if kind == "image":
        if not info.width or not info.height:
            raise mediaprobe.MediaError(f"cannot read image size of {abs_path}")
        return image_tokens(info.width, info.height)
    if kind == "audio":
        if info.duration_s is None:
            raise mediaprobe.MediaError(f"cannot read audio duration of {abs_path}")
        return audio_tokens(info.duration_s)
    raise ValueError(f"unknown ref kind {kind!r}")


def ref_token_counts(refs: list[Any]) -> list[int]:
    """Reference tokens per ref (items with kind, path, sha), probing the real files."""
    return [_file_tokens(str(media.abs_path(_get(r, "path"))), _get(r, "sha"), _get(r, "kind")) for r in refs]


def ref_tokens(refs: list[Any]) -> int:
    return sum(ref_token_counts(refs))


@dataclass(frozen=True)
class Estimate:
    duration_s: int
    ref_tokens: int
    per_take_usd: float
    n: int

    @property
    def total_usd(self) -> float:
        return round(self.per_take_usd * self.n, 6)


def estimate(refs: list[Any], duration_s: int, n: int = 1) -> Estimate:
    tokens = ref_tokens(refs)
    return Estimate(duration_s, tokens, h3_cost(duration_s, tokens), n)


# --- spend cap -------------------------------------------------------------------


@dataclass(frozen=True)
class CapStatus:
    cap_usd: float
    video_spend_usd: float
    in_flight_usd: float
    new_usd: float

    @property
    def projected_usd(self) -> float:
        return round(self.video_spend_usd + self.in_flight_usd + self.new_usd, 6)

    @property
    def exceeded(self) -> bool:
        return self.projected_usd > self.cap_usd + 1e-9

    def message(self) -> str:
        return (
            f"Episode spend cap ${self.cap_usd:.2f} would be exceeded: video spend "
            f"${self.video_spend_usd:.2f} + in flight ${self.in_flight_usd:.2f} + this generate "
            f"${self.new_usd:.2f} = ${self.projected_usd:.2f}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cap_usd": self.cap_usd,
            "video_spend_usd": round(self.video_spend_usd, 6),
            "in_flight_usd": round(self.in_flight_usd, 6),
            "new_usd": round(self.new_usd, 6),
            "projected_usd": self.projected_usd,
            "exceeded": self.exceeded,
        }


def in_flight_usd(conn: sqlite3.Connection, episode_id: int) -> float:
    """Estimates of takes not yet completed at fal (their cost isn't in the spend table yet)."""
    marks = ",".join("?" * len(IN_FLIGHT_STATUSES))
    return conn.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM takes"
        f" WHERE episode_id = ? AND status IN ({marks})",
        (episode_id, *IN_FLIGHT_STATUSES),
    ).fetchone()[0]


def cap_status(conn: sqlite3.Connection, episode_id: int, new_usd: float) -> CapStatus:
    r = conn.execute("SELECT spend_cap_usd FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if r is None:
        raise ValueError(f"episode {episode_id} not found")
    return CapStatus(
        cap_usd=r[0],
        video_spend_usd=spend.episode_total(conn, episode_id, "video"),
        in_flight_usd=in_flight_usd(conn, episode_id),
        new_usd=new_usd,
    )
