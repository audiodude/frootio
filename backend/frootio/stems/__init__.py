"""Speech / effects stem separation for takes, using BandIt v2.

BandIt v2 (cinematic audio source separation) splits a mix into speech, music and
effects. ``separate()`` keeps speech and effects and discards music. The model is a
mono model running at 48 kHz; stereo input is separated one channel at a time, so the
stems keep the input's stereo image. See docs/spike-bandit.md for the evaluation that
picked the checkpoint and the inference settings.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import urllib.request
import wave
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from .bandit import Bandit

# Versions validated in the spike (docs/spike-bandit.md).
REQUIREMENTS: list[str] = ["torch==2.14.1", "torchaudio==2.11.0", "numpy==2.5.4"]

SAMPLE_RATE = 48_000  # model rate; stems are written at this rate
CHANNELS = 2  # input is down/up-mixed to stereo; each channel is separated independently
STEMS = ("speech", "music", "sfx")  # model output order (training config)

CHECKPOINT = "checkpoint-multi.ckpt"  # picked in the spike over checkpoint-eng.ckpt
CHECKPOINT_URL = "https://zenodo.org/records/12701995/files/{name}?download=1"
CHECKPOINT_MD5 = {
    "checkpoint-eng.ckpt": "9b74787e7f752709ce986ba1b1ac29a9",
    "checkpoint-multi.ckpt": "fea2868787551b0cff36cfcf7c3622a3",
}
CACHE_DIR = Path.home() / ".cache" / "frootio" / "bandit"

# configs/models/bandit-mus64.yaml in the upstream repo (shared by every DnR v3 checkpoint).
MODEL_KWARGS = dict(
    in_channels=1,
    band_type="musical",
    n_bands=64,
    normalize_channel_independently=False,
    treat_channel_as_feature=True,
    n_sqm_modules=8,
    emb_dim=128,
    rnn_dim=256,
    bidirectional=True,
    rnn_type="GRU",
    mlp_dim=512,
    hidden_activation="Tanh",
    hidden_activation_kwargs=None,
    complex_mask=True,
    use_freq_weights=True,
    n_fft=2048,
    win_length=2048,
    hop_length=512,
    window_fn="hann_window",
    wkwargs=None,
    power=None,
    center=True,
    normalized=True,
    pad_mode="reflect",
    onesided=True,
)

# Chunked overlap-add inference. The model was trained on 8 s chunks.
CHUNK_SECONDS = 8.0
HOP_SECONDS = 1.0
BATCH_SIZE = 1  # chunks per forward pass (~1.1 GB peak VRAM; 2 -> ~1.9 GB, ~30% faster)

_MODELS: dict[tuple[str, str], Bandit] = {}


def separate(input_media: Path, vocals_out: Path, fx_out: Path, *, device: str | None = None) -> dict:
    """Separate the audio of ``input_media`` into a speech and an effects stem.

    Writes 16-bit stereo WAVs at 48 kHz (the model rate) to ``vocals_out`` (speech) and
    ``fx_out`` (effects: foley and ambience); the music stem is discarded. Both files
    have exactly the decoded audio's length.
    """
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    mix = decode_audio(Path(input_media))
    stems = separate_array(mix, checkpoint=CHECKPOINT, device=device)
    write_wav(Path(vocals_out), stems["speech"])
    write_wav(Path(fx_out), stems["sfx"])
    return {
        "sample_rate": SAMPLE_RATE,
        "duration": mix.shape[-1] / SAMPLE_RATE,
        "checkpoint": CHECKPOINT,
    }


def decode_audio(path: Path) -> torch.Tensor:
    """First audio stream of ``path`` as a float32 (CHANNELS, n) tensor at SAMPLE_RATE."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "-",
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg could not decode audio from {path}: {proc.stderr.decode(errors='replace').strip()}"
        )
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError(f"{path} has no audio samples")
    return torch.from_numpy(audio.reshape(-1, CHANNELS).T.copy())


@torch.inference_mode()
def separate_array(
    mix: torch.Tensor, *, checkpoint: str = CHECKPOINT, device: str = "cpu"
) -> dict[str, torch.Tensor]:
    """Separate a (channels, n) float mix at SAMPLE_RATE into all model stems (on CPU)."""
    model = load_model(checkpoint, device)
    n_channels, n_samples = mix.shape

    chunk = int(CHUNK_SECONDS * SAMPLE_RATE)
    hop = int(HOP_SECONDS * SAMPLE_RATE)
    # Pad both ends by one overlap so every real sample is covered by full overlap.
    pad = chunk - hop
    n_chunks = math.ceil((n_samples + 2 * pad - chunk) / hop) + 1
    total = (n_chunks - 1) * hop + chunk
    padded = _pad(mix, pad, total - n_samples - pad).to(device)

    # (channels * n_chunks, 1, chunk): every channel of every chunk is a mono item.
    frames = padded.unfold(-1, chunk, hop).reshape(-1, 1, chunk)
    window = torch.hann_window(chunk, periodic=True, device=device)
    out = {stem: torch.zeros(n_channels, total, device=device) for stem in STEMS}
    wsum = torch.zeros(total, device=device)
    for k in range(n_chunks):
        wsum[k * hop : k * hop + chunk] += window

    for start in range(0, frames.shape[0], BATCH_SIZE):
        batch = frames[start : start + BATCH_SIZE]
        estimates = model({"mixture": {"audio": batch}})["estimates"]
        for stem in STEMS:
            audio = estimates[stem]["audio"][:, 0] * window  # (b, chunk)
            for j, item in enumerate(range(start, start + batch.shape[0])):
                ch, k = divmod(item, n_chunks)
                out[stem][ch, k * hop : k * hop + chunk] += audio[j]

    return {stem: (out[stem] / wsum)[:, pad : pad + n_samples].float().cpu() for stem in STEMS}


def _pad(x: torch.Tensor, left: int, right: int) -> torch.Tensor:
    """Reflect-pad (as far as the signal allows), then zero-pad the remainder."""
    n = x.shape[-1]
    rl, rr = min(left, n - 1), min(right, n - 1)
    x = F.pad(x[None], (rl, rr), mode="reflect")[0] if (rl or rr) else x
    return F.pad(x, (left - rl, right - rr))


def write_wav(path: Path, audio: torch.Tensor) -> None:
    """Write a (channels, n) float tensor as 16-bit PCM WAV, atomically."""
    pcm = (audio.clamp(-1.0, 1.0) * 32767.0).round().to(torch.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(pcm.shape[0])
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.T.contiguous().numpy().tobytes())
    tmp.replace(path)


def load_model(checkpoint: str = CHECKPOINT, device: str = "cpu") -> Bandit:
    """Build BandIt v2 and load ``checkpoint``; cached per (checkpoint, device)."""
    key = (checkpoint, device)
    if key not in _MODELS:
        state = torch.load(checkpoint_path(checkpoint), map_location="cpu", weights_only=True)["state_dict"]
        # Lightning system checkpoint: the separator lives under "model.".
        state = {k[len("model.") :]: v for k, v in state.items() if k.startswith("model.")}
        model = Bandit(fs=SAMPLE_RATE, stems=list(STEMS), **MODEL_KWARGS)
        model.load_state_dict(state, strict=True)
        _MODELS[key] = model.eval().to(device)
    return _MODELS[key]


def checkpoint_path(name: str = CHECKPOINT) -> Path:
    """Local path of a Zenodo checkpoint, downloading and MD5-checking it on first use."""
    path = CACHE_DIR / name
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{name}.part")
    md5 = hashlib.md5()
    with urllib.request.urlopen(CHECKPOINT_URL.format(name=name)) as resp, tmp.open("wb") as f:
        while block := resp.read(1 << 20):
            md5.update(block)
            f.write(block)
    if md5.hexdigest() != CHECKPOINT_MD5[name]:
        tmp.unlink()
        raise RuntimeError(f"{name}: MD5 mismatch ({md5.hexdigest()} != {CHECKPOINT_MD5[name]})")
    tmp.replace(path)
    return path
