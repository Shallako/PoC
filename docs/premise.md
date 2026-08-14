# Shoulico -- a story orchestrator

*What this is for, in plain terms. The technical picture is in the
[README](../README.md).*

---

## The idea

Most people can describe the story they want to see. Almost nobody can write the
prompt that gets it.

Shoulico closes that gap. You paste what you would say out loud -- a paragraph, a
memory, a pitch, a bedtime story -- and it comes back as an ordered set of
scenes, each one rendered as a picture, spoken aloud, and cut into a finished
video with captions. No storyboard, no prompt vocabulary, no editing timeline.

The whole thing costs about **a dollar** and takes a few minutes.

## Who it is for

Someone with a story and no production crew. A teacher turning a lesson into
something a class will sit through. A founder who needs a sixty-second explainer
before Friday. A parent making one thing for one child. A creative director who
wants to see the shape of an idea before committing a budget to it.

The common thread is that none of them want to learn a tool. They want to be
understood the first time and then adjust in words.

## What you actually do

1. **Paste the story.** Up to 5,000 characters, in any language. Say how many
   pictures you want.
2. **Read back what it understood.** The story is split into beats, each with a
   title, an image description and a line of narration. All of it is editable
   text -- this is the step where you disagree with it.
3. **Render the pictures.** You see the price before you spend it, and you have
   to say yes.
4. **Hear it spoken.** Pick a voice, confirm again, and every line gets read
   aloud.
5. **Get the video.** Assembled on your own machine, free, with the pictures held
   for exactly as long as the narration takes.
6. **Take it away.** Images, audio, captions, a timing sheet and the MP4, in a
   folder you can hand to anyone.

Steps 1, 2, 5 and 6 cost nothing. Only rendering and speaking spend money, and
neither happens without an explicit confirmation.

## Steering it, at whatever level you know

The hard part of image generation is not the machine, it is the vocabulary. "Make
it look nicer" is not a prompt. "35mm, shallow depth of field, overcast key light"
is, and most people have never needed to know that.

So the controls are layered, and you meet them where you are:

- **Say nothing.** Defaults are chosen and the story renders. This is a complete
  path -- it is not a degraded one.
- **Say it in your own words.** "Warmer," "less cartoonish," "1970s," "keep the
  dog in every shot." That goes into the style block, which applies to every
  image at once, so one sentence re-styles the whole set.
- **Edit the beats.** Change which moments got picked, reorder them, rewrite a
  description, fix a name the machine misheard.
- **Edit the compiled prompt.** The exact text being sent is shown, and you can
  overwrite it. Nothing is hidden behind the friendly version.
- **Change the engine and its parameters.** Resolution, aspect ratio, seed, model
  -- everything the underlying service exposes.

You can move up and down that list mid-project. Nobody has to start at the
bottom, and nobody is capped at the top.

Two things that matter more than they sound: the story's **own language** carries
through, so an author writing in Portuguese reviews and edits in Portuguese, and
the interface follows. And re-running is **free unless something changed** --
edit one line and only that line is re-rendered or re-spoken, so iterating does
not repeatedly charge you for the parts you were happy with.

## Why the output is a draft, and why that is the point

This is not trying to be the final film. It is trying to be the thing you look at
*before* the final film exists.

| | Shoulico draft | A real production |
|---|---|---|
| Cost | ~$1 | thousands, upward |
| Time | minutes | days to weeks |
| Changing your mind | retype a sentence | renegotiate |

The expensive decisions in any visual project -- how many scenes, what order,
what it sounds like, how long it runs, whether the idea works at all -- are all
answerable from a rough cut. Today those decisions are usually made from a
document and a conversation, and get discovered to be wrong after the money is
spent.

A draft that costs a dollar changes who gets to make those decisions and when.
You can show it to the client, the class, the investor or your co-founder, watch
their face, and throw it away. Three times, in an afternoon.

That is why the hand-off matters as much as the video does. The export folder
carries a **timing sheet** -- one row per scene with its start, its length,
whether that length was measured or estimated, and the exact narration text --
plus subtitle files in standard formats. A real editor can rebuild the whole cut
in CapCut, Premiere or Resolve from that sheet without dragging a single clip
against a waveform. The draft is not a dead end you abandon; it is the plan the
serious version gets built from.

## What it deliberately does not do

- **It does not generate video clips.** Motion currently costs 20-40x a still and
  comes back as fixed-length silent fragments that still have to be assembled by
  hand. A still image held for the length of its narration costs nothing and
  fits any line. If that price moves, the decision moves with it.
- **It does not guess your voice for you.** Narration is written, shown to you,
  and only spoken after you have approved the words. Speaking is the step that
  bills, so the script is yours to fix first.
- **It does not hide what it spends.** Every batch is priced before it runs, and
  what was actually charged is recorded per asset.
- **It is not a multi-user product.** No accounts, no sharing links, no billing
  layer. It runs on one machine, for one person, and writes plain files to a
  folder.

## The shape of the bet

Generation is cheap and getting cheaper. Judgment is not. So the value is not in
the pictures -- anyone can buy pictures -- it is in going from *a paragraph a
person actually wrote* to *a watchable thing with a beginning and an end*,
without asking that person to learn anything, and cheaply enough that being wrong
is fine.

Get that right and the draft is not a compromise. It is where the work starts.
