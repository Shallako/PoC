# Working on Shoulico

A local FastAPI + vanilla-JS app that turns a story into scenes, prompts, images,
a narration script, narration audio, a video and an export folder, through the
Renderful and Anthropic APIs. Loopback only, no auth, single user.

`README.md` is the reference and is kept current -- every merge that changes
behaviour updates it in the same commit, including the test count in *Tests*.
This file is the shorter thing: how work is done here.

## Before the first edit

Cut a feature branch. Never commit to `main` directly. Merge with `--no-ff` and
a message that says what changed and why, push, delete the branch.

## Every button on the page spends real money

Renderful bills per image and per 1000 characters of speech, and **a refused
generation is billed like any other**. That is the fact behind most of the
design here: an incident cost $2.88 and produced no pictures at all.

So:

- Plan before spending. `plan()` and `plan_audio()` answer "what would this do
  and what would it cost" without doing it, and every spend endpoint requires
  `confirm: true`.
- Say what something will cost *before* the press, and what it did cost after.
  `activity.jsonl` records the attempt, not the success -- `manifest.json` only
  holds things that exist, which is exactly the wrong half when money went
  missing.
- Never make a request the user did not ask for. An empty selection means none,
  not all; a subset renders the character portraits that subset uses, not every
  stale one.

## Nothing blocks, everything explains

The style screen, the out-of-date warning, the prompt reading before a render:
all of them are warnings with a way forward, and all of them let you go ahead.
A gate gets clicked through the second time it appears; a sentence that says
what will happen does not. `screening.py`'s docstring has the long version.

## The page never holds a copy of a number the server owns

`MAX_STORY_CHARS`, `MAX_SCENE_COUNT`, `CLAUDE_ATTEMPTS`, the backoff ladder --
all published through `/api/status` and read from there. A limit typed into the
markup disagrees with `config.py` the moment anyone tunes it, and disagrees
*silently*. Before `/api/status` answers, a control has no bounds rather than
invented ones. The same rule applies to money: the page displays the server's
estimate, it does not multiply anything out itself.

Wording is the page's, codes are the server's: the server returns
`reason_key`/`code`, the page holds the sentence, and the server's English is
the fallback for a code this build has never heard of.

## Tests

`.venv\Scripts\python -m pytest` -- 433 offline tests, free, under a minute. It
drives the real FastAPI app against a fake Renderful HTTP server on loopback and
a fake Anthropic client, so retries, polling, the 4xx split and file writing all
execute for real. An autouse fixture fails any non-loopback request, so the
suite can never spend.

- **A new test must fail against the code it was written for.** Stash the source
  (`git stash push -- shoulico/`) and run it. If it passes, it is testing the
  fixture, not the fix. Say the ratio in the commit message.
- Fix the code, not the test, when they disagree -- unless the test is the thing
  that is wrong, in which case say so plainly.
- Test names are sentences. Docstrings say why the test exists, usually by
  naming the bug or the incident it came from.
- `tests/browser/` drives the real page in Chromium and is the only thing that
  can prove the page's behaviour. It needs Node, so it is outside pytest --
  start the app, seed a project, `node check.mjs http://127.0.0.1:PORT <pid>`.
  `run.py` has no `--reload`, so restart the server after editing the page.

## Comments

Say why, not what. The ones worth writing record the failure that made the code
look like this -- a measured incident, a race that was actually hit, a number
that came from a real run. If a comment could be deleted without losing
anything, delete it.

## Source control is code only

Nothing a fresh clone can regenerate: no artifacts, venvs, caches, editor
config, and no `engines.json` (`engines.py` writes it on first run). `projects/`
and `i18n/` are runtime output. Keys live in `api_key.txt` / `anthropic_key.txt`
or the environment, are gitignored, and are never logged, never sent to the
browser, and never written into a project file.

## Layout

| File | What |
|---|---|
| `run.py` | launcher; refuses a non-loopback bind without `--allow-remote` |
| `shoulico/app.py` | every endpoint; `_decorate` adds the derived fields the page reads |
| `shoulico/config.py` | every tunable number, with the reasoning beside it |
| `shoulico/orchestrator.py` | plans and runs the parallel jobs; owns `Job` and `plan` |
| `shoulico/compiler.py` | the Claude calls, the retry ladder, prompt compilation |
| `shoulico/engines.py` | the engine/voice/video registry and its migrations |
| `shoulico/screening.py` | reads a prompt the way a safety classifier will |
| `shoulico/activity.py` | the business-activity ledger and its diagnosis |
| `shoulico/static/index.html` | the whole front end: markup, CSS, JS, strings |
| `docs/` | one note per feature: the reasoning, the numbers and the sources |
