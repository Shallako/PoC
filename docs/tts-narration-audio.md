# Narration audio via Renderful TTS

Turn the narration *script* into narration *audio*, and replace the word-count
duration estimate with a measured one.

Status: specification. Scope agreed 2026-08-13.

---

## Why

The export today gives an editor images and `.txt` files. CapCut needs sound with
a known length. Every downstream feature -- captions, image timing, an ffmpeg
assembly, video clips whose duration must match the line -- derives from a real
per-scene duration, and there is no way to get one from text.

This is the keystone increment. Nothing else in the video roadmap can start
until it lands.

## Scope

In:

- One `text-to-audio` engine in the registry, driven by the same schema machinery
  as the image engines.
- Per-scene MP3 written beside the image under the shared flat stem.
- Measured duration stored per scene in `project.json` and per asset in
  `manifest.json`.
- Audio carried into `export/`.

Out (deliberately, for now):

- SRT/VTT captions. Trivial once durations exist; a separate increment.
- ffmpeg assembly, Ken Burns, muxing.
- Image-to-video clips.
- Voice cloning, per-character voices, SSML.

## Rejected: native audio from video models

`seedance-2.0-t2v`, `kling-v2-6`, `kling-v3`, `sora-2-i2v` and `vidu-q2-i2v` all
advertise native audio. It is the wrong audio. Those models invent ambience,
effects and dialogue from the *image* prompt; there is no parameter that accepts
an approved narration line and reads it aloud. Using it would discard the
review-and-edit narration workflow, which is the part of this app a human
actually controls.

The split stays: **video models move pictures, TTS speaks the script, ffmpeg
marries them.** Generate clips silent or mute the native track.

---

## Verified against the live account

One live generation, 2026-08-13, id `tro5R9t8qky565RboWR6`.

Request -- `POST https://api.renderful.ai/api/v1/generations`:

```json
{"type": "text-to-audio", "model": "eleven_flash_v2_5", "prompt": "..."}
```

Response on submit, then poll `GET /api/v1/generations/{id}` to `completed`:

```json
{"id": "...", "status": "completed",
 "outputs": ["https://d2w6....cloudfront.net/api/.../..._0.mp3"],
 "output":  "https://d2w6....cloudfront.net/api/.../..._0.mp3",
 "cost": 0.0035, "estimated_cost": 0.0035}
```

Facts that change the design:

| Finding | Consequence |
|---|---|
| **Cost was $0.0035**, not the $0.05--0.20 the model listing implies | The listing's `cost.min/max` is a ceiling, not a price. Bill from the response, as the image path already does. A 12-scene story costs about **$0.04**. |
| Completed in **~5 seconds** | No need for the 3600s image poll ceiling. 300s is generous. |
| Output is an **MP3 with an ID3v2 header** (`49 44 33`, then `04`) | `sniff()` needs `ID3` *and* the raw MPEG frame sync (`\xff\xfb`, `\xf3`, `\xf2`) -- a stripped MP3 has no ID3 tag. |
| **`ffprobe` is not installed on this machine** | Duration must be measured in pure Python. No new dependency: the project uses stdlib `urllib`, not `requests`, and Pillow is already optional-only. |
| Response echoes `aspect_ratio`/`resolution`/`num_outputs` back on an audio job | Harmless server-side defaulting. Do not send them; do not read them. |
| 13 words measured **4s**; `NARRATION_WPM = 150` predicts **5.2s** | The estimate runs ~30% long. This is the whole justification for the increment -- keep the estimate for pre-flight UI, replace it with truth after render. |

Model shortlist (`GET /api/v1/models?type=text-to-audio`, 10 available):

| Model | Provider | Note |
|---|---|---|
| `eleven_flash_v2_5` | ElevenLabs | verified above; fastest, cheapest |
| `eleven_multilingual_v2` | ElevenLabs | most stable for professional content |
| `eleven_v3` | ElevenLabs | most expressive |
| `speech-2.6-turbo` | MiniMax | **40 languages** -- the match for our i18n narration |
| `speech-2.6-hd` | MiniMax | best prosody |

Parameters for the ElevenLabs family, from the model page:

- `prompt` (string, required, max 40 000 chars)
- `voice` -- George, Rachel, Adam, Bella, Josh, Arnold
- `language_code` -- 32 languages
- `output_format` -- MP3 128/192/64 kbps, PCM 44.1/24 kHz
- `stability`, `similarity_boost` (0--1), `speed` (0.25--4.0)

MiniMax parameters are unconfirmed -- ship it `"verified": false`.

---

## Changes

### `shoulico/renderful.py`

`submit()` hardcodes `"type": "text-to-image"` at line 80 and always sends
`aspect_ratio`, `resolution`, `num_outputs`, `output_format`. This is the one
blocking change.

- Add `gen_type: str = "text-to-image"` and build the payload from the engine's
  declared inputs instead of the fixed image five, so an audio engine sends
  `prompt` plus its own params and nothing else.
- `sniff()`: add `mp3` (ID3 or frame sync) and `wav` (`RIFF`/`WAVE`). Note the
  README says the fallback extension is `.img`; the code returns `"bin"`. Leave
  the behaviour, fix the README line.
- Add `POLL_TIMEOUT_AUDIO = 300`. The 3600s ceiling exists because a paid image
  render once finished late; a 5s TTS job does not need it.

### `shoulico/audio.py` (new)

Pure-stdlib MP3 duration. Skip the ID3v2 tag using its syncsafe size field, read
the first MPEG frame header for version/layer/bitrate/sample rate, prefer a
Xing/Info frame count when present, else fall back to CBR
`(bytes - tag) * 8 / bitrate`. Returns seconds as a float.

Must not raise on a malformed file -- return `None` and let the caller fall back
to `narration.estimate_seconds()`. A bad duration should degrade to the old
behaviour, never fail a render that was already paid for.

### `engines.json`

Add a sibling `voices` map beside `engines`, same shape so `engines.py`
validation is reused unchanged: `inputs` array driving both UI and validation,
`confirmed` lists, `verified` flag, price fields.

- `eleven_flash_v2_5` -- `"verified": true`, `price_per_generation: 0.0035`
  with a `price_note` recording the live figure and date, matching the Seedream
  entry's convention.
- `speech-2.6-turbo` -- `"verified": false`, for multilingual stories.

### `shoulico/tts.py` (new)

`synthesize(text, key, model, params) -> (bytes, cost, ext)`. Submits, polls,
downloads, sniffs. Mirrors the image path's failure classes exactly: 401/402/403/429
and "limit reached" are fatal for the whole run; other 4xx fail the one line;
5xx and network errors retry three times with backoff. Reuse `api_call()` -- do
not write a second retry ladder.

### `shoulico/orchestrator.py`

A narration-audio job reusing the existing pool, stop event and status
reporting. Idempotence rule mirrors the image rule: re-synthesize when the
narration text differs from the text stored against the audio asset, or the file
is missing. Editing one line re-cuts one line.

### `shoulico/store.py`

- `audio_dir(pid)` -> `projects/<id>/audio/`.
- Record `duration` and `audio` on the scene; add an audio record to
  `manifest.json` carrying model, cost, exact text sent, and duration.
- `export()` copies the MP3 under the flat stem, so `export/` holds
  `..._001_arrival-at-dusk.jpg` / `.txt` / `.mp3` as a set.

### `shoulico/app.py`

`POST /api/projects/{id}/narrate-audio`, confirmation mandatory exactly as
`/render` is -- without `confirm: true` it is a 400. It spends money; it follows
the same rule. Plus a cost pre-flight, and cancel wired to the existing endpoint.

### `shoulico/static/index.html`

In step 2 beside the narration editor: voice engine picker, per-scene "speak
this line", a batch button with cost estimate, and the measured duration shown
next to the existing estimate once known.

---

## Tests

`tests/fake_tts.py` alongside `fake_renderful.py`, serving a real generated MP3
of known length over loopback so `audio.duration()` is exercised against actual
bytes rather than a stub. The autouse fixture that fails any non-loopback request
already covers this -- the offline suite must stay free.

Cover: submit/poll/download happy path; the 4xx-splits-one-line vs
fatal-stops-the-run ladder; idempotent resume when text is unchanged; re-cut when
a line is edited; duration parsed from a real MP3; malformed audio falling back
to the estimate rather than raising; export placing the three files under one
stem.

Add one `@pytest.mark.live` test guarded by a budget env var like the existing
`SHOULICO_LIVE_IMAGE_BUDGET`, asserting it spent no more than that.

## Acceptance

1. `python -m pytest` passes offline and free.
2. A 3-scene project produces three MP3s named to match their images.
3. `manifest.json` carries model, real cost and measured duration per line.
4. `export/` holds image, text and audio under one stem per scene.
5. Editing one narration line and re-running re-cuts that line only.
6. Total spend for a 12-scene story is about $0.04.

## Follow-ups

Captions from measured durations; ffmpeg assembly with Ken Burns; image-to-video
clips choosing duration from measured narration length; `music-2.5` for a
background bed; `openai-whisper-with-video` for forced-aligned caption timing.
