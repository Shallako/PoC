# Shoulico -- local MVP

Story -> scenes -> engine-targeted prompts -> images -> narration script ->
narration audio -> export.

Image creation defaults to **Seedream 5.0 Pro** through the Renderful API.
Narration is spoken by **ElevenLabs Flash v2.5**, through the same API and the
same key. Scope is **images, narration script and narration audio** -- no video
assembly, no accounts, no billing layer.

Claude writes one narration line per scene (PRD FR-1201/1203/1206) and you review
and edit it before anything is spoken. Synthesis is a separate, confirmed step:
the script is yours to fix first, and speaking it costs money.

Speaking a line also **measures** it. Until then a duration is a word-count
estimate at 150 wpm, which runs about 30% long -- a 13-word line estimates 5.2s
and speaks in 4.2s. Every downstream step (captions, image timing, clip length)
depends on that number, so it comes off the delivered bytes, not a guess.

---

## Install

```powershell
cd C:\Users\shoul\Renderful\PoC
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

(The venv already exists if you're reading this on the machine it was built on.)

## Keys

| Key | Used for | Lookup order |
|---|---|---|
| Renderful | image rendering **and narration audio** | `RENDERFUL_API_KEY` -> `PoC\api_key.txt` -> `Renderful\api_key.txt` |
| Anthropic | segmentation, prompt writing, narration | `ANTHROPIC_API_KEY` -> `PoC\anthropic_key.txt` -> `Renderful\anthropic_key.txt` -> an `ant auth login` profile |

Both keys are present on this machine (`PoC\api_key.txt` and
`PoC\anthropic_key.txt`), so all four steps are live. Without an Anthropic key,
step 1 (segment) and the narration button stay disabled; everything else still
works, including hand-writing prompts and rendering. A key that is present but
does not start with `sk-ant-` is flagged in the UI rather than shown green.

Keys are read on demand, never logged, never sent to the browser, and never
written into a project file. The UI only ever sees a found/missing boolean.

## Run

```powershell
.\.venv\Scripts\python.exe run.py          # http://127.0.0.1:8765, opens a browser
.\.venv\Scripts\python.exe run.py --port 9000 --no-browser
```

Loopback only. Nothing here is hardened for a public interface.

## Run it in VS Code

Install the **Python** extension (Pylance comes with it), then open this folder --
`File > Open Folder...` on `C:\Users\shoul\Renderful\PoC`. Opening the parent
`Renderful\` folder instead breaks the interpreter and test discovery, both of
which are resolved relative to the workspace root.

**Pick the interpreter -- this is the step that bites.** `Ctrl+Shift+P` ->
*Python: Select Interpreter* -> `.\.venv\Scripts\python.exe`. Left on the system
Python, ▷ dies immediately with `uvicorn is not installed` (nothing in
`requirements.txt` is installed globally, only in `.venv`). This workspace pins
the venv in `.vscode\settings.json`:

```json
"python-envs.defaultEnvManager": "ms-python.python:venv",
"python.defaultInterpreterPath": "${workspaceFolder}\\.venv\\Scripts\\python.exe"
```

The newer Python Environments extension writes `defaultEnvManager` itself and can
reset it to `:system`; if run starts failing again, that key is the first thing to
check. Once the interpreter is right, every new integrated terminal activates the
venv, so a bare `python run.py` uses it. If the prompt shows no `(.venv)` prefix,
activate by hand:

```powershell
.\.venv\Scripts\Activate.ps1
```

(Blocked by execution policy? `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`,
or just keep calling `.\.venv\Scripts\python.exe` explicitly -- it needs no
activation.)

**Run and debug.** With `run.py` open, the ▷ button in the title bar starts the
server; the browser opens on its own after a second and Ctrl-C in the terminal
stops it. To debug with breakpoints and pass flags, create `.vscode/launch.json`
(`Run > Add Configuration...` -> *Python Debugger* -> *Python File*, then replace
its body):

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Shoulico server",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/run.py",
      "args": ["--port", "8765"],
      "console": "integratedTerminal",
      "justMyCode": false
    }
  ]
}
```

Breakpoints in `shoulico/` route handlers hit on the next request. Don't add
`--reload` to a debug run -- uvicorn's reloader spawns a child process the
debugger isn't attached to, so breakpoints silently stop firing.

`.vscode/` is gitignored, so a `launch.json` you add stays local; nothing in this
repo depends on it.

**Tests.** Open the Testing panel (the flask icon) -- pytest discovery is driven
by `pytest.ini`, so `tests/` is picked up once the interpreter is set. Only the
offline suite runs there: the live tests need `--live` on the command line and
are skipped otherwise, which is deliberate -- clicking *Run All Tests* must never
spend money.

---

## The five steps

1. **Story** -- paste up to 5,000 characters in any language, pick the image count
   and the engine. Engine parameters are rendered from the registry schema and
   validated *before* anything is submitted. Renderful bills a request it later
   rejects for a bad parameter, so an out-of-schema value has to fail locally, for
   free.
2. **Scenes & prompts** -- Claude returns ordered beats, one prompt body per beat,
   and one shared style block, in the language of the story. Everything is editable;
   the compiled prompt (what actually gets sent) is shown per scene. Narration is
   written and edited here too.
3. **Render** -- preview the batch and its cost, then confirm. Confirmation is
   mandatory: `POST /api/projects/{id}/render` without `confirm: true` is a 400.
   Three workers, live per-scene status, per-scene re-render from the gallery (the
   `?` beside that button says what it spends), and `POST .../cancel` stops the run
   and returns unfinished scenes to pending.
4. **Speak** -- pick a voice on step 2, preview the character count and its cost,
   then confirm. Same contract as rendering: `POST
   /api/projects/{id}/narration/speak` without `confirm: true` is a 400. One MP3
   per scene under the image's own stem, a player and the measured duration
   beside each line, and per-line "Speak" buttons for one-offs.
5. **Video** -- cut the finished MP4 on this machine with ffmpeg. Each scene holds
   the screen for exactly as long as its narration audio runs, with an optional
   Ken Burns zoom, and captions built from the same timeline. Nothing is uploaded
   and nothing is billed, so unlike rendering and speaking there is no
   confirmation gate. ffmpeg is optional: without it the panel says so and offers
   the install command, and captions still export.
6. **Export** -- flattened, sort-safe copies plus matching narration files and
   audio, the full voiceover, an `.srt`, a `.vtt`, a timing sheet, the assembled
   video if there is one, and a copy of `manifest.json`. `flatten: false` keeps
   the versioned filenames instead. `export/` is cleared first, so it never
   carries files from a previous run.

## What lands on disk

```
projects/<project-id>/
  project.json     story, language, style block, scenes, prompts, narration, params, spend
  manifest.json    one record per asset: engine, seed, params, exact prompt sent, cost
  images/          <project>_<NNN>_<slug>_v<VV>[_seed<SEED>].<jpg|png>
  narration/       <project>_<NNN>_<slug>.txt   and  <project>_full-voiceover.txt
  audio/           <project>_<NNN>_<slug>.mp3   -- the spoken line, same stem
  video/           <project>_<NNN>_<slug>_seg.mp4  -- one segment per scene
                   <project>_video.mp4              -- the finished cut
  export/          flattened <project>_<NNN>_<slug>.<jpg|png> + .txt + .mp3
                   + <project>_full-voiceover.txt + <project>_captions.srt/.vtt
                   + <project>_timing.csv + <project>_video.mp4 + manifest.json
engines.json       engine, voice and video registries -- edit to add a model
i18n/<code>.json   cached interface translations, one file per language
```

## Getting to a finished video

The three ways out of `export/`, cheapest first:

| Route | Cost per 12-scene story | Needs |
|---|---|---|
| Captions + timing sheet into CapCut | **$0.00** | nothing |
| Assemble the MP4 here (step 5) | **$0.00** | ffmpeg installed |
| Generated video clips per scene | $1.80 -- $3.60 | not built; see below |

`<project>_timing.csv` is the hand-off: one row per scene with its start, its
duration, whether that duration was measured or estimated, and the exact
narration text. An editor can place the whole cut from it without dragging a
single clip against a waveform.

Generated image-to-video clips are deliberately **not** built. Renderful is a
generation API, not an editing one: it returns N separate silent clips with no
narration and no way to join them, so buying motion does not remove the assembly
step, it adds cost on top of it. The cheapest usable model is 2--4x the entire
cost of the images and narration combined, and its clips are a fixed ~5s against
narration lines that commonly run 5--15s. A still holds any length for free.

---

## Behaviour worth knowing

**The story's language carries through.** Segmentation detects what language the
story is written in and stores it on the project (`language: {code, name,
native_name}`). Titles, beats, prompt bodies, the shared style block and the
narration script all come back in that language, so the author edits in the
language they wrote in. Two consequences worth knowing:

- Image engines are trained mostly on English text, so a prompt in another
  language can render worse than the same prompt in English. **Prompt language**
  on step 1 switches just the prompt bodies and the style block to English and
  leaves titles, beats and narration alone; it takes effect on the next segment.
- Filenames stay ASCII whatever the script: accents fold to their base letter
  (`Café à Montréal` -> `cafe-a-montreal`), and a title with no Latin form at all
  (Japanese, Arabic) falls back to `scene` -- the `_NNN_` ordinal is what keeps
  those unique and in order.

Narration length is estimated in words at 150 wpm, or in characters at 350 cpm for
scripts that do not space their words (Chinese, Japanese). That estimate is
pre-flight only, and it runs long -- a 13-word line estimates 5.2s and speaks in
4.2s. Once a line has been spoken the duration is read off the delivered MP3
(`shoulico.audio` parses the ID3v2 tag and MPEG frame headers directly, so there
is no ffmpeg dependency) and the scene carries `audio_measured: true`. A container
that cannot be parsed keeps the audio and falls back to the estimate rather than
failing a line you have already paid for.

**Narration audio.** Idempotent on the *text*, exactly as rendering is on the
prompt: edit one line and only that line is re-spoken; run again with nothing
changed and nothing is spent. Blank the language code and the voice follows the
story's own detected language, which is the point of segmenting in it. Video
models that advertise "native audio" are not used for this -- they invent
ambience and dialogue from the image prompt and cannot be handed an approved
script, so pictures move and TTS speaks.

**The interface follows the story.** Once a language is detected, the page posts
its own English strings to `POST /api/ui/strings` and redraws itself in that
language; right-to-left languages also flip `dir`. The English in the markup is
the source of truth -- nothing is duplicated into per-language files by hand.
Translations are cached in `i18n/<code>.json` against the English they came from,
so a language costs one Claude call the first time it is used, nothing after that,
and editing one English string re-translates that key alone. The header switch
forces English back at any time, and if the call fails the page simply stays in
English. Engine parameter labels come from `engines.json` and stay as written
there.

**Idempotent resume.** A scene is re-rendered when the freshly compiled prompt
differs from the prompt stored against its asset, or its file has gone missing from
`images/` -- never on file timestamps. Edit
a prompt and only that scene re-queues; re-run with nothing changed and nothing is
spent. "Re-render everything" is an explicit checkbox.

**Seedream guardrails**, applied on every compile including hand edits:
Midjourney flags (`--ar`, `--v`, `--sref`, `--sw`, ...) are stripped -- the engine reads
them as literal words; quoted dialogue is stripped -- the engine renders it as
lettering on the image, and that covers `"..."`, `«...»`, `„..."` and `「...」`
so the guard holds in any language; the scene body goes first and the shared style block last,
because instructions buried at the end of a long style block are partially ignored.
There is no negative-prompt parameter, so exclusions have to be phrased in words.

**Format follows the bytes, not the request.** Renderful has delivered both JPEG
and PNG regardless of the `output_format` asked for, so the extension comes from
sniffing the payload (`renderful.sniff`: png / jpg / webp / mp3 / wav, else
`.img`; an MP3 counts by its `ID3` tag *or* a bare frame sync). The bytes
are saved as delivered; re-encoding a JPEG to PNG cannot undo compression that
already happened, it just costs ~6x the bytes (`renderful.save_delivered` takes a
`convert_png` flag if you ever want it, and it needs Pillow).

**Failure classes**, carried over from the working `generate_boston.py`:
401/402/403/429 and "limit reached" stop the whole run -- the account is out of
credit or throttled, and the UI says so. Other 4xx (content rejection 451,
malformed request) fail that one scene immediately and the batch continues. 5xx and
network errors retry three times with backoff. Polling waits up to 3600s per image;
600 once stranded a paid render that finished later. Speech is a different animal
-- it came back in ~5s live -- so it polls every 2s up to 300s.

A render and a voice run hold separate job slots, so cancelling one never stops
the other.

**When Claude is overloaded.** `overloaded_error` (HTTP 529) means Anthropic
turned the request away before the model ran -- it is capacity on their side, it
has nothing to do with the story, and nothing is charged for it. The SDK's own
retries are sub-second and twice, which is right for a blip and useless for a
busy few minutes, so `compiler._call` ladders on top of them: four tries on the
requested model at 3s, 8s and 20s, then one try on `FALLBACK_CLAUDE_MODEL`
(`claude-sonnet-5`) as a last resort. Set that to `""` in `config.py` to fail
instead of falling back.

If the fallback answers, the project records which model actually wrote the
scenes (`claude_model`, `claude_fell_back`) and step 1 says so, because a set
written by a different model is a thing you should know about rather than
discover. Both events are logged to the terminal running the server.

Only capacity and connection failures are retried. A rejected key (401/403) and a
malformed request fail on the first attempt -- laddering those wastes your time
and never succeeds. Each failure comes back as a sentence rather than a raw SDK
exception: 529 and 429 are `503 + Retry-After`, everything else upstream is a
502, and the request id travels in `X-Claude-Request-Id` so the page can keep it
behind a *Technical details* toggle instead of in the message.

**Cost figures.** The pre-flight estimate uses `price_per_image` from
`engines.json` (a local guess, editable), narrowed by `price_table` where one
exists -- Seedream is $0.045 at 1K against $0.09 at 2K, and anything not in the
table falls back to the dearer headline price. The *actual* charge comes back on the
Renderful response and is what accumulates in "Spent so far" and in each manifest
record.

Speech is billed **per character**, not per request, so its estimate is
`price_per_1k_chars * chars / 1000` rather than a per-line figure -- a two-word
beat and a long paragraph cannot cost the same. That rate is derived, not
guessed: a live 70-character line billed $0.0035, which is exactly $0.05 per 1000
characters. A whole 12-scene story speaks for roughly **$0.04**, so the cost of
voicing a project is a rounding error next to rendering it.

---

## Adding an engine

Edit `engines.json`. Each engine declares its price, its dialect (which guardrails
to apply), and an `inputs` array that drives both the UI controls and validation:

```json
{"key": "resolution", "label": "Resolution", "type": "enum",
 "options": ["1K", "2K"], "default": "2K", "confirmed": ["1K", "2K"]}
```

Control types: `enum`, `seed`, `range`, `toggle`, `text`. `confirmed` lists the
values actually seen working on a live account -- anything outside it renders a
warning in the cost bar before you spend. Set `"verified": false` on an engine you
haven't proven yet and the UI will tell you to render one scene before a batch.
The built-in `custom` engine lets you type any Renderful model id.

Only `seedream-5.0-pro` is confirmed against a live account (it rendered the Boston
set). Everything else is unverified until you prove it.

## Adding a voice

The same file, under `voices`, with the same schema machinery -- `inputs` drives
both the controls and validation, and `verified: false` warns before a batch.

**A voice is an id, never a name.** ElevenLabs resolves `voice` only as an opaque
`voice_id`; a display name passes our validation, is accepted by Renderful, and
is rejected by the provider on the one hop that bills. So the enum's options are
ids and a `labels` map carries the human reading:

```json
{"key": "voice", "label": "Voice", "type": "enum",
 "options": ["JBFqnCBsd6RMkjVDRZzb", "EXAVITQu4vr4xnSDxMaL"],
 "labels": {"JBFqnCBsd6RMkjVDRZzb": "George · british male · warm storyteller",
            "EXAVITQu4vr4xnSDxMaL": "Sarah · american female · reassuring"},
 "default": "JBFqnCBsd6RMkjVDRZzb"}
```

`labels` is optional and general: an enum without it renders its values, exactly
as the image engines always have. To use a **cloned** voice, add its id and a
label here. The 21 shipped ids are the premade library, read from
`https://api.elevenlabs.io/v1/voices` -- public, no key needed, and worth
re-reading rather than trusting a list: three of the six names this app first
shipped had already been retired from the library.

An `engines.json` written before voices existed gains the section on first load,
and a *shipped* voice whose `schema_version` is behind the current one is
rewritten in place -- otherwise the bad list written on first run would outlive
the fix. Voices you add yourself have no shipped counterpart and are never
touched. `GET /api/v1/models?type=text-to-audio` on your own account lists what
else you could put here -- there were ten when this was written.

`eleven_flash_v2_5` is live-verified for model id, prompt and voice id. Its
speed, stability and similarity values are declared from the published schema and
unproven -- speak one line before a batch. `speech-2.6-turbo` deliberately ships
*no* voice picker: MiniMax uses its own ids, and guessing them is precisely the
mistake above.

## Not built (deliberately)

Video assembly / Seedance, SRT/VTT captions, billing and credits, accounts and
auth, sharing links. Multi-user concerns from the PRD (Postgres, Redis, S3, KMS,
spend caps at an orchestrator) collapse here to: the filesystem, `manifest.json`,
a thread pool, and a confirmation dialog.

## Tests

    .venv\Scripts\pip install -r requirements-dev.txt
    .venv\Scripts\python -m pytest             # 140 offline tests, free
    .venv\Scripts\python -m pytest -m live --live -s   # 2 live tests, ~$0.05

The offline suite drives the real FastAPI app against a fake Renderful HTTP
server on loopback and a fake Anthropic client, so the retry ladder, poll loop,
4xx split and file writing all execute for real. An autouse fixture turns any
non-loopback request into a failure, so it can never spend. The fake serves a
genuine MPEG frame stream for speech jobs, so the duration parser is measured
against real bytes rather than a stub.

Video assembly is faked at `subprocess.run`, not at the module boundary, so
every filtergraph and codec flag is constructed for real and asserted on. What
that cannot prove is that ffmpeg *accepts* the command line, so the three tests
making that claim are marked `needs_ffmpeg` and skip unless a real one is
installed. They encode locally and spend nothing, so they are not `live` tests --
they ffprobe the result and check the container length against the timeline,
because a wrong zoompan frame count does not error, it silently produces a video
of the wrong length.

The missing-ffmpeg path is forced with a fixture rather than left to the
developer's PATH. Otherwise the suite proves different things on different
machines, and silently stops proving that one the moment somebody installs
ffmpeg.

**Installing ffmpeg.** `winget install Gyan.FFmpeg` on Windows, `brew install
ffmpeg` on macOS. winget appends its `bin` to the persistent user PATH, so
**restart the terminal and the app** afterwards -- an already-running process
keeps the environment it started with. `SHOULICO_FFMPEG` overrides the lookup
with a full path if PATH does not reach it.

The live suite talks to the real Renderful and Anthropic accounts. It is skipped
unless you pass --live, renders one 1K image (SHOULICO_LIVE_IMAGE_BUDGET, default
1) and asserts at the end that it rendered no more than that.