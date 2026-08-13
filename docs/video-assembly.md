# Captions and video assembly

Turn measured narration into a finished cut, and into the timed artifacts an
editor needs.

Status: **implemented** 2026-08-13. Scope agreed 2026-08-13: captions *and* the
ffmpeg MP4, with the CapCut hand-off kept as a first-class option rather than a
fallback.

---

## Why now

The TTS increment was justified as the keystone: nothing downstream can be timed
without a real per-scene duration. That is now true, so everything here is
arithmetic over numbers that already exist.

Before this, `export/` held images, narration text and MP3s. An editor still had
to place every clip by eye against a waveform. There was no video step in the UI
at all.

## The fact that shaped the design

**Renderful is a generation API, not an editing one.** Image-to-video returns N
separate silent clips: no narration in them, and no way to concatenate them
there. So buying motion does not remove the assembly step, it adds cost on top of
one that still has to happen locally or in CapCut.

That reframes the decision. The question is not "stills or generated video" but
**where assembly happens**. Motion source is a secondary, per-scene choice, and
the cheap one is free.

Prices pulled from the live account, 2026-08-13, for a 12-scene story:

| Route | Cost | Note |
|---|---|---|
| Captions + timing sheet, assemble in CapCut | **$0.00** | no dependency at all |
| ffmpeg Ken Burns assembly here | **$0.00** | needs the ffmpeg binary |
| `seedance-1.5-pro-i2v`, the cheapest usable clip model | $1.80 -- $3.60 | 2--4x the whole rest of the project |
| `google-veo-3.1-i2v` | $34 -- $68 | no |

Generated clips also have a duration mismatch: they are a fixed ~5s, while
narration lines commonly run 5--15s. A still holds any length for free. Clips are
deliberately not built; the follow-up section says what would change that.

---

## Design

### One timeline, two consumers

`shoulico/timeline.py` is the spine. Captions and the encoder both derive every
number from it, because the alternative is two nearly-identical calculations that
drift by a frame and put the subtitles out of sync with the voice -- the one
defect in a finished video that is obvious to everybody and invisible in a test
that checks each half on its own.

A `Beat` carries `start`, `duration`, `speech_start`, `speech_seconds` and
`measured`. Duration is measured audio plus a lead-in and a tail, because a cut
landing on the last syllable sounds clipped and a line starting on the first
frame of a new picture reads as a mistake. Where a line has no audio yet the
word-count estimate stands in and `measured` is false, so a preview works before
a voice run and simply gets more accurate after one.

### Captions are not narration lines

One scene's line can run fifteen seconds and two hundred characters. Holding that
on screen at once is unreadable, so a line is split at sentence boundaries and
the scene's measured speech time is shared out in proportion to length. The
conventions are the broadcast ones, not invented: 42 characters a line, two
lines, 5/6 s minimum, 7 s maximum.

The proportional split assumes an even speaking rate within a line, which is not
exactly true. It is honest at the scene boundary, which is what matters: every
cue lies inside that scene's real audio, so drift cannot accumulate across the
video the way it would if each cue were timed from a word-count guess. Forced
alignment would tighten the inside of a line and is the obvious upgrade.

One case ends captions before the audio deliberately: a three-word line over a
twelve-second scene hits the 7 s cap and the cue comes down. The reader has
finished; leaving it up for the pause is not a caption. There is a test named for
that so it does not read as a bug later.

### One segment per scene, then a stream copy

A single giant filtergraph is faster to write and impossible to resume, report
progress on, or cancel. This app already reports per-scene progress for renders
and for speech, so video matches: one `encode_segment` per scene, then `join`
with `-c copy`. Every segment is written with identical codec settings, which is
what makes the copy legal and what makes the join take a second instead of
another full encode.

Every segment carries an audio stream even when its scene is silent
(`anullsrc`), because a stream copy breaks on a segment missing a stream.

### Two ffmpeg traps worth recording

- **zoompan expands one input frame into `d` frames.** A looped input therefore
  multiplies out to `frames x duration x fps`. Ken Burns feeds a single frame and
  lets zoompan make the rest; the still-frame path is the one that needs `-loop`.
- **The `subtitles=` filter parses its own argument**, so a Windows absolute path
  arrives as a filter option separator plus a path. Burn-in runs with `cwd` set
  to the video directory and passes a bare filename. The concat list does the
  same, for the same reason.

Zoom is linear in the frame counter rather than the usual `zoom+step`
accumulator: the accumulator's final zoom depends on how many frames it happened
to run for, so two scenes of different lengths would end at different sizes.

### Settings live in the registry

`engines.json` gained a `video` section beside `engines` and `voices`. These are
typed inputs with defaults, which is exactly what the schema machinery already
draws controls for and validates -- so the six controls and their validation came
free, and the defaults are the user's to change.

That needed one new control type, `integer`, distinct from `range` because the
consumer is a command line: a frame rate of `30.0` is not a frame rate ffmpeg
takes.

### ffmpeg is optional

Every entry point reports its absence as a fact the UI can act on rather than
raising deep in a worker thread. The panel says so, disables the one button that
needs it, and prints the install command. Captions, the timing sheet and the
whole CapCut hand-off need no ffmpeg at all. The assemble endpoint answers 424,
not 500: the request was fine, a dependency this server needs is not here.

---

## What changed

| File | Change |
|---|---|
| `shoulico/timeline.py` | new -- the shared spine |
| `shoulico/captions.py` | new -- SRT/VTT with broadcast conventions |
| `shoulico/video.py` | new -- ffmpeg detection, segment, join, subtitles |
| `shoulico/engines.py` | `video` section, `integer` control type, generalised repair |
| `shoulico/store.py` | `video/` dir, `write_editor_files`, timing CSV, video settings |
| `shoulico/orchestrator.py` | `KIND_VIDEO` job, `plan_video`, `start_video` |
| `shoulico/app.py` | six endpoints, `video_job` on the project |
| `shoulico/narration.py` | `unspaced()` extracted so captions reuse it |
| `index.html` | step 4 panel, renumbered export to 5, `renderParams` default fallback |

Steps were renumbered so Video is 4 and Export is 5. Export bundles the finished
cut, so the reverse order would have meant exporting twice.

## Tests

`tests/fake_ffmpeg.py` fakes at `subprocess.run`, not at the module boundary, so
every filtergraph and codec flag is constructed for real and asserted on. What no
fake can prove is that ffmpeg *accepts* the command line; that claim belongs to
three tests marked `needs_ffmpeg`, which skip unless a real binary is installed.
They encode locally and spend nothing, so they are not `live` tests. They ffprobe
the output and check the container length against the timeline, because a wrong
zoompan frame count does not error -- it silently produces a video of the wrong
length, which is the failure this whole file is most exposed to.

The *missing* ffmpeg path is forced with a `no_ffmpeg` fixture rather than left
to whatever the machine has. That test passed for the wrong reason until ffmpeg
was installed, at which point it failed and exposed the flaw: a test of a missing
dependency has to control that dependency, or the suite proves different things
on different machines.

## Verified live, 2026-08-13

ffmpeg 9.0 (`Gyan.FFmpeg`, with libass and libx264) installed on the dev machine,
and the encode proven end to end. No API call and no money -- assembly is local.

- Three scenes from real 1424x800 renders, encoded to 1920x1080 h264 + AAC and
  joined: **12.02s against a 12.0s timeline**, 4.47 MB, 11.8s to encode.
- Ken Burns is genuinely moving: frames sampled at 0.1s and 3.8s of the same
  scene differ, and the later frame is a clean ~12% push-in matching
  `VIDEO_KEN_BURNS_ZOOM` -- tighter framing, no jitter, no crop damage.
- Soft subtitles produce a real subtitle stream; burn-in produces none, because
  the captions are pixels by then.
- The still-frame path holds for the whole scene, so the `-loop` branch is right.

winget appends its `bin` to the persistent user PATH, so the app must be
restarted after installing: a running process keeps the environment it started
with. `install_hint()` already says so.

## Follow-ups

- Forced alignment (`openai-whisper-with-video`) to tighten cue timing inside a
  line rather than sharing time out by character count.
- A `music-2.5` background bed, ducked under the narration.
- Per-scene generated clips as an opt-in upgrade for hero shots, priced and
  confirmed like every other spend -- never the default, for the cost reasons at
  the top of this file.
- Crossfades between scenes. The timeline already has the shape for it; it needs
  an overlap term that both the encoder and the captions read.
