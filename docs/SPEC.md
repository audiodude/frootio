# frootio — spec

A local web app for planning, writing, generating, and assembling short AI video episodes with a recurring cast.

## Glossary

| Term | Meaning |
|---|---|
| **Series** | Top-level show (e.g. *Kiwi and Lemon*). Owns episodes and a music library. |
| **Character** | A reusable cast member: one main ref image, a description, a voice sample, and aliases (the script speaker tags that resolve to it). Global library; a character can be cast into one or more series. Sharing is possible but not required. |
| **Location** | Named set with a text description and **optional** 1–3 ref images. Global library, same sharing rules as characters. |
| **Episode** | One video, targeting 30–45s (warned, not enforced). Has script versions, snippets, a default location, a take-count setting, assembly settings, and a spend total. |
| **Snippet** | One prompt to the video model, i.e. one fal call and **at most 15s**. It can contain several shots (multi-shot prompt). Snippets are spliced together in assembly. A snippet is either active (on the board) or archived; archived snippets keep their takes. |
| **Take** | One generation result for a snippet. Every take is kept (it can be hidden, never deleted), and one per snippet is *selected*. |

## Scope

frootio covers the whole pipeline: script → snippets → takes → assembly → final MP4. No external video editor is needed for the final cut.

- Runs locally for a single user. No auth. The server binds to `127.0.0.1` by default; `FROOTIO_HOST` overrides it (e.g. `0.0.0.0` for LAN use, at the cost of anyone on the network being able to spend fal credit). To reach it from another machine without exposing it, forward the port over SSH (`ssh -L`).
- Nothing is hard-deleted from the UI, and the app never deletes media files. Takes are hidden; snippets, characters and locations are archived.
- Output is **9:16 vertical** only, at **768p**, and every snippet uses both. There is no resolution or aspect picker.

## Stack

- **Backend:** Python, FastAPI, managed with `uv`. Handles fal calls, BandIt v2 stem separation, faster-whisper with stable-ts (forced alignment), ffmpeg (with libass), and SQLite.
- **Frontend:** React + Vite SPA. Includes the script editor, snippet board, take picker, trim UI, voice clipper, and caption preview.
- **Jobs:** a SQLite `jobs` table plus asyncio workers that run inside the FastAPI process. The fal `request_id` is saved when a job is submitted, so after a restart the worker resumes polling instead of submitting (and paying) again. Live status reaches the UI over SSE. There is no Redis, Celery, or separate worker.
  - GPU/CPU-heavy work (stem separation, caption alignment) runs in a subprocess, one job at a time, so it never blocks the event loop and N parallel takes don't load N separation models onto the GPU at once.
- **Keys:** `FAL_KEY` and `OPENROUTER_API_KEY` are read from the process environment. The launcher (`run.sh`) first sources `./frootio.env` from the repo root if it exists; that file is gitignored, and `frootio.env.example` shows its contents.

## Storage

The storage model is hybrid:

- **Media tree** (plain files, browsable in any file manager): `~/frootio/media/`. Override it with `FROOTIO_MEDIA_ROOT`.
  ```
  characters/<slug>/images/<sha12>.<ext>          # main, extras and generated candidates; the main image is a DB pointer
  characters/<slug>/voice/<sha12>.wav
  locations/<slug>/images/<sha12>.<ext>
  series/<slug>/music/<track>.mp3
  series/<slug>/episodes/<n>/snippets/<snippet-id>/takes/<take-id>.mp4
  series/<slug>/episodes/<n>/snippets/<snippet-id>/takes/<take-id>.vocals.wav
  series/<slug>/episodes/<n>/snippets/<snippet-id>/takes/<take-id>.fx.wav
  series/<slug>/episodes/<n>/snippets/<snippet-id>/takes/<take-id>.poster.jpg
  series/<slug>/episodes/<n>/snippets/<snippet-id>/start-frames/<sha12>.png
  series/<slug>/episodes/<n>/snippets/<snippet-id>/overdubs/<sha12>.wav   # fix-in-post lines
  series/<slug>/episodes/<n>/preview.mp4
  series/<slug>/episodes/<n>/exports/<timestamp>.mp4
  .frootio-backup/frootio-<date>.db
  ```
  Snippet folders use the snippet's ID, not its position, because order changes and re-splits archive snippets.
- **Images and voice samples are content-addressed** (named by the first 12 hex characters of their SHA-256) and never overwritten. Changing a character's main image or voice sample just moves the DB pointer, so every take's stored ref list still points at the exact files that were sent.
- **SQLite DB** lives outside the media tree at `~/.local/share/frootio/frootio.db` (override with `FROOTIO_DB`). The media tree may sit in a sync folder, and a live SQLite file inside a sync folder risks corruption. It holds the source of truth for metadata: series, characters, locations, episodes, script versions, snippets, takes (the five prompt parts and their flags, the joined prompt, seed, ref list as local path + hash + uploaded URL, start frame, computed cost, fal request id, status), jobs, and spend.
- **Backups:** on startup and then every 24h while running, the app writes a SQLite online backup to `<media root>/.frootio-backup/frootio-<date>.db` (written to a temp file, then renamed) and keeps the newest 14. This puts script versions and all metadata in the media tree (and any sync folder it lives in) without ever syncing the live DB.

## Characters & locations

- **Character fields:** name, slug, description (used in the refs legend), a **single main ref image** (the only image sent to the video model), extra images (library only), a voice sample (**2–5s**), and **aliases** (e.g. Plum Prentice with aliases Plum, Ms. Plum and Landlady). Captions never show speaker names, so there is no caption name field.
- **Voice sample length:** 2–5s, enforced on save. An upload or generated clip longer than 5s opens the voice clipper so you pick the range. Three speakers × 5s always fits fal's 15s combined audio limit.
- **Aliases:** the speaker tags that resolve to this character, matched case-insensitively against `[Speaker]` tags. The character's name always matches. Two characters cast in the same series can't share an alias; that's a validation error.
- **Voice sample sources:**
  1. Upload a file.
  2. Clip a time range from any take's separated vocals stem.
  3. Generate it with Chatterbox HD on fal (`resemble-ai/chatterboxhd/text-to-speech`), from one of its preset voices and a test line.

  A character with no sample is described by text only in the prompt.
- **Ref image generation:** nano-banana on fal: `fal-ai/nano-banana` from a prompt alone, or `fal-ai/nano-banana/edit` when input images are given. For example, an existing character can serve as a style anchor so new fruits match the house look. It produces N candidates, and you set one as the main image. Uploads (e.g. Midjourney output) are also supported. There is no LoRA training.
- **Locations:** a description plus optional 1–3 ref images, from the same generate/upload flow.
- **Archiving:** characters and locations can be archived. Archived ones disappear from pickers and casting but still resolve for existing snippets and takes, and can be unarchived.

## Scripts

- **Canonical grammar** (the only form the app parses as dialogue):
  - Dialogue: `[Speaker]: line`, one line per utterance. `Speaker` must resolve to a character name or alias.
  - Delivery directions: `*direction*` inside the dialogue line only.
  - Everything else (prose action, `<shot notes>`, scene headings, `[PHOTO: …]`-style brackets, blank lines, `-----`) is prose.
- **Lint:** the editor shows a soft warning (no auto-fix) on lines that look like dialogue but aren't canonical (e.g. `Kiwi: …`, `Ms. Plum [softly]: …`, `[Lemon] [still crying]: …`), and on `[Speaker]` tags that don't resolve to a character.
- Scripts pasted in from elsewhere that don't use the canonical form mostly parse as prose until you fix them by hand or use Level 2.
- Scripts are versioned. Saving is explicit (Save button or Ctrl+S), and each save that changes the text creates a new version. There is no autosave; navigating away with unsaved changes asks for confirmation.
- **The script and the snippet prompts are both editable, and neither syncs to the other.** Once a split has run, editing the script does not change the snippets, and editing a snippet does not change the script. You keep them consistent yourself.

## Splitting into snippets

You choose the LLM level each time you click **Split**:

- **Level 0 (no LLM):**
  - Packs consecutive script lines greedily into snippets of up to 15s. The length estimate is about 2.5 words/s of dialogue, plus about 1s per `*direction*`, plus about 1s per non-blank prose line.
  - Starts a new snippet when the next line would push the snippet past 15s or add a 4th speaker. `-----` is a hard break. Blank lines are not breaks, and there is no location detection; you set locations per snippet.
  - A single line estimated over 15s gets its own snippet at 15s, with a warning on the snippet.
  - Copies the lines into the snippet body verbatim, and stores them as the snippet's script slice (used for captions).
  - Fills in each snippet's fields: **cast** = the resolved speakers in the slice (non-speaking characters are added by hand); **location** = the episode's default location; **target duration** = the length estimate rounded up to whole seconds and clamped to 5–15s.
  - Archives the current snippets and puts the new ones on the board.
- **Level 2 (OpenRouter, model configurable):**
  - Gets the script, the cast with descriptions, the locations, and the constraints: **max-length packing** (fill toward 15s), at most 15s and at most 3 speakers per snippet, and a 30–45s total target. It is told to keep every line of dialogue; it never cuts material to hit the target.
  - Returns snippets, each with a script slice, location, cast, and a timestamped multi-shot body.
  - Shows a preview. You accept or reject it. Accepting archives the current snippets and puts the new ones on the board.
- **Episode length:** both levels show the estimated total length after a split, with a warning when it falls outside 30–45s. Fixing a long script is your job, in the script.
- **Per-snippet L2:** "Rewrite body with LLM" on a single snippet. The split is left unchanged.

### Archiving and manual edits

- A split never hard-deletes anything. Archived snippets keep their takes, media, and spend.
- An **Archived** drawer lists archived snippets. You can restore one (it goes to the end of the board) or copy its prompt into a new snippet.
- On the board you can add a blank snippet, drag to reorder, and delete. Delete means archive.
- Reordering marks a snippet with "continue from previous" stale if its predecessor changed.

## Snippet prompt (structured parts)

The exact prompt sent to fal is built from these parts:

1. **Series style preamble** (default from the series).
2. **Refs legend.** It is generated from the cast and location, e.g. `Image 1 is Kiwi Kowalski: <desc>. Image 2 is …. Audio 1 is Kiwi Kowalski's voice.` The order is character main refs first, then location refs. There is one audio entry per cast member that has a voice sample, at most 3. Generate is blocked if the ref list exceeds fal's limits (9 images, 3 audio clips, 12 files in total).
3. **Location text.**
4. **Body:** free text, a timestamped multi-shot sequence with the dialogue.
5. **Fixed tail:** `No music. Dialogue, foley and ambience only.`

Each part is shown separately and can be edited per snippet. The preview shows the joined string and its character count against fal's 50,000-character prompt limit (`maxLength`). `prompt_expansion_mode` is always `disabled`.

### Inherited vs. overridden parts

- The derived parts (style preamble, refs legend, location text) are each either **inherited** or **overridden**.
  - **Inherited:** rebuilt from its source (series preamble; cast and character descriptions; location description) whenever the preview is shown or a take is generated.
  - **Overridden:** your text, frozen. Editing a derived part makes it overridden. **Reset to inherited** switches it back.
  - An overridden part whose source has changed since you overrode it shows a **source changed** badge.
- The body and the fixed tail are always per-snippet text.
- The ref list (images and audio) is always built from the current cast and location, even when the legend is overridden. The badge is how you notice an overridden legend that no longer matches the refs.
- **Every take stores all five parts, each part's inherited/overridden flag, the exact joined prompt string, and its ref list.** Loading a take's prompt back into the editor restores all five parts **as overridden**, so the editor reproduces exactly what was sent. Use Reset to inherited per part to pick up current sources.

### Continuity

Each snippet has a **"continue from previous"** toggle, off by default and disabled on the first snippet. When it's on:

- The app extracts the frame at the previous snippet's selected take's **trim-out point** (not its last frame, since H3 overshoots and assembly trims that away), saves it under `start-frames/`, and passes it as `image_url`, alongside the refs. Each take records which start frame it used (source take + trim-out time).
- The snippet is **stale** when its selected take's start frame differs from the frame it would get now. That happens when the previous snippet's selected take changes, its trim-out changes, or reordering gives it a different predecessor.
- Staleness doesn't cascade: a stale snippet's own selected take hasn't changed, so the snippet after it isn't affected until you pick a new take.
- Stale is a badge only; it doesn't block Generate (new takes use the current frame).
- If the previous snippet has no selected take, Generate is blocked with a message saying so.

## Generation

- **Model:** `minimax/h3-max/reference-to-video` (fal), for every snippet. H3 Max Turbo was rejected because it has no reference-to-video mode.
- **Fixed params:** `resolution: 768P`, `aspect_ratio: 9:16`, `duration` = the snippet's target length (5–15s), and `prompt_expansion_mode: disabled`. The seed is random unless one is pinned.
- **Takes per click:** N defaults to 1. It's configurable per episode and can be overridden on each click, and the N takes run in parallel. With a pinned seed, the N takes use `seed`, `seed + 1`, …, `seed + N − 1`. Each take stores the seed fal reports back.
- **Ref uploads:** on each Generate click, every ref file is uploaded to fal storage once and the URLs are shared by that click's N takes. URLs are not reused across clicks, so their expiry never matters.
- **Pricing:** $0.08/s at 768p, billed on the *requested* duration. Each call includes 4,096 reference tokens, and usage beyond that costs $0.02 per 1,000 tokens (prorated). The Generate button shows the estimated cost before you click.
- **Cost is computed, not returned.** fal's response has no cost field, so each take's cost is computed from fal's published formula:
  - `cost = duration × $0.08 + max(0, ref_tokens − 4,096) × $0.02 / 1,000`
  - Image tokens depend on aspect ratio: 1:1 = 1,024, 4:3 or 3:4 = 1,376, 16:9 or 9:16 = 1,824, 5:2 or 2:5 = 2,560. Other shapes need fal's full table. Reference audio is about 80 tokens/s.
- **Other paid calls:** nano-banana and Chatterbox HD costs are computed from fixed per-call rates (kept in config, copied from fal's pricing pages). OpenRouter costs come from the usage cost in its response. These are logged to the spend table against the character, location or episode they were for.
- **Spend tracking:** costs are totalled per episode and per series.
- **Failsafe cap:** **$20 per episode** by default (configurable). A Generate is blocked if actual video spend plus in-flight estimates plus the new estimate would exceed the cap. It's a hard stop, not a budgeting feature. Only video takes count toward the cap.

### Failures

- **fal returns an error** (validation, safety checker, model failure): the take is marked `failed` with the error message, its cost is $0, and its in-flight estimate is released. **Retry** creates a new take from the failed take's stored parameters.
- **Submit fails before a `request_id` comes back:** the take is marked `failed` and is never resubmitted automatically, so a request that did reach fal can't be paid for twice.
- **Slow jobs:** the worker polls with backoff and has no automatic timeout. A take queued or running for more than 30 minutes shows as slow, with a **Cancel** button that cancels the fal request. A cancelled take costs $0.
- **Download or post-processing fails:** the result is fetched again by `request_id` (up to 3 tries), then the take is marked `download_failed` or `processing_failed` with a **Retry** button that doesn't make a new fal call. The take's cost still counts, because the video was generated.
- **Takes:** any take except the selected one can be hidden from the list; a "show hidden" toggle brings them back.

## Post-processing per take

These run automatically on download:

- **Stem separation (BandIt v2):** splits each take's audio into three stems, speech, music and effects, using [BandIt v2](https://github.com/kwatcharasupat/bandit-v2) (code Apache-2.0; weights CC BY-SA 4.0 on [Zenodo](https://zenodo.org/records/12701995)) with the `checkpoint-multi.ckpt` checkpoint, chosen in the [stem-separation spike](spike-bandit.md). Speech is saved as `vocals.wav`, effects (foley and ambience) as `fx.wav`, and the music stem is discarded. This removes any music H3 adds despite the prompt while keeping foley and ambience. It runs on every take because voice samples can be clipped from any take's vocals.
  - Demucs can't do this: its non-vocal stems mix music, foley and ambience together.
  - BandIt v2 is research code, not a pip package. The app vendors only its model code (`src/models/bandit/` and `src/models/base.py`, which depend on just `torch` and `torchaudio`), loads a Zenodo checkpoint, and runs its own chunked inference. None of the repo's training dependencies (ray, Lightning, Hydra, its pinned torch 2.0) are used.
- **Poster frame:** one still per take for the takes list.

Caption alignment runs only for selected takes, when the assembly screen opens, and is cached per take and script slice. It uses stable-ts `align()` on a faster-whisper model to align the snippet's caption text to the vocals stem. If alignment fails, or returns a line whose words have near-zero total duration (typical when H3 ad-libs), that line's words are spread evenly across the speech regions of the vocals stem.

## Assembly

- **Order:** the selected takes, in snippet order, each with a trim in and out point (H3 can overshoot the requested length by up to about 0.7s).
- **Audio:** the vocals and fx stems from each take, plus one episode music track from the series library. The track has a start offset and a volume, and is automatically ducked under dialogue (sidechained on the vocals stems). The fx stems play at a single episode-level fx volume.
- **Captions:**
  - **Text:** the dialogue lines of the snippet's **script slice**, with `[Speaker]:` and `*directions*` removed, **force-aligned** to the take's vocals. If H3 ad-libs or garbles a line, the captions show the script anyway. That's intentional and part of the genre. The script slice is editable in the snippet side panel (manually added snippets start with an empty one).
  - **Style:** Shorts-style motion graphics. 1–3 words at a time, bold and outlined, in the lower-middle safe area. All in one color, with words popping in bright yellow (scale/pop animation).
  - **Rendering:** ASS subtitles burned in with ffmpeg/libass. Captions can be turned off at export.
- **Strip all audio (voice + fx):** a per-take toggle that drops that take's own audio (vocals and fx stems) from the preview and export. It's meant for scenes that should be silent but where H3 added mumbling (e.g. a texting scene). The episode music keeps playing over the take without ducking, since there is no dialogue to duck under. Captions still follow the snippet's script slice. Fix-in-post lines (below) still play over a stripped take.
- **Preview and output:** both are rendered by ffmpeg with the same filter graph (trims, vocals, fx, fix-in-post lines, ducked music, burned-in captions). **Preview** renders a fast, lower-quality `preview.mp4` and reuses it until the assembly settings change. **Export** renders the final **9:16 768p MP4** to `exports/`. The trim UI plays each take's own MP4 in the browser; there is no live composition in the browser.

### Fix-in-post lines

A take can carry single lines of dialogue generated after the fact, to add a missing line or replace a garbled one:

- **Generation:** you pick a character, type one line (at most 300 characters) and generate it at the current playhead. The line is spoken by Chatterbox HD on fal (`resemble-ai/chatterboxhd/text-to-speech`), cloning the character's current voice sample (sent as the reference audio). A character without a voice sample can't be used. It runs as a resumable job like the other paid calls, and its cost is logged as TTS spend against the episode and character. The audio is stored content-addressed under the snippet's `overdubs/` folder.
- **Placement:** each line has a start time in the take's own (untrimmed) time and a volume (0–2×). Both can be nudged after generation; the line can be auditioned over the take and removed (archived).
- **Mixing:** lines are mixed into the take's vocals, so the episode music ducks under them and caption alignment runs against the vocals plus the lines (without the take's own vocals if the take is stripped). The caption text still comes from the snippet's script slice, so a new line must also be added there to be captioned.
- **Replace voice (punch-in):** a per-line toggle that silences the take's own vocals stem for the line's span, with 50 ms fades either side so the cut doesn't click. The fx stem (foley, ambience) keeps playing. The browser audition can only mute the whole take during the line, fx included; the preview and export silence just the voice. To drop all of a take's audio instead, use Strip all audio.

## UI

The **episode workspace** is where nearly all the work happens. Everything else (series, characters, locations, music library) is a plain list-and-edit page.

### Episode workspace

- **Script and snippets side by side.** The script editor sits on the left and the snippet list on the right. Because the two don't sync, keeping them in view together is how you keep them consistent. The script editor shows the grammar lint warnings. The snippet list supports add, reorder, and delete (archive), and has the Archived drawer.
- **Side panel for the selected snippet.** Selecting a snippet opens a panel that holds:
  - the five prompt parts, each editable, with each derived part's inherited/overridden state, Reset to inherited, and the source changed badge; plus the joined preview and its character count against 50,000;
  - cast, location, the "continue from previous" toggle (with the stale badge), the target duration, and the editable script slice;
  - the Generate button with the take count (N) and the estimated cost.
- **Takes as a list with one player.** The panel lists the snippet's takes (status, seed, cost, time, selected marker). Clicking a take plays it in a single player. Selecting a take shows the exact prompt it was generated from, and that prompt can be loaded back into the parts for editing. There is no grid of autoplaying videos, so the backend only needs a poster frame per take, not preview transcodes.

### Assembly screen

A separate screen per episode. It covers:

- the selected takes in snippet order, with trim in/out on each;
- the music track picker, start offset, volume, and duck amount, and the fx volume;
- per take: the Strip all audio toggle and the take's fix-in-post lines (generate at playhead, start time, volume, replace voice, audition, remove);
- caption preview and the on/off toggle;
- preview playback and export.

Waveforms (peaks computed from each vocals stem) and caption alignment are generated for the selected takes when you open assembly, not for every take as it downloads.

## Out of scope

The following are not part of this design:

- Multi-user use, auth, or hosting.
- Keyframe/composite pipelines, TTS → lipsync chains, and LoRA training.
- 16:9 output or other aspect ratios.
- A full timeline editor, transitions, overlays, or title cards.
- Generating music.
- Importing finished clips or projects from other tools.

## Assumptions and outcomes

- **BandIt v2 separation quality: verified.** A spike on 15 real H3 takes checked that the speech stem is good enough for dialogue and voice-sample clipping and that H3-added music leaves the fx stem. BandIt v2 passed, `checkpoint-multi.ckpt` was chosen over `checkpoint-eng.ckpt` (eng attenuated shouted dialogue in one stress case), and the Demucs two-stem fallback was not needed. See [`spike-bandit.md`](spike-bandit.md).
- **Still assumed:** fal doesn't bill requests that end in an error or are cancelled before running (failed and cancelled takes are logged at $0).
- **Still assumed:** stable-ts `align()` with a faster-whisper model handles short, expressive cartoon dialogue well enough for 1–3-word captions. Lines it can't place fall back to even spreading over the speech regions (see Post-processing per take).
