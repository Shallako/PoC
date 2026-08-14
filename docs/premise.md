# Shoulico -- a story orchestrator

**Plain conversational text in. A finished, captioned, narrated vertical video
out. About a dollar, in minutes.**

*Investor brief, August 2026. Every figure is sourced at the foot of the
document; the technical picture is in the [README](../README.md).*

> **Say plainly what the output is.** Shoulico does **not** generate moving
> footage. It generates one **still image per scene** and stitches those stills
> into a video -- each one held for exactly as long as its narration line runs,
> with an optional slow Ken Burns zoom over it, spoken narration on top and
> burned or soft captions. Technically: a narrated slideshow rendered to MP4.
>
> That is the honest description, and section 7 argues it is the right product
> decision rather than a limitation we are working around. Anyone evaluating
> this should read the claim as *"the format that already fills these feeds,
> produced for a dollar"* -- not as animation, and not as generated video.

---

## 1. The format won. That part is settled.

Short vertical video is no longer a content type. It is where the attention is.

| | Time per day, per user |
|---|---|
| TikTok | **1h 37m** |
| YouTube | 1h 23m |
| Instagram | 1h 13m |
| Facebook | 67m |

*(DataReportal, Digital 2026, Android app data.)*

**5.66 billion** social identities -- 68.7% of the planet -- and **94.6%** of
online adults watch online video every month. On Instagram in the US, Reels took
**46% of all time spent** in 2025, up from 37% the year before.

The money moved with the attention. **More than half of every ad Instagram ran in
2025 ran inside Reels**, up from 35% in 2024. Zuckerberg told the October 2025
earnings call that Reels across Instagram and Facebook had passed a **$50 billion
annual run rate**.

Two numbers that matter more than the rest for what we are building:

- **71%** of marketers say the effective length is **30 seconds to 2 minutes**.
- **84%** of consumers say they want *more* video from brands.

A 12-scene Shoulico story runs 60-90 seconds. That is not a coincidence; it is
the target.

**The honest caveat.** These figures prove where attention sits -- short, vertical,
captioned, sound-on. They do not, on their own, prove that a sequence of *stills*
holds up in that feed. The nearest measured evidence says stills are more than
viable, and it is worth reading exactly rather than cherry-picking.

Buffer's analysis of **52m+ posts** ranks Instagram formats by engagement per
person reached: **carousels 6.90%, single images 4.44%, Reels 3.31%**. Stills
beat video on engagement by roughly 2x. But Reels win the other half: **+36%
reach over carousels and +125% over single images**. Instagram is effectively two
platforms -- video buys reach, stills buy engagement.

TikTok is where the evidence conflicts, and we should say so. Buffer puts TikTok
video at **3.39%** against images at **1.92%** -- stills losing. A separate
Fanpage Karma study of **698,000 posts** (Jan-May 2025) reports the opposite:
carousels at **+81% engagement** over comparable video. Two credible firms, two
sample windows, opposite signs. Neither should be quoted alone.

And the deeper limit applies to both: **every one of these studies measures
swipeable carousels, not an auto-playing narrated video assembled from stills.**
That is our format and nobody has benchmarked it. So the claim this document
makes is the defensible one -- stills-based content demonstrably performs on
these platforms -- and **not** that our specific output matches shot footage.
Measuring that is first-party work: post a set, read the retention curve. It is
the assumption the rest of the business rests on, and it is cheap to test.

One implication worth noting: the top-engagement Instagram format is the carousel,
and a carousel is exactly what this pipeline already produces internally -- one
still and one text block per scene, before assembly. Exporting to the highest-
engagement format is close to free from where the code already stands.

## 2. Everyone needs it. Almost nobody can make it.

**91% of businesses now use video marketing** -- an all-time high. And this is
where the market breaks:

| Why businesses don't make (more) video | Share |
|---|---|
| Cost | **24%** |
| Don't see the need | 24% |
| No time | **19%** |
| Unclear ROI | 10% |
| **Don't know where to start** | **10%** |

*(Wyzowl, State of Video Marketing 2026.)*

Strip out the 24% who don't want video and **53% of the remainder are blocked by
cost, time, or not knowing how to begin** -- three problems software can actually
solve.

The cost figure is not irrational. Professional production runs **$1,000-$10,000
per finished minute**. The DIY alternative is not free either: small-business
owners already spend about **6 hours a week** on social content -- roughly
**$15,600 a year** of their own time at $50/hr.

And the demand is not a niche. **36.2 million** US small businesses. **68%** of
SMB owners across five countries say social posting and ads will drive the most
value for their business in 2026. **33%** are already on TikTok, double the 17%
of 2023. Add a creator economy estimated at **$235-320 billion** in 2026 and the
addressable population is not "marketers" -- it is anyone with something to say.

## 3. The actual gap: cadence versus capacity

The platforms reward volume. A serious account posts several times a week; the
highest-cadence ones post daily or more. Meanwhile the unit of production still
costs four figures a minute and takes days.

**That gap is the whole business.** Not "make video cheaper" -- make the *first
draft* nearly free, so the expensive version only ever gets made for the ideas
that already proved themselves.

The market has already voted on the direction: **63% of video marketers used an
AI video tool in the past year, up from 51% the year before**. What they have not
been given is a tool that starts from *the way a person actually talks*.

## 4. What Shoulico is

You paste what you would say out loud -- a paragraph, a memory, a pitch, a
bedtime story. It comes back as an ordered set of scenes. Each scene gets **one
still image**, one spoken narration line, and one caption block. Those stills are
then stitched into a single MP4, each held for the exact length of its own
narration.

To be unambiguous: **there is no moving footage anywhere in the pipeline.** The
only motion is an optional slow zoom across a still image. What plays back is a
narrated, captioned sequence of pictures.

No storyboard. No prompt vocabulary. No editing timeline.

1. **Paste the story.** Up to 5,000 characters, any language. Say how many
   pictures you want.
2. **Read back what it understood.** Beats, image descriptions, narration -- all
   editable text. This is where you disagree with it.
3. **Render the pictures.** Price shown before you spend; you have to say yes.
4. **Hear it spoken.** Pick a voice, confirm, every line is read aloud.
5. **Get the video.** The stills are stitched on your own machine -- free -- each
   held exactly as long as its narration runs, with an optional Ken Burns zoom.
   9:16, 1:1 or 16:9.
6. **Take it away.** Images, audio, captions, timing sheet and MP4 in one folder.

Steps 1, 2, 5 and 6 cost nothing. Only rendering and speaking spend money, and
neither happens without explicit confirmation.

## 5. The differentiator: you steer in your own words

The hard part of image generation is not the machine, it is the vocabulary. "Make
it look nicer" is not a prompt. "35mm, shallow depth of field, overcast key
light" is -- and most people have never needed to know that.

So the controls are layered, and you meet them at whatever level you know:

- **Say nothing.** Defaults are chosen and the story renders. A complete path,
  not a degraded one.
- **Say it in your own words.** "Warmer." "Less cartoonish." "1970s." "Keep the
  dog in every shot." That goes into the shared style block, which applies to
  every image at once -- one sentence re-styles the entire set.
- **Edit the beats.** Change which moments were picked, reorder, rewrite, fix a
  name it misheard.
- **Edit the compiled prompt.** The exact text being sent is shown and can be
  overwritten. Nothing hides behind the friendly version.
- **Change the engine and its parameters.** Model, resolution, aspect, seed.

You move up and down that ladder mid-project. Nobody has to start at the bottom;
nobody is capped at the top. Competing tools pick one rung and sell it: template
fillers own the bottom, prompt consoles own the top. The person with a story and
a growing sense of taste is served by neither.

Two more things that read as small and aren't. The story's **own language**
carries through -- an author writing in Portuguese reviews, edits and hears it
back in Portuguese, and the interface follows. And re-running is **free unless
something changed** -- edit one line and only that line is re-rendered or
re-spoken. Iterating does not repeatedly bill you for the parts you were happy
with, which is exactly the behaviour that makes people iterate.

## 6. Why a cheap draft is the wedge

| | Shoulico draft | Professional production |
|---|---|---|
| Cost | **~$1** | $1,000-$10,000 per finished minute |
| Time | minutes | days to weeks |
| Changing your mind | retype a sentence | renegotiate |

That is a **1,000-10,000x** cost delta on the first pass -- and it is deliberately
not a like-for-like comparison. A narrated sequence of stills is not a shot film,
and nobody should read that row as "Shoulico replaces a production company." It
replaces the *document and the conversation* that currently stand in for a
production before one is commissioned.

The expensive decisions in any visual project -- how many scenes, what order,
what it sounds like, how long it runs, whether the idea works at all -- are
answerable from a rough cut. Today they are made from a document and a
conversation, and discovered to be wrong after the money is spent.

So the hand-off matters as much as the video. Every export carries a **timing
sheet**: one row per scene with its start, its length, whether that length was
*measured or estimated*, and the exact narration text -- plus SRT and VTT
subtitles. An editor rebuilds the cut in CapCut, Premiere or Resolve without
dragging a single clip against a waveform.

The draft is not a dead end you abandon. It is the plan the serious version gets
built from. That is what makes it sellable to a professional buyer and not just
a hobbyist one.

## 7. The unit economics, and the lesson from Sora

A 12-scene story, all in:

| Line item | Cost |
|---|---|
| 12 images | $0.55 - $1.00 |
| Narration (~1,500 characters at $0.05/1k) | $0.04 |
| Assembly, captions, export (local ffmpeg) | **$0.00** |
| **Total** | **~$1** |

No GPU fleet. No inference capex. Generation is a metered pass-through; assembly
is the user's own CPU. Cost of goods scales with usage instead of preceding it.

**This is where stills earn their place.** Generated video clips are 20-40x the
price of a still -- twelve scenes is roughly $1 in images against **$10-$68 in
clips**, priced off the live model catalogue. They also come back at a fixed
length of about 4-8 seconds against narration lines that commonly run 5-15
seconds, silent, unjoined, and needing exactly the same local assembly step
afterwards. So buying motion does not remove any work; it adds cost on top of it
and takes away the ability to hold a beat for as long as the sentence needs.

A still holds any length for free. That is the entire argument, and it is a
pricing argument rather than a technical one -- if clip prices fall or lengths
become variable, the same architecture picks them up as an opt-in upgrade for
hero shots. The scene-by-scene structure, the measured timings and the assembly
step do not change.

This is not a theoretical advantage. **OpenAI's Sora is the control experiment.**
Launched September 2025 as a pure AI-video destination, it peaked at ~3.3 million
monthly downloads in November, fell to ~1.1 million by February, earned about
**$2.1 million in total in-app purchases**, and was shut down on 26 April 2026 --
seven months old.

The read: there was no sustained appetite for an AI-only feed to *watch*. The
appetite is for getting **your own story** into the feeds that already have the
audience. That is a tool, not a destination -- and a tool that costs a dollar a
draft has an economic floor a generative destination never had.

Meanwhile the tool side is where capital has gone: **Synthesia at a $4B valuation
on a $200M round** (Oct 2025), **Runway's $315M Series E** (Feb 2026), **Luma's
$900M Series C** (Nov 2025), **$75M into Captions/Mirage** (Mar 2026), HeyGen
reported at ~200,000 paying customers and ~$100M recurring revenue, OpusClip
valued at $215M by SoftBank. Every one of them sells to someone who *already
knows* they need video and already has assets. **None of them start from a
paragraph a person wrote in their own voice.**

## 8. What is built today

A working local application, verified end to end:

- Story segmentation, prompt compilation, and narration scripting via Claude, in
  the story's own language.
- Image generation through the Renderful API (Seedream 5.0 Pro live-verified;
  Nano Banana Pro and GPT Image 2 wired).
- Narration through ElevenLabs Flash v2.5, with duration **measured off the
  delivered audio** rather than estimated -- so captions and image timing cannot
  drift from the voice.
- Local ffmpeg assembly of the stills into one MP4: Ken Burns zoom over each
  still, broadcast-convention captions, soft or burned-in subtitles, proven
  against a real encode (12.02s output against a 12.0s timeline).
- **140 automated tests**, all offline and free, including a fake Renderful HTTP
  server so the retry ladder, poll loop and failure classes execute for real.

Deliberately **not** built, with the reasoning recorded in the repository:
generated video clips (section 7); accounts, billing and sharing. **Every frame
that plays back today originates as a still image.** The only motion is the Ken
Burns zoom, and the only sound is the narration -- no music bed, no effects, no
transitions between scenes beyond a hard cut.

## 9. The bet

Generation is cheap and getting cheaper. Judgment is not.

The value is not in the pictures -- anyone can buy pictures. It is in going from
*a paragraph a person actually wrote* to *a watchable thing with a beginning and
an end*, without asking that person to learn anything, and cheaply enough that
being wrong is fine.

Get that right and the draft is not a compromise. It is where the work starts.

---

## Sources

**Attention and platform data**
- [DataReportal -- Digital 2026 Global Overview Report](https://datareportal.com/reports/digital-2026-global-overview-report) -- time per day by platform, 5.66bn social identities, 94.6% monthly online-video reach.
- [CNBC, 20 Jan 2026 -- "Most of Instagram's ads ran on Reels in 2025"](https://www.cnbc.com/2026/01/20/most-of-instagrams-ads-ran-on-reels-in-2025-data-shows.html) -- >50% of IG ads on Reels (from 35%); US Reels at 46% of time spent (from 37%). Sensor Tower / Meta data.
- [Tubefilter, 30 Oct 2025 -- Meta Q3 2025 earnings call](https://www.tubefilter.com/2025/10/30/meta-reels-ad-revenue-q3-2025-earnings-report/) -- Zuckerberg on Reels' $50bn annual run rate.

**Format performance (stills vs video)**
- [Buffer -- State of Social Media Engagement 2026](https://buffer.com/resources/state-of-social-media-engagement-2026/) -- 52m+ posts across 10 platforms through 3 Dec 2025; Instagram format breakdown from 4m+ posts, Jan 2022 - Oct 2024. Engagement defined as likes + comments + saves + shares as a percentage of reach. Instagram: carousels 6.90%, single images 4.44%, Reels 3.31%; Reels reach +36% vs carousels, +125% vs single images. TikTok: video 3.39%, images 1.92%.
- Fanpage Karma, 698,000 TikTok posts Jan-May 2025 -- carousels +81% engagement and +82% likes vs comparable video, but 33% fewer shares. Reported via secondary coverage; **directly contradicts Buffer's TikTok figures** and should be re-verified at source before either is used in a deck.
- No published study measures an auto-playing narrated slideshow, which is what this product outputs. Treat all of the above as adjacent-format proxies.

**Demand and barriers**
- [Wyzowl -- State of Video Marketing 2026](https://wyzowl.com/video-marketing-statistics/) -- 91% of businesses use video; 82% report good ROI; barrier breakdown (cost 24%, time 19%, don't know where to start 10%); 71% on 30s-2min; 63% used AI video tools, up from 51%; 84% of consumers want more brand video.
- [SBA Office of Advocacy, 30 Jun 2025](https://advocacy.sba.gov/2025/06/30/new-advocacy-report-shows-the-number-of-small-businesses-in-the-u-s-exceeds-36-million/) -- 36.2m US small businesses.
- [eMarketer, 2026](https://emarketer.com/content/small-businesses-see-social-media-their-clearest-path-growth-2026) -- 68% of SMB owners (Constant Contact, Jan 2026, AU/CA/NZ/UK/US); 33% of small businesses on TikTok, up from 17% in 2023 (SBE Council).

**Production cost**
- [Vidico -- Video Production Cost 2026](https://vidico.com/news/video-production-cost/) and [Synthesia -- Cost of Video Production](https://www.synthesia.io/learn/video-production/cost) -- $1,000-$10,000 per finished minute, from Clutch agency survey data. Vendor-compiled; treat as an industry range, not an audited figure.
- 6 hours/week of owner time on social is a widely-reported SMB survey range (2025-26); the $15,600/yr figure is that range valued at $50/hr, i.e. illustrative arithmetic rather than a surveyed number.

**Comparables and the Sora case**
- [TechCrunch, 24 Mar 2026 -- OpenAI shuts down Sora](https://techcrunch.com/2026/03/24/openais-sora-was-the-creepiest-app-on-your-phone-now-its-shutting-down/) -- Appfigures download curve (3.33m Nov to 1.13m Feb), ~$2.1m lifetime in-app purchases.
- [OpenAI Help Center -- Sora discontinuation](https://help.openai.com/en/articles/20001152-what-to-know-about-the-sora-discontinuation) -- app discontinued 26 Apr 2026.
- [Forbes, 29 Oct 2025 -- Synthesia at $4bn](https://www.forbes.com/sites/rashishrivastava/2025/10/29/ai-video-startup-synthesia-valued-at-4-billion-in-new-200-million-raise/); [Sacra -- OpusClip](https://sacra.com/c/opusclip/) ($215m valuation, $20m SoftBank Vision Fund 2). Runway ($315m Series E, Feb 2026), Luma ($900m Series C, Nov 2025), Captions/Mirage ($75m, Mar 2026) and HeyGen's reported ~200k paying customers / ~$100m recurring revenue are from press reporting and should be re-verified before use in a deck.

**Creator economy**
- 2026 estimates range from ~$235bn to ~$323bn depending on methodology (Research and Markets, Coherent Market Insights, market.us). The spread is wide enough that the range, not any single figure, is the honest number.
