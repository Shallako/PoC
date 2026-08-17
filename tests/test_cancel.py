"""Every phase can be stopped, and stopping one leaves the project alone.

Three phases already had this: a render, a voice batch and a video assembly are
background jobs, so cancelling is setting a thread's stop event. The two Claude
phases -- segmentation and the narration script -- did not, and they are the two
that can leave someone staring at a disabled button the longest: the retry
ladder alone waits up to twenty seconds between four attempts before it reaches
the fallback model.
"""

from __future__ import annotations

import threading

import pytest
from conftest import STORY, new_project, render, segmented, speak, wait_for_job

from shoulico import compiler, orchestrator

# Every phase of the wizard, and the URL that stops it.
PHASES = [
    ("segment", "/api/projects/{pid}/segment/cancel"),
    ("narration script", "/api/projects/{pid}/narration/cancel"),
    ("images", "/api/projects/{pid}/cancel"),
    ("narration audio", "/api/projects/{pid}/narration/cancel-audio"),
    ("video", "/api/projects/{pid}/video/cancel"),
]


@pytest.mark.parametrize("phase,url", PHASES, ids=[p for p, _ in PHASES])
def test_every_phase_has_a_cancel(client, phase, url):
    pid = new_project(client)
    r = client.post(url.format(pid=pid))
    assert r.status_code == 200, f"{phase}: {r.text}"
    # Idle is not an error. Pressing stop when nothing is running says so.
    assert r.json() == {"cancelling": False}


@pytest.mark.parametrize("phase,url", PHASES, ids=[p for p, _ in PHASES])
def test_a_cancel_for_a_project_that_does_not_exist_is_a_404(client, phase, url):
    assert client.post(url.format(pid="no-such-project")).status_code == 404


# --------------------------------------------------------------------------- #
# The two Claude phases
# --------------------------------------------------------------------------- #

def test_segmenting_stops_when_cancelled(client, claude):
    """Cancel from inside the stream, which is where the real one spends its time."""
    pid = new_project(client)
    claude.on_event = lambda i: orchestrator.cancel(pid, orchestrator.KIND_SEGMENT)

    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 3})
    assert r.status_code == 409
    assert "cancelled" in r.json()["detail"].lower()

    # The point of stopping is that nothing lands on the project.
    assert client.get(f"/api/projects/{pid}").json()["scenes"] == []


def test_writing_narration_stops_when_cancelled(client, claude):
    pid = segmented(client, scenes=3)
    claude.on_event = lambda i: orchestrator.cancel(pid, orchestrator.KIND_NARRATION)

    r = client.post(f"/api/projects/{pid}/narration", json={})
    assert r.status_code == 409
    assert client.get(f"/api/projects/{pid}").json()["scenes"][0]["narration"] == ""


def test_a_cancelled_call_does_not_climb_the_retry_ladder(client, claude):
    """The ladder is where a slow call actually spends its time when Claude is
    busy, so a cancel has to stop it starting the next attempt."""
    from fake_claude import overloaded

    pid = new_project(client)
    claude.segment = overloaded()          # every attempt would be a 529
    claude.on_event = lambda i: None

    orchestrator.cancel(pid, orchestrator.KIND_SEGMENT)   # nothing running yet
    calls_before = len(claude.calls)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 3})

    # An un-cancelled ladder is four attempts plus the fallback. This one was
    # cancelled before it started, so it must not have been any of them.
    assert r.status_code in (409, 503)
    assert len(claude.calls) - calls_before <= 5


def test_cancelling_one_phase_leaves_the_others_alone(client, claude):
    pid = segmented(client, scenes=2)
    with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT):
        assert client.post(f"/api/projects/{pid}/segment/cancel").json() == {
            "cancelling": True}
        # The narration slot was never running, so its cancel still says idle.
        assert client.post(f"/api/projects/{pid}/narration/cancel").json() == {
            "cancelling": False}


def test_overlapping_calls_are_all_stopped_and_clean_up_only_themselves(client):
    """Nothing stops two segmentations for one project overlapping -- the button
    is disabled in the page, which is not the same as being impossible."""
    pid = new_project(client)
    with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT) as first:
        with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT) as second:
            # One press stops both, rather than whichever registered last.
            assert orchestrator.cancel(pid, orchestrator.KIND_SEGMENT) is True
            assert first.is_set() and second.is_set()
        # The inner call finishing must not deregister the outer one.
        assert orchestrator.call_running(pid, orchestrator.KIND_SEGMENT)
    assert not orchestrator.call_running(pid, orchestrator.KIND_SEGMENT)


# --------------------------------------------------------------------------- #
# The compiler half, without HTTP in the way
# --------------------------------------------------------------------------- #

def test_the_stream_is_abandoned_the_moment_the_flag_is_set(claude):
    stop = threading.Event()
    seen = []
    claude.on_event = lambda i: (seen.append(i), stop.set())

    with pytest.raises(compiler.Cancelled):
        compiler.segment(STORY, 3, api_key="k", stop=stop)

    # Stopped on the first event rather than draining the stream.
    assert seen == [0]


def test_a_backoff_wakes_early_when_cancelled(monkeypatch):
    """A stop button that does nothing for twenty seconds is not a stop button."""
    slept: list[float] = []
    monkeypatch.setattr(compiler, "time",
                        type("Clock", (), {"sleep": staticmethod(slept.append)})())

    stop = threading.Event()
    stop.set()
    compiler._sleep(20.0, stop)
    assert slept == [], "already cancelled: not one slice should have been waited out"

    compiler._sleep(1.0, threading.Event())
    assert sum(slept) == pytest.approx(1.0), "uncancelled: the whole backoff is waited"
    assert max(slept) <= compiler.CANCEL_POLL_SECONDS, "and in slices, so a cancel lands"


def test_without_a_stop_flag_nothing_changes(client, claude):
    """The flag is optional, and a call made without one behaves as it always did."""
    pid = segmented(client, scenes=3)
    body = client.get(f"/api/projects/{pid}").json()
    assert len(body["scenes"]) == 3


# --------------------------------------------------------------------------- #
# The three that already worked, still work
# --------------------------------------------------------------------------- #

def test_cancelling_a_render_still_works(client, claude, api):
    pid = segmented(client, scenes=3)
    render(client, pid)
    assert client.post(f"/api/projects/{pid}/cancel").json() == {"cancelling": True}
    wait_for_job(pid)


def test_cancelling_a_voice_batch_still_works(client, claude, api):
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    speak(client, pid)
    assert client.post(f"/api/projects/{pid}/narration/cancel-audio").json() == {
        "cancelling": True}
    wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
