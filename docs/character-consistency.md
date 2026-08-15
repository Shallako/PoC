# Character consistency

> **Built, and on by default.** This document was written as an estimate; the
> feature now exists and the estimate is left in place below so the two can be
> compared. What is *not* done is a live proving run -- no image-to-image request
> has been sent to a real account, so treat the whole path as unproven against
> the API even though it is fully exercised offline. See *Risks*.
>
> How it behaves: segmentation returns a **cast** of recurring characters and,
> per scene, which of them appear. Each character is rendered once as a square
> reference portrait, before any scene. Every scene is then submitted to the
> engine's `-i2i` sibling with the portraits of the characters in that scene
> attached. A scene with nobody in it is still a plain text-to-image request.

**Original estimate: about four days of work, roughly zero extra cost per image,
and both modes can live in one product without forking it.**

The cost question turns out to be the easy one. The interesting cost is
architectural, and it is smaller than expected because of one fact that had to be
checked rather than assumed.

*Everything below marked **verified** was read off the live Renderful account on
15 August 2026, not from memory. Re-check before relying on it.*

---

## What the API actually offers

**Verified -- `GET /api/v1/models`.** The catalogue carries **22 `image-to-image`
models**, and every image engine this app already ships has a sibling:

| Model | Type | Price band |
|---|---|---|
| `seedream-5.0-pro` | text-to-image | $0.045 - $0.18 |
| **`seedream-5.0-pro-i2i`** | image-to-image | **$0.045 - $0.18** |
| `nano-banana-pro` / `-i2i` | both | $0.135 - $0.54 |
| `gpt-image-2` / `-i2i` | both | $0.03 - $0.12 |
| `seedream-5.0-lite-edit-sequential` | image-to-image, `max_outputs: 15` | $0.035 - $0.14 |

**The price bands are identical, pair for pair.** Reference-conditioned
generation is not a premium tier -- it is the same model taking one more input.

Their own descriptions are explicit about the use case: `seedream-5.0-pro-i2i` is
*"premium image-to-image with single or multi-reference (2-10) input"*, and
`seedream-5.0-lite-edit-sequential` is *"batch edit multiple images with
consistent style"* at up to 15 outputs -- which is a whole 12-scene story in one
request.

## The fact that decides the architecture

**Verified -- `GET /api/v1/openapi.json?model=seedream-5.0-pro-i2i`.** The
reference is passed as:

```
image_url   string
images      array of string, format: uri
```

**URIs, not base64.** For a local-only application with no object storage, that
reads like a blocker: there is nowhere to put a locally-saved PNG that
Renderful's servers can reach, and loopback URLs are not reachable from anywhere.

Except we never need to upload anything, because **Renderful already hosts every
image it generated, and this app already stores the URL.** Every manifest record
carries `source_url`, written at render time.

**Verified by HEAD request.** A PNG rendered on 13 August answered **HTTP 200,
1,946,702 bytes**, two days later, from an unsigned CloudFront URL with no query
string, no token and no expiry parameter:

```
https://d2w6xqzevsijyc.cloudfront.net/api/<account>/<generation-id>_0.png
```

So the reference for scene 7 is the URL scene 1 came back on. **No object
storage, no upload endpoint, no presigning, no new infrastructure.** That is the
single biggest reason this is a four-day feature instead of a two-week one.

The caveat is proportional: two days of persistence is two days of evidence, not
a retention guarantee. The failure policy has to be written before this ships --
see *Risks*.

## The design

Add a **character anchor**: one image that every other scene is conditioned on.

1. Render the anchor. Either scene 1 as it is generated today, or a dedicated
   character sheet -- one prompt describing the recurring subject, neutral pose,
   plain background -- for **$0.045**.
2. Render scenes 2..N with `type: image-to-image`, the same compiled prompt as
   today, and `images: [anchor_url]`.
3. Multi-reference takes 2-10, so a story with a recurring character *and* a
   recurring location passes both.

The prompt compiler needs no new vocabulary. The style block already carries the
look; the anchor carries the identity. They are complementary, which is exactly
why bolting "same face please" onto the prompt text has never worked.

## The real cost: three things in the code

**1. Idempotency identity -- the one that matters.** Today a scene re-renders
when its freshly compiled prompt differs from the prompt stored against its
asset. With an anchor, a scene's output also depends on *the anchor image*. So
the stored identity becomes `(prompt, anchor_asset_version)`, and re-rendering
the anchor makes every dependent scene stale.

That is a genuine dependency graph where today there are independent scenes, and
it is the only part of this feature that can quietly corrupt a project: get it
wrong and a user re-renders the anchor, sees eleven scenes still showing the old
face, and cannot work out why. It needs to be right, and it needs a test that
asserts staleness propagates.

**2. Ordering.** Three workers currently render every scene in parallel because
nothing depends on anything. The anchor must complete before the rest start, so
the orchestrator gains one barrier. Modest work, but it changes the shape of a
run and the progress UI has to say what it is waiting for.

**3. Payload construction.** `renderful.submit()` hardcodes the image payload to
the shape the Boston set was rendered with. It needs a third generation type
alongside `text-to-image` and `text-to-audio`, carrying `images`. Small and
contained -- the function was already written to branch on `gen_type`.

Everything downstream is untouched. Timings, captions, assembly, the timing sheet
and export do not know or care how a scene's PNG was produced.

## Effort

| Work | Days |
|---|---|
| `renderful.py` -- image-to-image payload and generation type | 0.5 |
| `engines.json` -- i2i siblings and an `anchor` block on each engine entry, following the existing `clip` pattern | 0.5 |
| Anchor concept, dependency staleness, orchestrator barrier | **1.5** |
| UI -- mode switch, anchor preview, "re-rendering this restages 11 scenes" warning | 0.5 |
| Tests, including staleness propagation and a `live`-marked proving run | 1.0 |
| **Total** | **~4 days** |

Plus **$1-2 of live spend** to prove it, which is the part that cannot be faked
in a test.

## Cost to the end user

| | Today | With an anchor |
|---|---|---|
| 12-scene story, Seedream 5.0 Pro at 1K | $0.54 | **$0.585** |
| Narration | $0.04 | $0.04 |
| Assembly | $0.00 | $0.00 |

**One extra image -- about 8% -- and only if a dedicated character sheet is used.**
Anchoring on scene 1 instead costs nothing at all. The `-edit-sequential` route
could come in *under* today's price by batching, though it caps at 15 outputs and
has not been tested here.

There is no per-user infrastructure cost either, because there is no
infrastructure: the reference is a URL Renderful is already serving.

## Yes, both modes -- and it is a setting, not a fork

This is the important product answer. Consistency is a **project mode**, chosen
where the engine is chosen today:

- **Draft mode (current behaviour).** Text-to-image, every scene in parallel,
  cheapest, fastest, no dependency graph. Right for a lesson sequence, a pitch,
  a mood piece, or any story with no recurring subject.
- **Consistent mode.** Anchor plus image-to-image. Right for a picture book, a
  character-driven story, a brand mascot, a series.

They share the prompt compiler, the timeline, the captions, the assembly, the
carousel and the export. The divergence is one field in the render payload and
one edge in the job graph. **There is no second pipeline to maintain**, which is
the outcome worth designing for -- and the reason to do it now, while the render
path is one function, rather than after it has grown two.

It also creates a genuinely good product moment: **"make these consistent"** on a
finished draft. The anchor URL is already stored, so upgrading a draft is a
re-render of scenes 2..N against an image the user has already seen and approved.
The draft is not thrown away; it becomes the reference.

## Risks and the honest unknowns

**Still unproven: whether it actually holds a face across 12 scenes.** This is
now the only thing standing between the feature and a claim. Multi-reference
conditioning is well documented and widely used, but "documented" is what the
retired voice list was too, and **nobody on this account has rendered a single
image-to-image request**. Everything below the API boundary is exercised
offline -- the payload, the model id, the ordering, the staleness, the failure
paths -- and none of that proves Renderful accepts the request or that the
result looks like the same person.

A **$0.50 test** settles it: one story, two characters, three scenes. Until it
has run, `"ref": {"verified": false}` stays as it is and the cost preview keeps
warning before a batch.

**URL longevity is evidenced, not guaranteed.** Two days proven, unsigned, no
expiry parameter. If those URLs ever rotate, every project's anchor breaks at
once and silently. The policy has to be decided up front: on a dead anchor URL,
re-render the anchor from its stored prompt and seed, mark dependents stale, and
tell the user -- never fail the run, and never quietly fall back to
text-to-image, which would produce a different face with no explanation.

**Base64 is untested.** `format: uri` may well accept a `data:` URI, which would
remove the dependency on Renderful-hosted URLs entirely and let a user anchor on
an image of their own. Worth one cheap test, because it turns "consistent
characters" into "*your* character" -- a materially bigger feature.

**Multi-reference limits differ by model.** Seedream Pro takes 2-10, the
sequential variants cap at 15 outputs, and Nano Banana's own limit is not stated
in the catalogue. These belong in `engines.json` as dated, editable values, the
same treatment prices and voice ids get, and for the same reason.

**Anchoring on scene 1 couples two decisions.** Re-frame scene 1 and every other
scene moves. A dedicated character sheet costs $0.045 and avoids it. Recommend
the character sheet as the default and scene 1 as the free option.

## What was actually built, against the estimate

The estimate held. What it under-described was the failure handling, which is
where most of the care went.

| Estimated | Built |
|---|---|
| `renderful.py` i2i payload | `GEN_TYPE_IMAGE_REF`, `references=`, and a refusal to submit a reference render with an empty array -- that request is a text-to-image one wearing the billing model's id |
| Registry `ref` block | On all three engines, plus an **additive-only** migration: a missing key is filled in, an existing entry is never rewritten, so a re-priced or renamed engine survives |
| Anchor + staleness + barrier | As designed. Anchors render first, all of them, before any scene |
| UI | Cast panel on step 2, per-character description and portrait, toggle, and the anchors named in the cost preview |
| Tests | **26**, including the dependency and every failure path below |

Three things the estimate did not anticipate:

**A failed anchor must not take the story down.** A story rendered with one
character unpinned is worth more than no story, so a failed portrait drops out of
its scenes' references and those scenes record what they were *actually* rendered
against. That matters because a scene that recorded the intended references would
look settled forever and never re-stage once the portrait worked.

**A failed *re*-render must not advertise the new version.** The version and the
URL are two halves of one token. An early draft bumped the version when the
anchor started rendering, so a failure left the character claiming v2 while still
holding v1's picture -- every scene would then be conditioned on the old face
while recording that it used the new one. Wrong, and permanently settled, which
is the worst combination. The version is now written only on success, and
`test_a_failed_re_render_never_advertises_the_new_version` fails without that.

**Cast order, not scene order.** Two scenes with the same characters must produce
the same fingerprint, or reordering a scene's cast list would re-render a picture
that would come out identical.

## What this changes in the pitch

[premise.md](premise.md) currently states, as the honest boundary for educators
and writers, that a recurring character's face is not guaranteed across scenes --
and that it is *"a known, solvable engineering problem"* not solved here today.

That sentence stays true until the live test above passes. What changes is that
"solvable" now has a number attached: **four days, $0.045 a project, no new
infrastructure, and no second pipeline.** For the self-publishing writer segment
in particular, this is the difference between a story-shape test and something
that could carry a character through a whole book -- which is most of the
distance between a draft tool and a product they would pay for.
