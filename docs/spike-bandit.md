# Spike: BandIt v2 stem separation on H3 takes

Date: 2026-10-11. This spike checks the SPEC assumption "BandIt v2 separates H3 output cleanly enough" and picks the checkpoint.

**Result: BandIt v2 passes, so the Demucs fallback is not needed. Checkpoint: `checkpoint-multi.ckpt`.**

- On all 15 real clips, the speech stem transcribes word-for-word the same as the original mix.
- Music H3 added goes almost entirely to the (discarded) music stem.
- The fx stem keeps foley and ambience: footsteps, rain, zippers, paper, whooshes.
- A 4–11 s take separates in about 5–9 s on the RTX 3080 Ti, with about 1.1 GB of VRAM.

## Implementation (`backend/frootio/stems/`)

- `bandit/`: the vendored model code from [kwatcharasupat/bandit-v2](https://github.com/kwatcharasupat/bandit-v2) at commit `d5563d9` (Apache-2.0). `LICENSE` is upstream's; `NOTICE` lists every change. The changes are:
  - package-relative imports;
  - `nn.Module` base instead of Lightning;
  - librosa's `hz_to_midi`/`midi_to_hz` inlined as the same numpy formulas;
  - training-only `checkpoint_sequential` and `torch.compile(disable=True)` wrappers removed;
  - unused band-split specs and `film.py` dropped.

  The checkpoint loads with `strict=True`, so all 2,946 model tensors match.
- `separate(input_media, vocals_out, fx_out, *, device=None) -> {"sample_rate", "duration", "checkpoint"}`:
  1. ffmpeg decodes the first audio stream to **48 kHz stereo float32**. 48 kHz is the model rate. Mono input is upmixed.
  2. The model is mono (`in_channels=1`), so each channel is separated independently and the stems keep the take's stereo image.
  3. Chunked overlap-add inference: 8 s chunks (the training chunk length), 1 s hop (upstream's `chunked-tensor` inference config), and a periodic Hann window normalised by the summed window. Both ends are reflect-padded by one overlap, so every output sample gets full overlap.
  4. It writes **16-bit PCM stereo WAV at 48 kHz** for speech → `vocals_out` and sfx → `fx_out`. Each write goes to a `.part` file, which is then renamed. The music stem is discarded.
  5. Output length equals the decoded audio length, sample-exact.
- The model is cached per process for each `(checkpoint, device)` (`_MODELS`). The device is CUDA when available, otherwise CPU.
- The checkpoint downloads on first use from `https://zenodo.org/records/12701995/files/<name>?download=1` to `~/.cache/frootio/bandit/` and is MD5-verified against the Zenodo record. Each checkpoint is 447 MB.
- Model hyperparameters come from upstream `configs/models/bandit-mus64.yaml`, with stems `[speech, music, sfx]` and `fs=48000`. These are the same for every DnR v3 checkpoint (`expt/v3-48k-smad-*-w.yaml`).
- `REQUIREMENTS = ["torch==2.14.1", "torchaudio==2.11.0", "numpy==2.5.4"]` (see Deps). ffmpeg must be on `PATH`.

## Method

- **Material:** 15 existing clips from two earlier episodes (8 + 7), labelled A–O below. They are H3-style takes with dialogue, and some have H3-added music. Video is 834×1112, 720×1280 or 768×1344; audio is AAC stereo at 44.1 or 32 kHz, 4–11 s long. Each clip was run through both checkpoints with the same code as `separate()`.
- **Energy:** the RMS level (dBFS) of each stem, overall and per 1 s window. The reconstruction residual `mix − Σstems` came out between −18 and −53 dB relative to the mix.
- **Transcripts:** faster-whisper `large-v3` (CUDA, int8_float16, `language="en"`, beam 5) on the mix and on every stem. The table reports word error rate (WER) of the speech-stem transcript against the mix transcript. The reference is the mix transcript, not the script, because H3 ad-libs.
- **Music detection:** the AudioSet tagger `MIT/ast-finetuned-audioset-10-10-0.4593`, reporting the P(Music) label probability on the mix and on each stem.
- **Known-source tests** (there is no ground truth for the H3 clips):
  - **Synthetic mix:** clean voice (two clean dialogue lines, 8.7 s together) plus a real music track (from 20 s in) at −10 dB and 0 dB music-to-voice. Speech quality is measured as SI-SDR against the clean voice. Music-only input checks where music goes.
  - **Injected music:** 4 real clips with that music added 10 dB below the clip's own level. The table reports the injected-music component of each stem: the projection of `stem(clip+music) − stem(clip)` onto the music, in dB relative to the music.
- **Machine:** an RTX 3080 Ti 12 GB that was **not idle during the spike**, so the timings are pessimistic and noisy. The fx stem is quiet on most clips because H3 output here is mostly dialogue.

## Numbers

### Real clips, both checkpoints (eng / multi)

| clip | dur s | runtime s | mix dB | speech dB | fx dB | music dB | WER speech vs mix | P(Music) mix → fx |
|---|---|---|---|---|---|---|---|---|
| A | 6.06 | 7.1 / 5.4 | −24.6 | −24.6 / −24.6 | −93.7 / −59.8 | −54.4 / −54.6 | 0 / 0 | 0.42 → 0.13* / 0.00 |
| B | 5.06 | 6.6 / 5.1 | −23.2 | −23.4 / −23.4 | −89.5 / −66.1 | −37.8 / −38.0 | 0 / 0 | 0.32 → 0.01 / 0.01 |
| C (screaming) | 8.06 | 6.9 / 7.7 | −27.0 | −30.7 / −31.4 | −35.5 / −36.0 | −35.3 / −34.5 | 0 / 0 | 0.55 → 0.00 / 0.01 |
| D (no dialogue) | 4.06 | 5.2 / 5.6 | −22.1 | −102.9 / −105.5 | −27.5 / −29.6 | −25.6 / −24.7 | n/a† | 0.70 → 0.01 / 0.01 |
| E | 4.06 | 5.2 / 5.6 | −18.6 | −18.7 / −18.7 | −103.2 / −63.5 | −35.5 / −36.0 | 0 / 0 | 0.30 → 0.10* / 0.01 |
| F | 11.05 | 8.0 / 8.8 | −27.1 | −28.4 / −28.3 | −34.0 / −34.6 | −43.2 / −42.8 | 0 / 0 | 0.39 → 0.00 / 0.00 |
| G | 4.06 | 5.6 / 5.6 | −20.2 | −20.2 / −20.2 | −44.6 / −44.4 | −44.1 / −43.9 | 0 / 0 | 0.59 → 0.01 / 0.00 |
| H | 6.06 | 6.5 / 6.4 | −19.3 | −19.3 / −19.3 | −104.8 / −68.6 | −43.8 / −44.3 | 0 / 0 | 0.74 → 0.10* / 0.07 |
| I | 10.12 | 8.1 / 8.2 | −19.0 | −19.0 / −19.0 | −78.6 / −71.9 | −74.6 / −107.4 | 0 / 0 | 0.00 → 0.03 / 0.29* |
| J | 4.06 | 5.3 / 5.3 | −20.8 | −20.8 / −20.9 | −56.5 / −55.7 | −60.4 / −63.5 | 0 / 0 | 0.01 → 0.00 / 0.00 |
| K | 9.06 | 7.1 / 7.7 | −26.2 | −26.2 / −26.2 | −58.9 / −58.9 | −119 / −111 | 0 / 0 | 0.00 → 0.01 / 0.01 |
| L | 4.06 | 3.8 / 5.6 | −22.7 | −22.8 / −22.8 | −73.6 / −99.3 | −40.7 / −40.7 | 0 / 0 | 0.40 → 0.08 / 0.10* |
| M | 7.06 | 5.1 / 7.5 | −22.3 | −22.3 / −22.3 | −99.8 / −103.9 | −84.7 / −76.6 | 0 / 0 | 0.01 → 0.03 / 0.10* |
| N | 6.06 | 4.7 / 7.0 | −22.0 | −22.0 / −22.0 | −54.4 / −52.6 | −57.1 / −62.9 | 0 / 0 | 0.01 → 0.00 / 0.00 |
| O | 10.05 | 5.8 / 9.1 | −20.8 | −21.1 / −21.1 | −57.9 / −56.8 | −32.1 / −32.2 | 0 / 0 | 0.76 → 0.09 / 0.13 |

\* The fx stem at these points is at or below about −70 dBFS. That is inaudible, and the tagger output there is noise-level guessing ("Silence" was the top label).
† No dialogue: Whisper hallucinates "Thank you for watching!" on the mix and "Thank you." on the (silent, −103 dB) speech stem.

- **Speech:** the speech-stem transcripts matched the mix transcripts exactly in 14 of 14 dialogue clips for both checkpoints.
  - The tagger's P(Speech) on the speech stem was ≥ the mix's (within 0.01) in 13 of 15 clips for eng and 12 of 15 for multi. The exceptions were clip O for both (0.86 → 0.59, transcript still identical), clip N for both (0.91 → 0.90 / 0.89), and clip F for multi (0.68 → 0.56).
  - On the music stem, Whisper produced only its usual silence hallucinations ("Thank you for watching", "welcome to my channel"). The exception was clip C, where the stem leaks "Oh"/"Oh no!" (the scream, see below).
- **Music removal from fx:** wherever the mix had audible music (P(Music) 0.30–0.76: clips A–H, L, O), the fx stem drops to P(Music) ≤ 0.13, and the music stem holds it (0.23–0.77).
- **Foley kept in fx:** the fx stem's top tags were:
  - clip D: rain, footsteps, clip-clop;
  - clips J and N: zipper, crunch, scrape;
  - clip K: tearing, scissors (paper);
  - clip O: whoosh, timpani hit;
  - clip F: sigh, gasp.
- **Non-verbal vocalisations go to fx:** gasps, sighs, groans and screams land in fx, not speech. On clip C the fx stem tags as Speech 0.72–0.76, and Whisper reads "Oh no!" from it. That is the second, screamed "Oh no!", which is still also present in the speech stem.

### Known-source tests

Synthetic clean voice + real music (speech SI-SDR vs clean voice; the mix itself scores 10.0 dB at −10 dB music and 0.0 dB at 0 dB music):

| | eng | multi |
|---|---|---|
| speech SI-SDR, music −10 dB | 27.0 dB | 26.6 dB |
| speech SI-SDR, music 0 dB | 21.0 dB | 20.6 dB |
| music-only input → fx (re music) | −49.5 dB | **−62.5 dB** |
| music-only input → speech (re music) | −88.4 dB | −88.4 dB |
| music-only input → music stem | −0.0 dB | −0.0 dB |

Music injected 10 dB below the level of real clips. Each cell is the injected-music component left in the stem (eng / multi):

| clip | in speech | in fx |
|---|---|---|
| I | −31.1 / −30.3 dB | −81.1 / −81.6 dB |
| O | −31.4 / −30.0 dB | −29.4 / −24.8 dB |
| F | −31.6 / −30.7 dB | −27.1 / −25.1 dB |
| C (screaming) | −52.1 / −43.9 dB | −26.8 / −17.0 dB |

On clip C (a screaming clip) with injected music, **eng attenuated the actual dialogue**. Its speech stem fell from −30.7 to −43.7 dBFS (from second 4 on: −26 → −39 dB per second), and Whisper lost the final "Oh no!". **multi left the speech untouched** (−31.4 → −31.3 dBFS) and kept the full transcript. On the other three clips both checkpoints were unchanged within 0.1 dB, with identical transcripts.

### Checkpoint choice: `checkpoint-multi.ckpt`

The two checkpoints are close: same transcripts on real clips, and clean-voice SI-SDR within 0.4 dB.

- **For multi:** multi is the robust one for dialogue, which is what frootio needs most (captions and voice-sample clipping). In the one real-clip stress case, eng suppressed shouted dialogue and multi didn't. multi also leaks less music into fx on pure music (−62.5 vs −49.5 dB).
- **For eng:** eng left slightly less injected music in fx on the injected-music clips (2–10 dB less).

That residual music in fx sits 17–30 dB under the music level. It is further reduced by the episode-level fx volume and masked by the episode music track. Losing a line of dialogue cannot be fixed downstream.

### Inference settings, runtime, VRAM

- **Hop** (eng, batch 1, 4 primary clips). For hop 2 s and 4 s, the speech SI-SDR against the hop-1 s output was:

  | clip | hop 2 s | hop 4 s |
  |---|---|---|
  | I | 60.9 dB | 56.5 dB |
  | O | 59.8 dB | 52.8 dB |
  | F | 50.3 dB | 45.7 dB |
  | C (screaming) | **24.6 dB** | **13.0 dB** |

  Runtime for a 8–11 s clip was about 8–9 s at hop 1, 4–4.5 s at hop 2, and 2 s at hop 4. The default stays at **hop 1 s** (upstream's setting). The expressive clip C changes audibly with larger hops, and about 1× realtime is fine for a background job.
- **Runtime** (hop 1, batch 1, GPU not idle): **3.8–9.1 s per 4–11 s clip**, roughly 0.5–1.3× realtime. Model load is 1.7–2.2 s (447 MB checkpoint, once per process).
  - Acceptance run through `uv run --with …`: 7.2 s total, model load included, for the 10.05 s clip O.
- **VRAM:**
  - Batch 1: **1.08–1.12 GB peak allocated, 1.66 GB peak reserved**. The model weights are 143 MB.
  - Batch 2: 1.92 GB peak, about 30% faster (3.18 s vs 4.44 s at hop 2).
  - Batch 4 ran out of memory on the 12 GB card (which other processes were also using). The default is `BATCH_SIZE = 1`.
- **CPU fallback** (Ryzen 7 5800X, 8 torch threads, hop 1): **257 s for a 10.1 s clip**, about 25× realtime. It works, but it is only an emergency path.

## Known limits

- **Non-verbal vocalisations:**
  - Gasps, sighs, groans and parts of screams go to **fx**, not vocals. Voice samples clipped from vocals won't contain them. They will be heard in the fx mix at the fx volume.
  - The shouted/screamed second "Oh no!" in clip C is split between speech and fx.
- **Residual music in fx:** when H3 adds music under foley, some music remains in fx. With music 10 dB under the take, the residual was 17–30 dB below that music. When the take has no foley, fx is essentially silent.
- **CPU-only use is slow** (about 25× realtime). `jobs.run_heavy`'s single-slot semaphore keeps one separation on the GPU at a time. About 1.1 GB plus Whisper fits comfortably in 12 GB.
- `separate()` raises `RuntimeError` when the input has no audio stream. Callers should treat that as `processing_failed`.
- Model weights are **CC BY-SA 4.0** (Zenodo record). The code is Apache-2.0.

## Deps

- Runtime (validated): `torch==2.14.1`, `torchaudio==2.11.0`, `numpy==2.5.4`. These are the PyPI default wheels, CUDA 13.0 build, Python 3.12. `torchaudio` is used only for `Spectrogram`/`InverseSpectrogram` inside the model.
- System: `ffmpeg` on `PATH`.
- Not needed: librosa, Lightning, Hydra, ray (all removed by the vendoring).
- Acceptance command (from `backend/`):
  ```
  PYTHONPATH=. uv run -p 3.12 --with torch==2.14.1 --with torchaudio==2.11.0 --with numpy==2.5.4 \
    python -c 'from pathlib import Path; from frootio.stems import separate; print(separate(Path("<clip>.mp4"), Path("v.wav"), Path("fx.wav")))'
  ```
  Output: `{'sample_rate': 48000, 'duration': 10.054, 'checkpoint': 'checkpoint-multi.ckpt'}`. Both WAVs are 48 kHz stereo, 10.054 s long (the source container says 10.053991 s). Vocals −21.1 dBFS RMS, fx −56.8 dBFS RMS.
- Spike-only (in a throwaway venv, not app deps): `faster-whisper==1.2.1` (ctranslate2 4.8.2) and `transformers==5.19.0`. Two findings matter for caption alignment:
  - ctranslate2 4.8.2 is built against CUDA 12, but torch 2.14.1 ships cuBLAS 13. GPU Whisper fails with `libcublas.so.12 is not found` until `nvidia-cublas-cu12` is installed and its `nvidia/cublas/lib` directory is on `LD_LIBRARY_PATH`.
  - faster-whisper 1.2.1's own file decoder breaks with the installed PyAV (`open() got an unexpected keyword argument 'metadata_errors'`). Pass a 16 kHz float32 numpy array instead of a path.
