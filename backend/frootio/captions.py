"""Caption text, forced alignment (with fallback) and Shorts-style ASS generation.

Pipeline (SPEC "Post-processing per take" + "Assembly / Captions"):

1. `caption_lines(script_slice)` keeps only canonical dialogue lines
   (`[Speaker]: line`) with the speaker tag and `*directions*` removed.
2. `run_align` (heavy subprocess task `align`) force-aligns those lines to the
   take's vocals stem with stable-ts `align()` on a faster-whisper model. A line
   that fails to align, or whose words total near-zero duration (typical when
   H3 ad-libs), has its words spread evenly across the speech regions of the
   vocals stem (energy-based VAD). Word times are relative to the take start.
3. `build_ass` turns the per-take words into one ASS script for the assembled
   timeline: 1-3 words per event, bold and outlined, lower-middle safe area,
   white text with the active word popping bright yellow.

Model choice: faster-whisper `large-v3` (already the most accurate English
aligner of the Whisper family, and alignment only runs on a few short takes).
On CUDA it runs as `int8_float16` (~2 GB VRAM, leaving room for BandIt on the
same 12 GB card); on CPU, or if the GPU is out of memory, as `int8` (a 10s take
aligns in a few seconds). Override with `FROOTIO_WHISPER_MODEL`.

Dialogue lines come from `script.caption_lines` (the canonical grammar parser);
this module only strips caption-only markup on top (`_emphasis_`, stray `*`).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import script

WHISPER_MODEL_DEFAULT = "large-v3"
WHISPER_LANGUAGE = "en"
CAPTION_FONT = "DejaVu Sans"  # bold weight via the style; any fontconfig family works


def whisper_model() -> str:
    return os.environ.get("FROOTIO_WHISPER_MODEL") or WHISPER_MODEL_DEFAULT


# --- caption text ------------------------------------------------------------------

_UNCLOSED_DIRECTION_RE = re.compile(r"\*")
_EMPHASIS_RE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_ALNUM_RE = re.compile(r"[^\w]|_", re.UNICODE)


def caption_lines(script_slice: str) -> list[str]:
    """Dialogue lines of a script slice with `[Speaker]:`, `*directions*` and markup removed."""
    out: list[str] = []
    for spoken in script.caption_lines(script_slice):
        text = _UNCLOSED_DIRECTION_RE.sub(" ", spoken)  # an unclosed `*` is not caption text
        text = " ".join(_EMPHASIS_RE.sub(r"\1", text).split())
        if text:
            out.append(text)
    return out


def text_hash(text: str) -> str:
    """Cache key for `caption_alignments.slice_sha256` (hash of the caption text)."""
    return hashlib.sha256(text.encode()).hexdigest()


def _norm(word: str) -> str:
    return _ALNUM_RE.sub("", word.lower())


def line_words(line: str) -> list[str]:
    """Whitespace tokens of a caption line; punctuation-only tokens join a neighbour."""
    words: list[str] = []
    pending = ""
    for tok in line.split():
        if not _norm(tok):
            if words:
                words[-1] += " " + tok
            else:
                pending = (pending + " " + tok).strip()
            continue
        words.append(f"{pending} {tok}".strip() if pending else tok)
        pending = ""
    if pending and not words:
        return []
    return words


# --- audio + speech regions ------------------------------------------------------------

SAMPLE_RATE = 16_000


def load_audio(path: str, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio/video file to mono float32 at `sr` with ffmpeg."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sr),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {path}: {proc.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


VAD_FRAME_S = 0.02
VAD_FLOOR_DB = -45.0  # never call anything quieter than this speech
VAD_BELOW_PEAK_DB = 35.0  # ... or more than this below the loudest frame
VAD_MERGE_GAP_S = 0.2  # bridge pauses shorter than this
VAD_MIN_REGION_S = 0.12  # drop blips shorter than this
VAD_PAD_S = 0.05


def speech_regions(samples: np.ndarray, sr: int = SAMPLE_RATE) -> list[tuple[float, float]]:
    """Energy-based VAD: (start, end) seconds of regions with speech-level energy."""
    hop = max(1, int(sr * VAD_FRAME_S))
    n = len(samples) // hop
    if n == 0:
        return []
    frames = samples[: n * hop].reshape(n, hop).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-10))
    threshold = max(VAD_FLOOR_DB, float(db.max()) - VAD_BELOW_PEAK_DB)
    active = db > threshold
    regions: list[list[float]] = []
    for i, on in enumerate(active):
        if not on:
            continue
        start, end = i * VAD_FRAME_S, (i + 1) * VAD_FRAME_S
        if regions and start - regions[-1][1] <= VAD_MERGE_GAP_S:
            regions[-1][1] = end
        else:
            regions.append([start, end])
    duration = len(samples) / sr
    return [
        (max(0.0, s - VAD_PAD_S), min(duration, e + VAD_PAD_S))
        for s, e in regions
        if e - s >= VAD_MIN_REGION_S
    ]


# --- merging alignment with the fallback ------------------------------------------------

# A line is degenerate when its words average less than this many seconds each.
MIN_WORD_DURATION_S = 0.05
# When a fallback window has no room at all, give each word this long.
FALLBACK_WORD_S = 0.25

Timing = tuple[float, float]


def _line_ok(timings: Sequence[Timing | None] | None) -> bool:
    if timings is None or any(t is None for t in timings):
        return False
    total = sum(max(0.0, e - s) for s, e in timings)  # type: ignore[misc]
    return total >= MIN_WORD_DURATION_S * len(timings)


def _spread(n: int, window: Timing, regions: Sequence[Timing], duration: float) -> list[Timing]:
    """Spread `n` words evenly over the speech inside `window` (or the window itself)."""
    w0, w1 = window
    speech = [(max(s, w0), min(e, w1)) for s, e in regions if min(e, w1) > max(s, w0)]
    if not speech:
        if w1 - w0 <= 1e-6:
            w1 = min(duration, w0 + FALLBACK_WORD_S * n) if duration > w0 else w0 + FALLBACK_WORD_S * n
        speech = [(w0, w1)]
    total = sum(e - s for s, e in speech)
    if total <= 1e-9:
        return [(w0, w0)] * n

    def at(pos: float, *, is_end: bool) -> float:
        # a start exactly on a region boundary belongs to the next region; an end to this one
        acc = 0.0
        for s, e in speech:
            limit = acc + (e - s)
            if pos < limit - 1e-9 or (is_end and pos <= limit + 1e-9):
                return s + (pos - acc)
            acc = limit
        return speech[-1][1]

    step = total / n
    out: list[Timing] = []
    for k in range(n):
        start = at(k * step, is_end=False)
        # an end exactly on a boundary closes the region it is in (no word spills into a pause)
        end = at((k + 1) * step, is_end=True)
        out.append((round(start, 3), round(max(start, end), 3)))
    return out


# An aligned word shorter than this is treated as squashed by silence suppression.
SQUASHED_WORD_S = 0.15


def _repair_short_words(timings: list[Timing], regions: Sequence[Timing], floor: float) -> list[Timing]:
    """Stretch a squashed word (stable-ts silence suppression often pins a line's first
    word to ~0.1s) from the speech onset after its predecessor to the next word's start.
    `floor` is where the previous line ended."""
    out = list(timings)
    for k, (s, e) in enumerate(out):
        if e - s >= SQUASHED_WORD_S or k + 1 >= len(out):
            continue
        prev_end = out[k - 1][1] if k else floor
        nxt = out[k + 1][0]
        # the speech region the word starts in, else the first one starting after its predecessor
        containing = [max(a, prev_end) for a, b in regions if a <= s < b and b > prev_end]
        later = [a for a, _ in regions if prev_end <= a < nxt]
        start = (containing or later or [max(s, prev_end)])[0]
        if nxt - start > e - s:
            out[k] = (round(start, 3), round(nxt, 3))
    return out


def merge_alignment(
    lines: Sequence[Sequence[str]],
    aligned: Sequence[Sequence[Timing | None] | None],
    regions: Sequence[Timing],
    duration: float,
) -> tuple[list[dict[str, Any]], str]:
    """Combine per-line alignment with the even-spread fallback.

    `lines[i]` are line i's words; `aligned[i]` their (start, end) timings, None for
    a failed line or word. Consecutive bad lines share the gap between the aligned
    lines around them. Returns ({word, start, end, line} list, method).
    """
    ok = [bool(words) and _line_ok(aligned[i]) for i, words in enumerate(lines)]
    timings: list[list[Timing]] = [[] for _ in lines]
    used_fallback = used_aligned = False
    i = 0
    while i < len(lines):
        if not lines[i]:
            i += 1
            continue
        if ok[i]:
            floor = next((timings[k][-1][1] for k in range(i - 1, -1, -1) if timings[k]), 0.0)
            timings[i] = _repair_short_words(
                [(round(s, 3), round(e, 3)) for s, e in aligned[i]],  # type: ignore[misc,union-attr]
                regions,
                floor,
            )
            used_aligned = True
            i += 1
            continue
        j = i
        while j < len(lines) and (not lines[j] or not ok[j]):
            j += 1
        prev_end = 0.0
        for k in range(i - 1, -1, -1):
            if timings[k]:
                prev_end = timings[k][-1][1]
                break
        next_start = duration
        if j < len(lines):
            next_start = max(prev_end, min(s for s, _ in aligned[j]))  # type: ignore[misc,union-attr]
        run = [k for k in range(i, j) if lines[k]]
        spread = _spread(sum(len(lines[k]) for k in run), (prev_end, next_start), regions, duration)
        pos = 0
        for k in run:
            timings[k] = spread[pos : pos + len(lines[k])]
            pos += len(lines[k])
        used_fallback = True
        i = j
    words = [
        {"word": w, "start": s, "end": e, "line": li}
        for li, line in enumerate(lines)
        for w, (s, e) in zip(line, timings[li], strict=True)
    ]
    method = "mixed" if used_fallback and used_aligned else ("fallback" if used_fallback else "aligned")
    return words, method


def map_result_words(
    lines: Sequence[Sequence[str]], result_words: Sequence[tuple[str, float, float]]
) -> list[list[Timing | None]]:
    """Map aligner output words back onto our caption words by matching characters.

    stable-ts re-tokenises the text, so words are matched on their lowercase
    alphanumeric characters; a caption word takes the span of every result word
    sharing a matched character with it. Unmatched words get None.
    """
    ours = "".join(_norm(w) for line in lines for w in line)
    owner: list[tuple[int, int]] = [
        (li, wi) for li, line in enumerate(lines) for wi, w in enumerate(line) for _ in _norm(w)
    ]
    theirs_chars: list[int] = []
    for ri, (w, _, _) in enumerate(result_words):
        theirs_chars.extend([ri] * len(_norm(w)))
    theirs = "".join(_norm(w) for w, _, _ in result_words)
    spans: dict[tuple[int, int], list[int]] = {}
    sm = difflib.SequenceMatcher(None, ours, theirs, autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            spans.setdefault(owner[a + k], []).append(theirs_chars[b + k])
    out: list[list[Timing | None]] = []
    for li, line in enumerate(lines):
        row: list[Timing | None] = []
        for wi, w in enumerate(line):
            idx = spans.get((li, wi))
            # a stray shared letter is not a match: at least half the word's characters must align
            if not idx or 2 * len(idx) < len(_norm(w)):
                row.append(None)
                continue
            row.append((min(result_words[r][1] for r in idx), max(result_words[r][2] for r in idx)))
        out.append(row)
    return out


# --- whisper alignment (heavy subprocess only) -----------------------------------------


def _preload_cuda12_libs() -> None:
    """ctranslate2 4.x is built for CUDA 12; torch ships CUDA 13. Load the cu12 cuBLAS wheel."""
    import ctypes

    try:
        import nvidia.cublas as cublas  # nvidia-cublas-cu12
    except ImportError:
        return
    for base in cublas.__path__:
        for name in ("libcublasLt.so.12", "libcublas.so.12"):
            path = os.path.join(base, "lib", name)
            if os.path.exists(path):
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "cuda" in text and "memory" in text


def _whisper_align(samples: np.ndarray, lines: list[str], model_name: str) -> tuple[list, str]:
    """Run stable-ts align on faster-whisper; returns (result words, device used)."""
    _preload_cuda12_libs()
    import ctranslate2
    import stable_whisper

    attempts: list[tuple[str, str]] = []
    if ctranslate2.get_cuda_device_count() > 0:
        attempts.append(("cuda", "int8_float16"))
    attempts.append(("cpu", "int8"))
    last: BaseException | None = None
    for device, compute_type in attempts:
        try:
            model = stable_whisper.load_faster_whisper(model_name, device=device, compute_type=compute_type)
            result = model.align(
                samples,
                "\n".join(lines),
                language=WHISPER_LANGUAGE,
                original_split=True,
                verbose=None,
            )
        except Exception as exc:  # noqa: BLE001 - OOM on a shared GPU falls back to CPU
            last = exc
            if device == "cuda" and _is_oom(exc):
                print(f"align: CUDA out of memory, retrying on CPU ({exc})", file=sys.stderr)
                continue
            raise
        if result is None:
            return [], device
        return [(w.word, float(w.start), float(w.end)) for w in result.all_words()], device
    raise RuntimeError(f"alignment failed: {last}")


def align_words(vocals_path: str, lines: list[str], model_name: str | None = None) -> dict[str, Any]:
    """Align caption lines to a vocals stem; falls back per line. Pure-Python entry."""
    samples = load_audio(vocals_path)
    duration = len(samples) / SAMPLE_RATE
    regions = speech_regions(samples)
    words_per_line = [line_words(line) for line in lines]
    model_name = model_name or whisper_model()
    error: str | None = None
    device: str | None = None
    aligned: list[list[Timing | None] | None] = [None] * len(lines)
    if any(words_per_line):
        try:
            result_words, device = _whisper_align(samples, lines, model_name)
            aligned = list(map_result_words(words_per_line, result_words))
        except Exception as exc:  # noqa: BLE001 - every line falls back
            error = f"{type(exc).__name__}: {exc}"
            print(f"align: {error}", file=sys.stderr)
    words, method = merge_alignment(words_per_line, aligned, regions, duration)
    return {
        "words": words,
        "method": method,
        "duration": round(duration, 3),
        "model": model_name,
        "device": device,
        "error": error,
    }


def run_align(args: dict[str, Any]) -> dict[str, Any]:
    """Heavy task `align`: {vocals: abs path, lines: [str]} -> align_words result."""
    return align_words(args["vocals"], list(args["lines"]), args.get("model"))


# --- ASS generation ----------------------------------------------------------------------

MAX_WORDS_PER_GROUP = 3
MAX_GROUP_CHARS = 14  # keep a 1-3 word group on one line at this font size
GROUP_GAP_S = 0.35  # a pause this long starts a new group
GROUP_HOLD_S = 0.25  # how long the last group lingers after its last word
_BREAK_AFTER = re.compile(r"[.,!?;:\u2026\u2014-]$")

PLAY_RES_X = 768
PLAY_RES_Y = 1344
FONT_SIZE = 72
OUTLINE = 6
SHADOW = 3
MARGIN_V = 400  # bottom margin: lower-middle, clear of the Shorts UI
COLOR_BASE = "&H00FFFFFF"  # white (ASS is &HAABBGGRR)
COLOR_ACTIVE = "&H0000E6FF"  # bright yellow #FFE600
POP_SCALE = 118
POP_MS = 110


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float
    line: int


def group_words(words: Sequence[CaptionWord]) -> list[list[CaptionWord]]:
    """Split one take's words into display groups of 1-3 words."""
    groups: list[list[CaptionWord]] = []
    cur: list[CaptionWord] = []
    for w in words:
        if cur:
            prev = cur[-1]
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if (
                len(cur) >= MAX_WORDS_PER_GROUP
                or w.line != prev.line
                or w.start - prev.end > GROUP_GAP_S
                or _BREAK_AFTER.search(prev.text)
                or chars > MAX_GROUP_CHARS
            ):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


@dataclass
class TakeCaptions:
    """One take on the assembled timeline: its words and where it lands."""

    words: list[dict[str, Any]]  # {word, start, end, line}, take-relative
    trim_in: float
    trim_out: float
    offset: float  # timeline start of this take (sum of previous trimmed durations)


def timeline_words(take: TakeCaptions) -> list[CaptionWord]:
    """Words inside the take's trims, clipped and shifted onto the timeline."""
    out: list[CaptionWord] = []
    for w in take.words:
        s, e = float(w["start"]), float(w["end"])
        inside = take.trim_in <= s < take.trim_out if e <= s else (e > take.trim_in and s < take.trim_out)
        if not inside:
            continue
        s, e = max(s, take.trim_in), min(e, take.trim_out)
        shift = take.offset - take.trim_in
        out.append(
            CaptionWord(str(w["word"]), round(s + shift, 3), round(e + shift, 3), int(w.get("line", 0)))
        )
    return out


def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").upper()


def _group_text(group: Sequence[CaptionWord], active: int) -> str:
    parts = []
    for k, w in enumerate(group):
        t = _ass_escape(w.text)
        if k == active:
            pop = (
                f"{{\\1c{COLOR_ACTIVE}&\\fscx{POP_SCALE}\\fscy{POP_SCALE}\\t(0,{POP_MS},\\fscx100\\fscy100)}}"
            )
            parts.append(f"{pop}{t}{{\\1c{COLOR_BASE}&\\fscx100\\fscy100}}")
        else:
            parts.append(t)
    return " ".join(parts)


def caption_events(takes: Sequence[TakeCaptions]) -> list[tuple[float, float, str]]:
    """(start, end, ASS text) events: one per active word, grouped 1-3 words."""
    events: list[tuple[float, float, str]] = []
    for take in takes:
        seg_end = take.offset + (take.trim_out - take.trim_in)
        groups = group_words(timeline_words(take))
        for gi, group in enumerate(groups):
            nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else seg_end
            g_end = min(group[-1].end + GROUP_HOLD_S, nxt, seg_end)
            for k, w in enumerate(group):
                start = group[0].start if k == 0 else w.start
                end = group[k + 1].start if k + 1 < len(group) else g_end
                if end - start < 0.01:
                    end = start + 0.01
                events.append((start, end, _group_text(group, k)))
    return events


def build_ass(takes: Sequence[TakeCaptions], *, font: str | None = None) -> str:
    """Full ASS script for the assembled timeline (PlayRes matches the 768x1344 output)."""
    font = font or CAPTION_FONT
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, \
MarginR, MarginV, Encoding
Style: Shorts,{font},{FONT_SIZE},{COLOR_BASE},{COLOR_ACTIVE},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,\
{OUTLINE},{SHADOW},2,40,40,{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [
        f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Shorts,,0,0,0,,{text}"
        for s, e, text in caption_events(takes)
    ]
    return header + "\n".join(lines) + ("\n" if lines else "")
