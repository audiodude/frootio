"""Heavy task `stems` (runs in the `python -m frootio.heavy` subprocess).

args: {"input": <video/audio path>, "vocals_out": <wav path>, "fx_out": <wav path>, "device"?: str}
Separates with BandIt v2 (`frootio.stems.separate`) into temp files next to the
outputs, then renames them into place so a crash never leaves a partial stem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def run(args: dict[str, Any]) -> dict[str, Any]:
    from frootio.stems import separate  # torch-heavy: imported only in the subprocess

    vocals_out = Path(args["vocals_out"])
    fx_out = Path(args["fx_out"])
    vocals_out.parent.mkdir(parents=True, exist_ok=True)
    fx_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_vocals = vocals_out.with_name(f".{vocals_out.stem}.{os.getpid()}.tmp.wav")
    tmp_fx = fx_out.with_name(f".{fx_out.stem}.{os.getpid()}.tmp.wav")
    try:
        info = separate(Path(args["input"]), tmp_vocals, tmp_fx, device=args.get("device"))
        os.replace(tmp_vocals, vocals_out)
        os.replace(tmp_fx, fx_out)
    finally:
        tmp_vocals.unlink(missing_ok=True)
        tmp_fx.unlink(missing_ok=True)
    return {
        "vocals_out": str(vocals_out),
        "fx_out": str(fx_out),
        "info": json.loads(json.dumps(info or {}, default=str)),
    }
