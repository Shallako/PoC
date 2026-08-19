"""What the page is told while Claude is overloaded.

A 529 is Anthropic at capacity: the model never ran, so nothing is charged, and
it is usually brief. That is why the server waits and tries again rather than
failing. The cost of that decision is that the whole ladder happens inside the
one POST that started it, so from the browser it used to be indistinguishable
from a very slow answer -- a disabled button for a minute or more with no
explanation.

The activity log has one of these: mv-boston's last segmentation, fell_back to
Sonnet, and nothing anywhere said why.

So these tests are about the ladder being *legible while it is running*, which
means reading it from a second request while the first is still open.
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import new_project, segmented
from fake_claude import overloaded
from shoulico import compiler, config, orchestrator


def collector():
    """The progress callback is called with keywords, the way orchestrator.Call
    receives it. Returns (list, callback)."""
    seen: list[dict] = []
    return seen, lambda **fields: seen.append(fields)


# --------------------------------------------------------------------- #
# The numbers belong to config.py
# --------------------------------------------------------------------- #

def test_the_ladder_is_parameterised_in_one_place():
    """compiler used to hold its own copies. A page that quotes them has to be
    reading the same constants the server retries on."""
    assert compiler.CLAUDE_ATTEMPTS is config.CLAUDE_ATTEMPTS
    assert compiler.CLAUDE_BACKOFF is config.CLAUDE_BACKOFF
    assert compiler.SDK_RETRIES is config.CLAUDE_SDK_RETRIES


def test_patience_is_the_waiting_the_constants_actually_produce():
    """4 attempts on the model plus one fallback is four gaps: 3 + 8 + 20 + 20."""
    assert config.claude_patience_seconds() == 51.0


def test_changing_the_constants_changes_the_answer(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_ATTEMPTS", 2)
    monkeypatch.setattr(config, "CLAUDE_BACKOFF", (5,))
    assert config.claude_patience_seconds() == 10.0      # one gap, plus fallback

    monkeypatch.setattr(config, "FALLBACK_CLAUDE_MODEL", "")
    assert config.claude_patience_seconds() == 5.0


def test_the_page_is_told_the_ladder_rather_than_repeating_it(client):
    status = client.get("/api/status").json()
    assert status["claude_attempts"] == config.CLAUDE_ATTEMPTS
    assert status["claude_backoff"] == list(config.CLAUDE_BACKOFF)
    assert status["claude_patience"] == config.claude_patience_seconds()
    assert status["claude_fallback"] == config.FALLBACK_CLAUDE_MODEL


def test_the_server_honours_retry_after_but_caps_it(monkeypatch):
    """Anthropic's own answer to "how long" is better than our ladder, up to the
    point where a parked worker is indistinguishable from a hung one."""
    class Resp:
        headers = {"retry-after": "9999"}

    exc = RuntimeError("busy")
    exc.response = Resp()
    assert compiler._retry_after(exc) == config.CLAUDE_RETRY_AFTER_MAX

    monkeypatch.setattr(config, "CLAUDE_RETRY_AFTER_MAX", 30.0)
    assert compiler._retry_after(exc) == 30.0


# --------------------------------------------------------------------- #
# The progress channel
# --------------------------------------------------------------------- #

def test_a_call_reports_nothing_until_it_has_something_to_say(client, claude):
    pid = new_project(client)
    assert client.get(f"/api/projects/{pid}/claude/segment").json() == {"running": False}

    with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT):
        assert client.get(f"/api/projects/{pid}/claude/segment").json() == {
            "running": True}


def test_a_note_replaces_rather_than_merges():
    """Each note describes the whole situation. A leftover retry_at from the
    last wait would have the page counting down during a live call."""
    call = orchestrator.Call()
    call.note(phase="waiting", attempt=2, retry_at=123.0)
    call.note(phase="calling", attempt=2)
    assert call.state() == {"phase": "calling", "attempt": 2}


def test_the_deadline_is_served_as_a_countdown(client, claude):
    """The page should not have to agree with this machine about what time it
    is, so the server subtracts rather than publishing an epoch."""
    pid = new_project(client)
    with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT) as call:
        call.note(phase="waiting", attempt=2, of=5, reason="overloaded",
                  seconds=8, retry_at=time.time() + 8)
        body = client.get(f"/api/projects/{pid}/claude/segment").json()

    assert "retry_at" not in body
    assert 7.0 <= body["retry_in"] <= 8.0
    assert body["phase"] == "waiting" and body["reason"] == "overloaded"


def test_a_countdown_that_has_run_out_reads_as_zero_not_as_negative(client, claude):
    pid = new_project(client)
    with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT) as call:
        call.note(phase="waiting", retry_at=time.time() - 30)
        assert client.get(f"/api/projects/{pid}/claude/segment").json()["retry_in"] == 0.0


def test_only_the_two_claude_phases_have_progress(client, claude):
    pid = new_project(client)
    assert client.get(f"/api/projects/{pid}/claude/render").status_code == 404
    assert client.get(f"/api/projects/{pid}/claude/narration").status_code == 200
    assert client.get("/api/projects/nope/claude/segment").status_code == 404


def test_a_live_call_wins_over_an_idle_one(client, claude):
    """Two segmentations can overlap, and there is one progress line to put them
    on. A registration with nothing to say must not blank out a countdown."""
    pid = new_project(client)
    with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT) as first:
        first.note(phase="waiting", attempt=3, reason="overloaded")
        with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT):
            body = client.get(f"/api/projects/{pid}/claude/segment").json()
    assert body["phase"] == "waiting" and body["attempt"] == 3


# --------------------------------------------------------------------- #
# The ladder, reported while it runs
# --------------------------------------------------------------------- #

def test_the_retry_ladder_says_what_it_is_doing_at_every_step(claude):
    """Driven through the real compiler, so what is asserted is what a browser
    would actually be shown -- not a reconstruction of it."""
    seen, note = collector()
    calls = {"n": 0}

    def flaky(kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise overloaded()
        return {"language": {"code": "en", "name": "English", "native_name": "English"},
                "style_profile": "x", "cast": [],
                "scenes": [{"ordinal": 1, "title": "T", "beat": "b", "prompt": "p",
                            "cast": []}]}
    claude.segment = flaky

    compiler.segment("A short story about a courier.", 1, progress=note)

    phases = [(s["phase"], s["attempt"]) for s in seen]
    # calling 1, then waiting before 2, calling 2, waiting before 3, calling 3.
    assert phases == [("calling", 1), ("waiting", 2), ("calling", 2),
                      ("waiting", 3), ("calling", 3)]

    waits = [s for s in seen if s["phase"] == "waiting"]
    assert [w["reason"] for w in waits] == ["overloaded", "overloaded"]
    # The backoff the user is quoted is the one actually slept.
    assert [w["seconds"] for w in waits] == list(config.CLAUDE_BACKOFF[:2])
    assert all(w["retry_at"] > time.time() - 60 for w in waits)
    # Every step knows how many there are, so "attempt 2 of 5" is possible.
    assert {s["of"] for s in seen} == {config.CLAUDE_ATTEMPTS + 1}


def test_the_wait_is_announced_before_it_is_slept_not_after(claude, monkeypatch):
    """A countdown that appears once the wait is over is a log entry."""
    order: list[str] = []
    monkeypatch.setattr(compiler, "_sleep",
                        lambda seconds, stop: order.append("slept"))

    calls = {"n": 0}

    def flaky(kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise overloaded()
        return {"language": {"code": "en", "name": "English", "native_name": "English"},
                "style_profile": "x", "cast": [],
                "scenes": [{"ordinal": 1, "title": "T", "beat": "b", "prompt": "p",
                            "cast": []}]}
    claude.segment = flaky

    def note(**fields):
        if fields.get("phase") == "waiting":
            order.append("announced")
    compiler.segment("A short story about a courier.", 1, progress=note)

    assert order == ["announced", "slept"]


def test_falling_back_to_another_model_is_flagged_as_it_happens(claude):
    """It used to be knowable only afterwards, from a boolean on the project.
    While it is happening is when somebody might want to cancel instead."""
    def always_busy(kwargs):
        if kwargs["model"] == config.SEGMENT_MODEL:
            raise overloaded()
        return {"language": {"code": "en", "name": "English", "native_name": "English"},
                "style_profile": "x", "cast": [],
                "scenes": [{"ordinal": 1, "title": "T", "beat": "b", "prompt": "p",
                            "cast": []}]}
    claude.segment = always_busy

    seen, note = collector()
    compiler.segment("A short story about a courier.", 1, progress=note)

    on_the_model = [s for s in seen if not s["falling_back"]]
    fell_back = [s for s in seen if s["falling_back"]]
    assert on_the_model and fell_back
    assert {s["model"] for s in fell_back} == {config.FALLBACK_CLAUDE_MODEL}
    # And it is flagged during the wait that precedes it, not only once the
    # different model has already answered.
    assert fell_back[0]["phase"] == "waiting"


def test_the_page_can_read_the_ladder_while_the_request_is_still_open(client, claude):
    """The whole point. A second request has to see the first one waiting --
    if this passes only after the POST returns, the feature does not exist."""
    pid = segmented(client, scenes=1)
    reached = threading.Event()
    release = threading.Event()

    def stuck(kwargs):
        reached.set()
        release.wait(timeout=10)
        raise overloaded()
    claude.segment = stuck

    result: dict = {}

    def run():
        result["r"] = client.post(f"/api/projects/{pid}/segment",
                                  json={"scene_count": 1})
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert reached.wait(timeout=10), "the segmentation never started"

    live = client.get(f"/api/projects/{pid}/claude/segment").json()
    assert live["running"] is True
    assert live["phase"] == "calling" and live["attempt"] == 1
    assert live["model"] == config.SEGMENT_MODEL

    release.set()
    worker.join(timeout=30)
    assert not worker.is_alive()

    # And it is gone once the call is over, so a stale countdown cannot linger.
    assert client.get(f"/api/projects/{pid}/claude/segment").json() == {"running": False}


def test_a_cancel_during_a_wait_still_stops_it(client, claude):
    """The wait is the longest part of a busy ladder, so it is where cancel
    matters most -- and Call grew a second half without losing its first."""
    call = orchestrator.Call()
    assert not call.is_set()
    call.note(phase="waiting", attempt=2)
    call.set()
    assert call.is_set()
    assert call.state()["phase"] == "waiting"     # still legible after stopping
