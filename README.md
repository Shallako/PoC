# Shoulico -- local MVP

Story -> scenes -> engine-targeted prompts -> images -> narration script -> export.

Image creation defaults to **Seedream 5.0 Pro** through the Renderful API.
Scope is **images and narration script only** -- no video, no TTS, no audio, no
accounts, no billing layer.

"Narration" here means the **script text** (PRD FR-1201/1203/1206): Claude writes
one line per scene, you review and edit it, and it is written to text files that
share the image filename. Nothing is synthesised.

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
| Renderful | image rendering | `RENDERFUL_API_KEY` -> `PoC\api_key.txt` -> `Renderful\api_key.txt` |
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

## The four steps

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
4. **Export** -- flattened, sort-safe copies plus matching narration files, the
   full voiceover, and a copy of `manifest.json`. `flatten: false` keeps the
   versioned filenames instead. `export/` is cleared first, so it never carries
   files from a previous run.

## What lands on disk

```
projects/<project-id>/
  project.json     story, language, style block, scenes, prompts, narration, params, spend
  manifest.json    one record per asset: engine, seed, params, exact prompt sent, cost
  images/          <project>_<NNN>_<slug>_v<VV>[_seed<SEED>].<jpg|png>
  narration/       <project>_<NNN>_<slug>.txt   and  <project>_full-voiceover.txt
  export/          flattened <project>_<NNN>_<slug>.<jpg|png> + .txt
                   + <project>_full-voiceover.txt + manifest.json
engines.json       the engine registry -- edit to add a model
i18n/<code>.json   cached interface translations, one file per language
```

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
scripts that do not space their words (Chinese, Japanese).

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
sniffing the payload (`renderful.sniff`: png / jpg / webp, else `.img`). The bytes
are saved as delivered; re-encoding a JPEG to PNG cannot undo compression that
already happened, it just costs ~6x the bytes (`renderful.save_delivered` takes a
`convert_png` flag if you ever want it, and it needs Pillow).

**Failure classes**, carried over from the working `generate_boston.py`:
401/402/403/429 and "limit reached" stop the whole run -- the account is out of
credit or throttled, and the UI says so. Other 4xx (content rejection 451,
malformed request) fail that one scene immediately and the batch continues. 5xx and
network errors retry three times with backoff. Polling waits up to 3600s per image;
600 once stranded a paid render that finished later.

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

## Not built (deliberately)

Video assembly / Seedance, TTS and audio, SRT/VTT captions, billing and credits,
accounts and auth, sharing links. Multi-user concerns from the PRD (Postgres, Redis,
S3, KMS, spend caps at an orchestrator) collapse here to: the filesystem,
`manifest.json`, a thread pool, and a confirmation dialog.

## Tests

    .venv\Scripts\pip install -r requirements-dev.txt
    .venv\Scripts\python -m pytest             # 65 offline tests, free
    .venv\Scripts\python -m pytest -m live --live -s   # 2 live tests, ~$0.05

The offline suite drives the real FastAPI app against a fake Renderful HTTP
server on loopback and a fake Anthropic client, so the retry ladder, poll loop,
4xx split and file writing all execute for real. An autouse fixture turns any
non-loopback request into a failure, so it can never spend.

The live suite talks to the real Renderful and Anthropic accounts. It is skipped
unless you pass --live, renders one 1K image (SHOULICO_LIVE_IMAGE_BUDGET, default
1) and asserts at the end that it rendered no more than that.