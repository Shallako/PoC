"""Business activity log -- what happened, what it was meant to cost, and what
it actually cost.

`manifest.json` is already an excellent provenance record, but it has one
precise and expensive blind spot: **it only records successes.** A generation
that was billed and produced nothing -- a 451 content rejection, a download that
died after the picture was made, a run cancelled after submission, a retry
ladder that burned three attempts -- leaves no trace at all. `project.json`
keeps a one-line `detail` per scene, and the next attempt overwrites it.

So the events that cost money and produced nothing were the only events not
written down, and "what did this story actually cost me" was unanswerable. This
module is the answer: one append-only JSONL line per business event, written
**at the attempt rather than at success**, so a failure is as legible as a win.

Two kinds of line go in:

  * `attempt(...)` -- one billable call to an upstream (an image, a character
    portrait, a spoken line, a Claude call). Always written, whatever happens.
  * `record(...)` -- a workflow milestone (project created, story segmented,
    video assembled, export written). No money, but it is the spine the
    billable lines hang off.

What is deliberately *not* in here
----------------------------------
No story text, no prompt, no narration line. Prompts are hashed, because a hash
is enough to tell two rows apart or to correlate one with the manifest, and
this file is the sort of thing that ends up pasted into a bug report. Keys and
URLs are never written at all. See §9 of docs/observability.md.

Deviations from that plan, and why
----------------------------------
The plan said `logging` with a `RotatingFileHandler`. A handler per project
holds an open file handle, and `store.delete()` does `shutil.rmtree` -- on
Windows that fails outright while a handle is open. So the append is done
directly under the same per-file lock the rest of the store uses, opening and
closing per write. At one line per 131-second render this costs nothing, and it
means deleting a project still works.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .security import project_id

log = logging.getLogger("shoulico.activity")

FILE_NAME = "activity.jsonl"

# Rotation. A line is roughly 400 bytes and a twelve-scene story writes about
# twenty of them, so a megabyte is several hundred stories -- generous enough
# that rotation is a backstop against a pathological loop rather than something
# that happens in normal use.
MAX_BYTES = 1024 * 1024
KEEP = 3

# --------------------------------------------------------------------------- #
# Outcomes
#
# Five verdicts, chosen because each one implies a different answer to the only
# question that matters here: was this billed, and did it produce anything?
# --------------------------------------------------------------------------- #

OK = "ok"                 # an asset exists
REJECTED = "rejected"     # the API read the request and refused it (451 and kin)
FAILED = "failed"         # anything else that went wrong
CANCELLED = "cancelled"   # the user stopped it
TIMEOUT = "timeout"       # still running when we stopped listening
FATAL = "fatal"           # out of credit, bad key, throttled out -- stops the run

# Where an attempt got to. This is what separates "we never reached them, so
# nothing was billed" from "it ran, and we lost the result", which is the
# difference between a wasted dollar and a free one.
SUBMIT = "submit"
POLL = "poll"
DOWNLOAD = "download"
SAVE = "save"

_ORDER = {SUBMIT: 0, POLL: 1, DOWNLOAD: 2, SAVE: 3}

# --------------------------------------------------------------------------- #
# Why it failed, in terms of something the user can change
#
# "HTTP 451: Prompt references minors" tells you a rule was broken. It does not
# tell you which of the three texts that get concatenated into a prompt broke
# it, nor that re-rendering it unchanged will fail again for the same money.
# That second half is the whole difference between a log and a diagnosis.
# --------------------------------------------------------------------------- #

CONTENT_POLICY = "content-policy"
OUT_OF_CREDIT = "out-of-credit"
BAD_KEY = "bad-key"
RATE_LIMITED = "rate-limited"
TIMED_OUT = "timed-out"
STOPPED = "stopped"
BAD_REQUEST = "bad-request"
ENGINE_FAILED = "engine-failed"
NETWORK = "network"
UNKNOWN = "unknown"

# Where each kind of prompt actually comes from. A rejection on a character
# portrait and a rejection on a scene need different edits, and sending someone
# to the scene body when the offending words are in a cast description wastes
# the next render too.
_SOURCE = {
    "anchor": ("this character's description on step 2. A reference portrait is "
               "built from the description alone -- no scene text reaches it"),
    "image": ("this scene's prompt body, the shared style block, or the "
              "descriptions of the characters it names. All three are "
              "concatenated into the one string that was sent"),
    "speech": "this scene's narration line",
    "claude": "the story text on step 1",
}

_HINTS = {
    CONTENT_POLICY: (
        "The service judged the words, not the picture, so sending the same "
        "text again fails again and bills again. What to edit: {source}."),
    OUT_OF_CREDIT: (
        "The account is out of credit or has hit a cap. Nothing will render "
        "until that is topped up, which is why the run stopped instead of "
        "retrying."),
    BAD_KEY: (
        "The key was refused. Check RENDERFUL_API_KEY or api_key.txt -- a key "
        "that is present but wrong looks identical to a good one until it is "
        "used."),
    RATE_LIMITED: (
        "Throttled even after backing off. Lower WORKERS in config.py if this "
        "keeps happening: more workers means more requests in the same second."),
    TIMED_OUT: (
        "It was still running when we stopped waiting, so it may yet complete "
        "and be billed. Check the account before re-rendering it."),
    STOPPED: "You cancelled it. Anything already submitted was still billed.",
    BAD_REQUEST: (
        "The request was malformed for this model, usually a parameter the "
        "engine does not accept. Check its entry in engines.json."),
    ENGINE_FAILED: (
        "It was accepted and then failed on their side, so it was billed. "
        "Retrying sometimes works; a prompt that fails twice will not start "
        "working on the third."),
    NETWORK: (
        "It never reached the service, so nothing was billed. Check the "
        "connection and try again."),
    UNKNOWN: "",
}

_HTTP_PREFIX = re.compile(r"^HTTP \d{3}: ")


def _reason_of(error, outcome: str) -> str:
    from . import renderful
    status = getattr(error, "status", None)
    text = str(error).lower()
    if getattr(error, "aborted", False) or outcome == CANCELLED:
        return STOPPED
    if getattr(error, "timed_out", False) or outcome == TIMEOUT:
        return TIMED_OUT
    if status == 451 or "not allowed" in text or "policy" in text:
        return CONTENT_POLICY
    if status == 402 or "limit reached" in text:
        return OUT_OF_CREDIT
    if status in (401, 403):
        return BAD_KEY
    if status == 429:
        return RATE_LIMITED
    if status == 400:
        return BAD_REQUEST
    if "generation failed" in text:
        return ENGINE_FAILED
    if isinstance(error, renderful.FatalAPIError):
        return OUT_OF_CREDIT
    if "urlerror" in type(error).__name__.lower() or "connection" in text:
        return NETWORK
    return UNKNOWN


def explain(error, kind: str = "", outcome: str = FAILED) -> dict:
    """{reason, detail, hint} -- a slug to group by, their sentence, and ours."""
    reason = _reason_of(error, outcome)
    detail = _HTTP_PREFIX.sub("", str(error)).strip()
    hint = _HINTS.get(reason, "")
    if hint and "{source}" in hint:
        hint = hint.format(source=_SOURCE.get(kind, "the text that was sent"))
    return {"reason": reason, "detail": detail, "hint": hint}


# --------------------------------------------------------------------------- #
# Saying it once
#
# Sixteen scenes rejected for one reason is one problem, not sixteen. The money
# still needs sixteen rows, because it was billed sixteen times -- but the
# paragraph explaining it belongs on the first of them and on stdout once.
# --------------------------------------------------------------------------- #

_seen: dict[tuple, int] = {}
_seen_guard = threading.Lock()


def _nth(run_id, reason: str, kind: str, detail: str) -> int:
    """How many times this exact failure has been seen in this run; 1 is first.

    Deduplicated only *within* a run. An attempt outside one is a single call
    that cannot repeat itself, and remembering it forever would silence the same
    problem happening again tomorrow.

    `kind` is part of the key because the advice depends on it: one rejection
    reason arriving on a character portrait and on a scene are two different
    edits, so each needs its own first-and-only explanation. Collapsing them
    would leave whichever came second with a count and no reason.
    """
    if not run_id:
        return 1
    key = (run_id, reason, kind, detail)
    with _seen_guard:
        _seen[key] = _seen.get(key, 0) + 1
        return _seen[key]


def forget(run_id) -> None:
    """Drop a finished run's dedup state so the next one starts fresh."""
    if not run_id:
        return
    with _seen_guard:
        for key in [k for k in _seen if k[0] == run_id]:
            del _seen[key]


class _Stdout(logging.StreamHandler):
    """Writes to whatever sys.stdout is *now*.

    StreamHandler binds its stream at construction, which is wrong for a handler
    installed at import: anything that replaces sys.stdout afterwards -- a test
    harness capturing output, a caller redirecting it -- gets nothing, and the
    log keeps writing to a stream nobody is reading.
    """

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass                            # the property above is the only answer


def install_stdout_handler() -> None:
    """Send this package's log to stdout.

    Without it `logging` falls through to its last-resort handler: stderr, and
    only at WARNING. A failure that costs money should arrive in the same stream
    as the banner that said where the projects live, rather than depending on
    whether uvicorn happened to configure the root logger.
    """
    parent = logging.getLogger("shoulico")
    if any(isinstance(h, _Stdout) for h in parent.handlers):
        return
    handler = _Stdout()
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    parent.addHandler(handler)
    parent.setLevel(logging.INFO)
    # Otherwise every record prints twice the moment anything configures a root
    # handler, which uvicorn --reload does in its child process.
    parent.propagate = False


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(pid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(pid, threading.Lock())


def new_run_id() -> str:
    """Correlates every line of one batch, anchors included."""
    return secrets.token_hex(4)


def file_for(pid: str) -> Path:
    # project_id() rather than a raw join, for the same reason store does it:
    # this is the one place a project id becomes a path in this module.
    return config.PROJECTS_DIR / project_id(pid) / FILE_NAME


def digest(text: str | None) -> str | None:
    """A prompt's fingerprint. Enough to correlate, useless to a reader."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    # Imported lazily from store's clock so tests that freeze time freeze this
    # too -- a ledger with a different idea of "now" than the manifest would be
    # worse than no ledger.
    from . import store
    return store.now()


def _rotate(path: Path) -> None:
    """Size-based, oldest dropped. Best effort: losing a line of the ledger must
    never take down the render that was writing it."""
    try:
        if not path.is_file() or path.stat().st_size < MAX_BYTES:
            return
        oldest = path.with_suffix(f".{KEEP}.jsonl")
        if oldest.is_file():
            oldest.unlink()
        for i in range(KEEP - 1, 0, -1):
            older = path.with_suffix(f".{i}.jsonl")
            if older.is_file():
                older.replace(path.with_suffix(f".{i + 1}.jsonl"))
        path.replace(path.with_suffix(".1.jsonl"))
    except OSError:
        pass


def _append(pid: str, line: dict) -> None:
    path = file_for(pid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(pid):
            _rotate(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # The ledger is an observer. A full disk or a locked file is a reason to
        # write nothing, never a reason to fail the render being observed.
        pass


def record(pid: str, event: str, **fields) -> dict:
    """One workflow milestone. Returns the line, mostly so tests can read it."""
    line = {"ts": _now(), "event": event, "project": pid}
    line.update({k: v for k, v in fields.items() if v is not None})
    _append(pid, line)
    return line


# --------------------------------------------------------------------------- #
# Billable attempts
# --------------------------------------------------------------------------- #

class Attempt:
    """One upstream call that may cost money, recorded however it ends.

    Written on the way out rather than on success, which is the entire point:

        with activity.attempt(pid, "image", run_id=job.run_id, scene=7,
                              estimate=0.04, model=model) as att:
            created = renderful.submit(...)
            att.submitted(created.get("id"))
            status = renderful.wait_for(...)
            ...
            att.ok(cost=status.get("cost"), bytes=len(data))

    An early `return` out of the block, an exception, or simply forgetting all
    end the same way: a line gets written. A billable call that leaves no trace
    is the bug this module exists to prevent, so the context manager will not
    let one happen even if a future edit of the caller forgets to say so.
    """

    def __init__(self, pid: str, kind: str, prompt: str = "", **fields) -> None:
        self.pid = pid
        self.kind = kind
        # Held in memory, never written unless the text itself is what failed.
        self.prompt = prompt
        self.fields = {k: v for k, v in fields.items() if v is not None}
        self.stage = SUBMIT
        self.generation_id: str | None = None
        self.outcome: str | None = None
        self.extra: dict = {}
        self._start = time.monotonic()
        self.written = False

    # -- progress ------------------------------------------------------ #

    def submitted(self, generation_id: str | None) -> None:
        """The request was accepted. From here on it is billed whatever we do."""
        self.generation_id = generation_id
        self.stage = POLL

    def downloading(self) -> None:
        self.stage = DOWNLOAD

    def saving(self) -> None:
        self.stage = SAVE

    # -- verdicts ------------------------------------------------------ #

    def ok(self, **fields) -> None:
        self.finish(OK, **fields)

    def failed(self, error, **fields) -> None:
        outcome = _outcome_of(error)
        self.finish(outcome, error=str(error),
                    error_class=type(error).__name__ if isinstance(error, BaseException)
                    else None,
                    http_status=getattr(error, "status", None),
                    _explained=explain(error, self.kind, outcome), **fields)

    def cancelled(self, **fields) -> None:
        self.finish(CANCELLED, **fields)

    def finish(self, outcome: str, **fields) -> None:
        if self.written:
            return
        self.written = True
        self.outcome = outcome
        # Passed through `failed()`; pulled out here so the diagnosis is applied
        # in one place whichever verdict method was called.
        why = fields.pop("_explained", None)
        line = {
            "ts": _now(),
            "event": "attempt",
            "project": self.pid,
            "kind": self.kind,
            "outcome": outcome,
            "stage": self.stage,
            "generation_id": self.generation_id,
            "latency_ms": int((time.monotonic() - self._start) * 1000),
        }
        line.update(self.fields)
        line.update(self.extra)
        line.update({k: v for k, v in fields.items() if v is not None})
        if why:
            line["reason"] = why["reason"]
            self._diagnose(line, why)
        _append(self.pid, {k: v for k, v in line.items() if v is not None})

    def _diagnose(self, line: dict, why: dict) -> None:
        """Say why, once.

        The count is what turns sixteen rows into one problem. Every row keeps
        its `reason` so the money can still be grouped by cause, but only the
        first carries the explanation and the text that caused it -- and only
        the first is printed.
        """
        nth = _nth(line.get("run_id"), why["reason"], self.kind, why["detail"])
        if nth > 1:
            line["repeat"] = nth        # nth failure of this kind in this run
            return

        line["detail"] = why["detail"]      # their sentence, without the HTTP noise
        line["hint"] = why["hint"]          # ours
        # The one place the raw text is written down, and only when the text is
        # what went wrong. A hash tells you two rows match; it cannot tell you
        # which words to change, and on a content rejection that is the entire
        # question. Set config.LOG_REJECTED_PROMPTS = False to keep the file
        # free of story text at the cost of having to guess.
        if (why["reason"] == CONTENT_POLICY and config.LOG_REJECTED_PROMPTS
                and self.prompt):
            line["prompt"] = self.prompt[:config.LOG_PROMPT_CHARS]

        where = ", ".join(
            str(v) for v in (self.kind,
                             f"scene {line['scene']}" if line.get("scene") else None,
                             line.get("character")) if v)
        log.warning("%s %s: %s", where, why["reason"], why["detail"])
        if why["hint"]:
            log.warning("    %s", why["hint"])
        if line.get("prompt"):
            log.warning("    sent: %s", line["prompt"])


def _outcome_of(error) -> str:
    """Read the verdict off the exception rather than off its message.

    renderful tags what it raises with `.status`, and FatalAPIError is its own
    type, so this is a lookup rather than a regex over our own error strings --
    which would break the first time one of them was reworded.
    """
    from . import renderful
    if isinstance(error, renderful.FatalAPIError):
        return FATAL
    if getattr(error, "timed_out", False):
        return TIMEOUT
    if getattr(error, "aborted", False):
        return CANCELLED
    status = getattr(error, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        return REJECTED
    return FAILED


@contextmanager
def attempt(pid: str, kind: str, prompt: str = "", **fields):
    att = Attempt(pid, kind, prompt=prompt, **fields)
    try:
        yield att
    except BaseException as exc:                       # noqa: BLE001 - re-raised
        att.failed(exc)
        raise
    finally:
        # A caller that returned early without a verdict still gets a line. It
        # is marked so nobody mistakes it for a real classification.
        att.finish(FAILED, error="ended without a recorded outcome")


# --------------------------------------------------------------------------- #
# Reading it back
# --------------------------------------------------------------------------- #

def read(pid: str, limit: int | None = None, run_id: str | None = None) -> list[dict]:
    """Oldest first. `limit` keeps the most recent N, which is what a panel wants."""
    path = file_for(pid)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    # A torn last line after a hard kill. Skip it; the rest of
                    # the file is still perfectly good.
                    continue
                if run_id and line.get("run_id") != run_id:
                    continue
                out.append(line)
    except OSError:
        return []
    return out[-limit:] if limit else out


def was_billed(line: dict) -> bool:
    """Did this attempt cost money, whether or not it produced anything?

    Three cases count, and the middle one is the one worth spelling out:

      * it produced something, so it certainly ran;
      * it got past submission, so a generation ran (`stage` is poll or later)
        even though we never saw the result;
      * it was *rejected* at submission -- a 451 on the content of a prompt is
        a verdict the service reached after reading the request, and this repo's
        history says those are billed.

    A connection error or a bad key at submission is not billed, and counting it
    as waste would overstate the figure the whole report exists to give.
    """
    if line.get("outcome") == OK:
        return True
    if _ORDER.get(line.get("stage"), 0) > _ORDER[SUBMIT]:
        return True
    return line.get("outcome") == REJECTED


def amount(line: dict) -> float:
    """What this line cost, best available. The real figure when the generation
    completed and told us; otherwise the estimate, which is what we were quoted
    for exactly this call."""
    for field in ("cost", "estimate"):
        try:
            value = line.get(field)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def report(pid: str) -> dict:
    """The reconciliation. Small on purpose -- every figure here answers a
    question somebody actually asks, and `wasted` answers the one that used to
    be unanswerable."""
    lines = read(pid)
    attempts = [ln for ln in lines if ln.get("event") == "attempt"]

    outcomes: dict[str, int] = {}
    kinds: dict[str, int] = {}
    estimated = billed = wasted = 0.0
    unpriced = 0
    tokens_in = tokens_out = 0

    for line in attempts:
        outcomes[line.get("outcome", "?")] = outcomes.get(line.get("outcome", "?"), 0) + 1
        kinds[line.get("kind", "?")] = kinds.get(line.get("kind", "?"), 0) + 1
        tokens_in += int(line.get("input_tokens") or 0)
        tokens_out += int(line.get("output_tokens") or 0)
        # Claude is counted in tokens, not dollars. There is no price table in
        # this app for a token, and inventing one so the money column looked
        # complete would make every figure below quietly wrong.
        if line.get("kind") == "claude":
            continue
        try:
            estimated += float(line.get("estimate") or 0.0)
        except (TypeError, ValueError):
            pass
        if not was_billed(line):
            continue
        value = amount(line)
        billed += value
        if line.get("outcome") != OK:
            wasted += value
        # A billed call whose cost we never learned is priced off its estimate.
        # Worth surfacing rather than hiding, because it bounds how much the
        # figures above can be wrong by.
        if line.get("cost") is None:
            unpriced += 1

    runs = {ln.get("run_id") for ln in lines if ln.get("run_id")}
    return {
        "project": pid,
        "events": len(lines),
        "attempts": len(attempts),
        "runs": len(runs),
        "by_outcome": outcomes,
        "by_kind": kinds,
        "estimated": round(estimated, 4),
        "billed": round(billed, 4),
        # The number this whole module was built for: money spent on attempts
        # that produced no usable asset.
        "wasted": round(wasted, 4),
        "waste_ratio": round(wasted / billed, 4) if billed else 0.0,
        "unpriced_attempts": unpriced,
        "claude_input_tokens": tokens_in,
        "claude_output_tokens": tokens_out,
        "first": lines[0]["ts"] if lines else None,
        "last": lines[-1]["ts"] if lines else None,
    }


def clear(pid: str) -> None:
    """Used by tests and by a project delete. Rotated files go too."""
    path = file_for(pid)
    for candidate in [path] + [path.with_suffix(f".{i}.jsonl") for i in range(1, KEEP + 2)]:
        try:
            os.unlink(candidate)
        except OSError:
            pass
