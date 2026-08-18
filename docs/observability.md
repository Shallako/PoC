# Observability -- plan

*Plan only, nothing built. Free and open-source components exclusively; licences
are named because two of the obvious choices are AGPL and that matters the day
this stops being a PoC.*

---

## 1. What is actually at risk here

Start from what has gone wrong in this repository rather than from a stack.

| Incident | What it cost | What would have caught it |
|---|---|---|
| Voice ids were display names | **Every** TTS call failed -- a 100% outage | An upstream catalogue drift check |
| Poll ceiling of 600s | A paid render finished after we stopped listening | Poll-duration distribution against the ceiling |
| Claude 529 overloads | Silent model substitution: a set written by a different model | Fallback-rate counter |
| Prompt rejected 451 | Billed, no asset, no record anywhere | A billable-attempt ledger that includes failures |
| Anchor re-render cascade | Re-renders 11 scenes -- correct, but expensive and surprising | Restage amplification figure |
| ffmpeg missing | Step 5 dead | A capability probe at startup |

Every row is either **money spent wrongly** or **an upstream contract that moved
underneath us**. Not one is a latency problem, and not one is a scaling problem.

**Thesis: Shoulico's observability problem is spend correctness and upstream
drift.** A single-user local FastAPI app with a three-worker thread pool has no
distributed-systems problem to solve. Instrumenting it as though it does would
produce a dashboard nobody opens and a compose file nobody runs.

## 2. What already exists, and the one thing it misses

`manifest.json` is **already an audit ledger**: one record per asset carrying
engine, model, params, the exact prompt sent, generation id, source URL, cost and
timestamp. That is better provenance than most production systems have.

Its blind spot is precise and expensive: **it only records successes.** A
generation that was billed and produced nothing -- a 451 content rejection, a
download that failed after completion, a run cancelled after submission, a retry
ladder that burned three attempts -- leaves no trace at all. `project.json` keeps
a `detail` string per scene, and the next attempt overwrites it.

So the first gap is not a tracing backend. It is that **the events which cost
money and produced nothing are the only events not written down.**

## 3. Tier 0 -- the ledger (no dependencies, highest value)

An append-only JSONL event log, written with the standard library's `logging` and
a JSON formatter. No third-party package, nothing to run, nothing to keep alive.

**One event per billable attempt, written at the attempt rather than at success.**
Proposed fields:

    ts, run_id, project, kind (image | anchor | speech), scene | character,
    engine, model, gen_type, generation_id,
    outcome (ok | rejected | failed | cancelled | timeout), http_status,
    attempt, estimate, actual_cost, latency_ms, poll_count,
    references[], prompt_sha256, bytes, error_class

Four deliberate choices:

- **`prompt_sha256`, not the prompt.** The prompt is already in the manifest for
  successes, and a hash is enough to correlate two rows without putting a user's
  story into a log that might be pasted into an issue.
- **`estimate` beside `actual_cost` on the same row**, so reconciliation is a read
  rather than a join across two files.
- **`run_id`** correlating every event of one batch, including the anchors that
  preceded the scenes.
- **One append-only JSONL file**, greppable with what is already on the machine,
  rotated by size with `RotatingFileHandler`.

**Then one read-only report**: for a project or for everything, what was
estimated, what was actually billed, where the two diverged, and how much was
spent on attempts that produced no asset. That last number -- **wasted spend** --
is the most useful figure in this whole document, and today it is unknowable.

*Effort: half a day. Dependencies: none.*

## 4. Tier 1 -- OpenTelemetry traces (Apache 2.0)

With the ledger in place the remaining question is *shape*: what waited on what.
That is a trace, and this app's structure maps onto one cleanly.

    story.segment                    gen_ai.*, scene count, cast size, fell_back
    render.plan                      to_render, skipped, anchors, estimate
    render.job (run_id)
    |-- render.anchor x N            <- the barrier, visible as a waterfall
    |   \-- renderful.submit / poll / download
    \-- render.scene x N
        |-- renderful.submit         gen_type, model, reference count
        |-- renderful.poll           poll_count, backoff
        \-- renderful.download
    narration.speak -> tts.line x N
    video.assemble -> ffmpeg.segment x N -> ffmpeg.join -> ffmpeg.subtitles

Why this earns its keep beyond the ledger: the **anchors-before-scenes barrier**
and the **retry ladder** are both time-shaped. A waterfall shows at a glance that
three workers sat idle while two portraits rendered, or that a batch's wall clock
was dominated by one scene's poll loop. A table of rows does not.

- **`opentelemetry-sdk` with hand-written spans.** Skip FastAPI
  auto-instrumentation at first: HTTP spans for a UI that polls are noise, and the
  boundaries that matter (Renderful, Anthropic, ffmpeg) need hand-written
  attributes anyway.
- **Use the `gen_ai.*` semantic conventions** on the Claude spans
  (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`). Following the convention rather than inventing
  names is what lets any OTel-native viewer read the data with no configuration.
- **Default exporter: file or console; OTLP strictly opt-in** by environment
  variable. With no collector running the app must not stall, retry, or print a
  wall of connection errors -- and by default there should be nothing to connect
  to at all.
- **No sampling.** At a few dozen spans per story, keep everything.

*Effort: one day. Dependencies: `opentelemetry-sdk`, `opentelemetry-exporter-otlp`,
both Apache 2.0.*

## 5. Tier 2 -- somewhere to look (optional, pick one)

Only worth running when a waterfall is actually going to be read. All are free and
self-hostable. **Verify each licence at adoption time -- they move.**

| Option | Licence (verify) | Why / why not |
|---|---|---|
| **Jaeger** | Apache 2.0 | One container, traces only, does the waterfall job and nothing else. **Best fit for this PoC.** |
| **SigNoz** | Apache 2.0 core, separate enterprise edition | Traces, metrics and logs in one compose file, OTel-native. Reasonable if you want all three. |
| **Grafana + Prometheus + Tempo/Loki** | Prometheus Apache 2.0; **Grafana, Loki and Tempo AGPLv3** | The industry default, and three services to keep alive for one user. AGPL is fine internally; worth knowing before anything is bundled or hosted for others. |
| **OpenObserve** | AGPLv3 | Single binary, very light, if you want one process instead of a stack. |
| **VictoriaMetrics** | Apache 2.0 | Only if metrics become a real need. Prometheus would already be enough. |

A `/metrics` endpoint via `prometheus-client` (Apache 2.0) is cheap and harmless.
But **a single-user local app has no time-series problem**: the figures that
matter here are cumulative and per-project, which the ledger answers better than
a scrape would.

## 6. The Claude half, if you want it

Claude does segmentation, prompt writing, narration and interface translation.
Those calls have token counts, latency, refusals and a fallback ladder, and none
of it is recorded today beyond `claude_model` and `claude_fell_back`.

| Option | Licence (verify) | Notes |
|---|---|---|
| **OpenLLMetry / Traceloop SDK** | Apache 2.0 | Auto-instruments the Anthropic SDK and emits standard OTel spans, so it feeds whichever Tier 2 you chose instead of adding a second system. **Lowest friction.** |
| **Langfuse** | MIT core, some features EE | Self-hosted, strong prompt/cost/version tracking. Purpose-built for this, but it is another stack to run. |
| **Arize Phoenix** | Elastic License 2.0 -- **not OSI-approved** | Good evaluation tooling, but the licence disqualifies it if "open source" is a hard requirement. |

Given the thesis, OpenLLMetry is the recommendation: one dependency, speaks OTel,
needs no extra service.

## 7. The check that would have caught the worst bug

**Upstream catalogue drift.** The voice-id outage happened because `engines.json`
held values that were true when they were written and silently stopped being true.

A read-only job -- `GET /api/v1/models` plus the public ElevenLabs voices
endpoint, neither of which bills -- comparing every model id, voice id and price
band in `engines.json` against what the account actually offers, and reporting:

- ids that no longer exist (the outage class);
- price bands that moved (every estimate in the UI is wrong from that moment on);
- `verified: false` entries still unproven;
- new siblings that have appeared (`-i2i`, video pairs).

This is neither tracing nor metrics. It is a **contract test against a live
upstream**, it costs nothing to run, and it addresses the only incident in this
repo's history that took the product to zero.

*Effort: half a day. Run on demand and before any release.*

## 8. The numbers worth watching

Not a dashboard -- a short report. If a figure would not change a decision, it is
not on the list.

**Money**
- Cost per finished story (the ~$1 claim in the pitch, measured rather than
  asserted)
- **Wasted spend ratio**: billed generations that produced no usable asset
- Estimate accuracy: `|actual - estimate| / estimate`, per engine
- Idempotency effectiveness: the fraction of re-runs that spent exactly $0
- Restage amplification: scenes re-rendered per character edit

**Upstream health**
- Renderful outcome mix: ok / 451 / 5xx-retried / timeout
- Claude fallback rate and 529 rate
- Poll duration p50 / p95 / max **against the 3600s ceiling** -- the margin is the
  point, not the average

**Local**
- ffmpeg present; encode seconds per finished minute
- Timeline drift: assembled duration against the computed timeline. The video
  tests assert this to within 0.5s; in normal use nothing checks it.

## 9. Safety, and the way instrumentation usually breaks it

The README promises that keys are *"read on demand, never logged, never sent to
the browser."* Instrumentation is the classic way that promise gets broken by
accident, so the constraints belong in the plan rather than in a later fix:

- **Never** log or attach as a span attribute the `Authorization` header, the key
  files, or any environment variable matching `*KEY*`. One redaction helper
  applied at the exporter, not trusted to each call site.
- **Story text, prompts and narration are user content.** Hash by default; full
  capture behind an explicit opt-in that is off by default and never enabled in a
  build that exports to a remote collector.
- **Cardinality**: project id, scene slug and prompt hash belong in span
  attributes, never in metric labels.
- **Local by default.** No configured OTLP endpoint means no egress. An
  observability feature that quietly shipped story content off the machine would
  be a worse bug than any it could detect.

## 10. The observability the *user* sees

For a consumer PoC this may matter more than everything above. The person using
Shoulico will never run Jaeger, and their question is not "what is p95" -- it is
**"why did this cost $1.40 when you said a dollar?"**

An in-app **run history** panel, reading the Tier 0 ledger: each run, what it
rendered, what it skipped and why, what failed, what it estimated, what it
actually cost, and the running total for the project. No new dependency -- it is
the ledger with a template around it.

This is the only item here that is also a product feature, and since the pitch
leans on cost transparency, it is arguably the highest-value one.

## 11. Phasing, and what I would skip

| Phase | Work | Effort | Deps |
|---|---|---|---|
| 0 | JSONL ledger + reconciliation report | 0.5 d | none |
| 1 | Catalogue drift check | 0.5 d | none |
| 2 | OTel spans, file exporter, opt-in OTLP | 1 d | otel-sdk |
| 3 | In-app run history panel | 1 d | none |
| 4 | Jaeger container, when a waterfall is needed | 0.5 d | docker |
| 5 | OpenLLMetry for the Claude half | 0.5 d | traceloop-sdk |

**Skipped deliberately:** Prometheus + Grafana + Loki (three services, one user,
AGPL, and the questions they answer are answered better by the ledger); FastAPI
auto-instrumentation (the UI polls, so it would drown the signal); trace context
propagation (there is nothing to propagate to); log aggregation (one machine, one
file); alerting (there is nobody to page).

**Do phases 0 and 1 even if nothing else ever happens.** Between them they cost a
day, add no dependencies, and address every incident in the table at the top.
