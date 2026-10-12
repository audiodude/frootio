# frootio

Local web app for planning, writing, generating and assembling short AI video episodes with a recurring cast: script → snippets → H3 takes → assembly → final 9:16 MP4. Design: [`docs/SPEC.md`](docs/SPEC.md). Stem-separation spike: [`docs/spike-bandit.md`](docs/spike-bandit.md).

Built with AI assistance (Claude).

## Requirements

- [`uv`](https://docs.astral.sh/uv/) (it installs the Python the backend needs)
- Node 20.19+ or 22.12+ (Vite 8) with `npm`
- `ffmpeg` built with libass, on `PATH`
- Optional: an NVIDIA GPU with CUDA. Stem separation and caption alignment use it when available and fall back to the (much slower) CPU otherwise.
- A [fal](https://fal.ai) API key (video, images, voices) and an [OpenRouter](https://openrouter.ai) API key (LLM split / body rewrite)

## Run

```sh
cp frootio.env.example frootio.env   # then fill in FAL_KEY and OPENROUTER_API_KEY
./run.sh
```

Then open http://127.0.0.1:8770/. `run.sh` sources `./frootio.env` if it exists (it's gitignored), rebuilds the frontend if its sources changed, and starts the FastAPI server. Instead of `frootio.env` you can export the variables in your shell. The BandIt checkpoint (~450 MB) is downloaded to `~/.cache/frootio/bandit/` on first use.

There's no auth. The server binds to `127.0.0.1` by default; keep it that way unless you trust everyone who can reach the port, since anyone who can reach it can spend your fal credit.

| Env var | Default | |
|---|---|---|
| `FAL_KEY` | | required for generation |
| `OPENROUTER_API_KEY` | | required for Level 2 split and body rewrite |
| `FROOTIO_HOST` | `127.0.0.1` | `0.0.0.0` exposes it, including fal spend, to the LAN |
| `FROOTIO_PORT` | `8770` | |
| `FROOTIO_MEDIA_ROOT` | `~/frootio/media` | media tree and DB backups (`.frootio-backup/`) |
| `FROOTIO_DB` | `~/.local/share/frootio/frootio.db` | kept outside the media root on purpose, so the media root can live in a sync folder |
| `FROOTIO_OPENROUTER_MODEL` | see `backend/frootio/config.py` | Level 2 split / body rewrite |

## Develop

```sh
cd backend && uv run pytest -q          # backend tests (no network)
cd frontend && npm run dev              # Vite dev server, proxies /api and /media to :8770
cd frontend && npm run build            # strict tsc + build
```

## Assembly notes

Each take card on the assembly screen has a **Strip all audio (voice + fx)** toggle. It drops that take's own audio from the preview and export, which is useful when H3 adds mumbling to a scene that should be silent, such as a texting scene. The episode music keeps playing over it, without ducking. Captions still follow the snippet's script slice.

**Fix-in-post lines:** each take card also lets you generate a single line of dialogue in a character's voice. The character's voice sample is cloned with Chatterbox HD on fal, at a fraction of a cent per line. Pick the character (say, Kiwi Kowalski), type the line and click **Generate at <playhead>**. A spinner shows while the line generates (about 2 minutes). Then nudge its start time and volume, audition it over the take, or remove it. Lines are mixed into the take's dialogue, so music ducks under them and caption alignment hears them. The take's script slice still sets the caption text, so add the line there too.

To **punch in** a replacement for a garbled line, tick **replace voice** on the new line. The take's own voice is silenced for the line's span, with 50ms fades, while foley and ambience keep playing. Line it up with the original using the start time. In the browser, Audition can only mute the whole take during the line, fx included; the preview and export silence just the voice. To replace all of a take's audio instead, use **Strip all audio**. Lines still play over a stripped take.

## Spend

The Generate button shows each H3 take's estimated cost before you click. Each take's cost is computed from fal's formula. Video spend per episode is hard-capped at $20 by default (editable per episode). Image, TTS and LLM costs are logged too and appear in the per-episode and per-series totals.

## License

Apache-2.0; see [`LICENSE`](LICENSE). The vendored BandIt v2 model code in `backend/frootio/stems/bandit/` is also Apache-2.0 (its own `LICENSE`/`NOTICE`); the BandIt checkpoint weights downloaded at runtime are CC BY-SA 4.0.
