# Carousel export -- scope

**Why:** Buffer's 52m-post analysis puts Instagram carousels at **6.90%**
engagement per person reached against Reels at **3.31%** -- roughly 2x, and the
top of the format table. This pipeline already produces the two things a carousel
is made of, one still and one text block per scene, and currently throws the
combination away by flattening them into a video. See
[premise.md](premise.md#1-the-format-won-that-part-is-settled) for the numbers
and the caveats attached to them.

**Size:** ~2 days for the full feature. **Half a day for tier 1**, which is
useful on its own and needs no new dependency.

---

## The two tiers

**Tier 1 -- slide set, no rendering.** The existing exported stills, renamed into
slide order, plus a paste-ready text file: one block per slide, the suggested
post caption, and the alt text. The user uploads the images to Instagram or
TikTok and pastes the caption. Zero new dependencies, no ffmpeg, works on the
free path exactly as captions do today.

**Tier 2 -- rendered slides.** Each still re-framed to the platform canvas with
its narration text burned onto it behind a readability scrim, which is what a
carousel actually looks like in the feed. Needs ffmpeg, which step 5 already
optionally depends on.

Ship tier 1 first. It is most of the value, it cannot break the no-ffmpeg
guarantee, and it makes tier 2 a rendering change rather than a feature.

## The design decision that matters

**Slides come from `timeline.build()`, not from the scene list.**

The beats returned by `timeline.build()` already carry image path, narration
text, scene number, title and slug -- everything a slide needs. Reading them
means slide *N*, video scene *N* and caption cue *N* are the same picture and
the same words by construction, not by two functions agreeing.

This is the same argument `timeline.py` was written for in the first place, and
its module docstring makes it: two nearly-identical calculations drift, and the
drift is invisible in a test that checks each half on its own. A carousel that
disagreed with the video about what scene 7 says would be exactly that bug.

Text wrapping reuses `captions.wrap()` for the same reason -- it already handles
the 42-character line limit, the two-line cap, and character-wrapping for scripts
that do not space their words.

## What changes

| File | Change | Size |
|---|---|---|
| `shoulico/carousel.py` | **new** -- `Slide`, `slides()`, `render()`, `caption_text()` | ~150 lines |
| `shoulico/config.py` | canvas sizes, slide limits, scrim opacity, suffixes | ~15 lines |
| `shoulico/engines.py` | `SECTION_CAROUSEL`, `DEFAULT_CAROUSEL_PROFILES`, one tuple entry in `_migrate`, `carousel_profile()`, `default_carousel_key()`, `public_carousel()` | ~70 lines |
| `shoulico/store.py` | `carousel_dir()`, slide stems, `write_carousel_files()` called from `export()` | ~40 lines |
| `shoulico/app.py` | `GET /api/carousel-profiles`, `POST .../carousel/settings`, `POST .../carousel/build`, `GET .../carousel/{name}` | ~60 lines |
| `shoulico/static/index.html` | a Carousel panel beside Video on step 5 | ~80 lines |
| `tests/test_carousel.py` | **new** | ~15 tests |

**No orchestrator changes.** Rendering twelve single frames is a sub-second job
per slide; there is no poll loop, no cost, and nothing to cancel. Rendering,
speaking and assembling earn job slots because they are slow, billable or both.
If slide counts ever reach the point where a build blocks a request
noticeably -- call it 30+ slides on a slow disk -- it graduates to a `KIND_CAROUSEL`
job then, and not before.

**The registry addition is one line.** `_migrate()` already loops over a
`sections` tuple filling missing sections and repairing stale shipped entries, so
`carousel` joins `voices` and `video` by appending one entry. The versioned
repair comes free.

Proposed profile inputs, driving both the UI and validation as every other
section does:

```json
{"key": "canvas",  "type": "enum", "options": ["ig-portrait", "ig-square", "tiktok", "source"], "default": "ig-portrait"},
{"key": "text",    "type": "enum", "options": ["burn", "none"], "default": "burn"},
{"key": "fill",    "type": "enum", "options": ["blur", "crop", "pad"], "default": "blur"},
{"key": "cover",   "type": "toggle", "default": true},
{"key": "end_card","type": "toggle", "default": false},
{"key": "max_slides", "type": "integer", "min": 2, "max": 35, "default": 20}
```

## Decisions and the alternatives rejected

**ffmpeg, not Pillow.** Pillow means a new dependency plus cross-platform font
discovery, which is its own long tail of bugs. ffmpeg is already the optional
dependency for step 5, `video.py` already has the scale/crop expression that
frames an image correctly, and libass already does font selection and wrapping
for burned-in subtitles. Reusing it means a slide and a video frame are framed by
the same code and cannot disagree.

**Blur fill, not a hard crop.** A 16:9 render cropped to 4:5 loses about 40% of
the frame, and the subject is not reliably centred. Filling the margin with a
scaled, blurred copy of the same image is the platform convention, keeps the
whole composition visible, and costs one extra filter stage. `crop` and `pad`
stay available for people who want them.

**Title and first line as the cover, not a generated hook.** A real hook slide
wants copy written for the purpose, which means another Claude call and another
few cents. The story title plus its opening narration line is free and adequate.
A generated hook is a priced, opt-in upgrade later, on the same confirmation
pattern as rendering and speaking.

**Slides per scene, not per caption cue.** A cue is a reading-speed unit, roughly
84 characters; a scene is a narrative beat. Carousel readers swipe on beats. Per-
cue slides would blow past the platform limit on a normal story.

## Risks

**Platform slide limits need verifying before they are hardcoded.** Instagram's
carousel maximum and TikTok Photo Mode's have both changed more than once. They
belong in the registry as editable values with a comment recording when they were
last checked -- the same treatment `engines.json` gives model prices, and for the
same reason.

**Burned text needs a font.** libass finds one through fontconfig, which is
reliable on Windows and macOS and occasionally not on a bare Linux container. The
failure mode must be a slide with no text and a warning, never a crashed export.
`text: none` is the fallback and is already a supported value.

**Text over a busy image is unreadable.** A scrim -- an opaque or semi-opaque band
behind the text -- is required, not optional. libass `BorderStyle=4` gives one
without a second filter pass.

**A 4:5 crop of a 16:9 render can lose the subject.** Blur fill mitigates it,
but the real answer is telling the user on step 1 that a carousel-first story
should be rendered 4:5 or 9:16. The aspect ratio is already an engine parameter,
so this is a hint in the UI rather than a code change.

## Tests

Faked at `subprocess.run` through the existing `tests/fake_ffmpeg.py`, so the
filtergraph is constructed for real and asserted on:

- slide count matches scene count, plus the cover when enabled
- slide *N* carries scene *N*'s image and scene *N*'s narration text
- the wrap matches what `captions.wrap()` produces for the same line
- canvas dimensions per profile; `source` follows the rendered aspect
- blur / crop / pad each produce the filter stage they claim
- `text: none` emits no subtitle or drawtext stage at all
- a story longer than `max_slides` is reported, not silently truncated
- the caption text file contains every line, in order
- export without ffmpeg still writes tier 1 and answers 424 only for tier 2
- settings validation rejects an out-of-schema canvas with a readable message

Plus one `needs_ffmpeg` test that renders a real slide and ffprobes it for exact
pixel dimensions -- the same reasoning as the video tests, where a wrong
expression does not error, it silently produces the wrong size.

## Done means

A 12-scene story exports a numbered slide set at 1080x1350 with its narration
burned on, a paste-ready caption file, and the same content as the video it
already produces -- with no second place in the code where "what scene 7 says"
is decided.
