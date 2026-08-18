"""Three changes that buy wall-clock, and the guard rails that make them safe.

Measured before writing any of this: a 12-scene cut encodes in 85s serially at
preset medium and parallelism buys nothing (ffmpeg already saturates the box),
while the polled project endpoint costs 2ms at 20 scenes. So the time is not
where it looks -- it is in waiting on Renderful, and in throwing a whole batch
away over one throttle.
"""

from __future__ import annotations

import pytest
from conftest import segmented, wait_for_job

from shoulico import config, renderful


class _Clock:
    """Records what was slept rather than sleeping it."""

    def __init__(self):
        self.slept: list[float] = []
        self._now = 1000.0

    def sleep(self, seconds):
        self.slept.append(seconds)
        self._now += seconds

    def time(self):
        return self._now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(renderful, "time", c)
    return c


# --------------------------------------------------------------------------- #
# 1. Poll before waiting, not after
# --------------------------------------------------------------------------- #

def test_a_finished_generation_is_not_waited_for_at_all(api, clock):
    """The whole point. Sleeping first cost every generation a full interval
    whether or not it was already done."""
    renderful.RENDERFUL_API_BASE = api.base
    created = renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    clock.slept.clear()

    renderful.wait_for(created["id"], "k", interval=5)
    assert clock.slept == [], "a completed generation should cost no waiting"


def test_an_unfinished_generation_still_waits_between_polls(api, clock):
    api.polls_before_complete = 3
    renderful.RENDERFUL_API_BASE = api.base
    created = renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    clock.slept.clear()

    renderful.wait_for(created["id"], "k", interval=5)
    # Three polls, so two waits between them -- not three.
    assert clock.slept == [5, 5]


def test_the_timeout_still_bounds_a_generation_that_never_finishes(api, clock):
    api.polls_before_complete = 10_000
    renderful.RENDERFUL_API_BASE = api.base
    created = renderful.submit("a prompt", "k", "seedream-5.0-pro", {})

    with pytest.raises(RuntimeError, match="timed out"):
        renderful.wait_for(created["id"], "k", timeout=12, interval=5)


def test_a_cancel_is_still_seen_before_the_first_poll(api, clock):
    renderful.RENDERFUL_API_BASE = api.base
    created = renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    before = len(api.polls)

    with pytest.raises(RuntimeError, match="aborted"):
        renderful.wait_for(created["id"], "k", should_stop=lambda: True)
    assert len(api.polls) == before, "a cancelled wait should not poll at all"


# --------------------------------------------------------------------------- #
# 2. A throttle is a moment, not a verdict
# --------------------------------------------------------------------------- #

def test_a_throttle_is_ridden_out_rather_than_killing_the_run(api, clock):
    api.submit_flaky = [429, 429]
    renderful.RENDERFUL_API_BASE = api.base

    created = renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    assert created["id"], "the third attempt should have gone through"
    assert len(clock.slept) == 2


def test_the_server_is_believed_when_it_says_how_long(api, clock):
    api.submit_flaky = [429]
    api.retry_after = "7"
    renderful.RENDERFUL_API_BASE = api.base

    renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    assert clock.slept == [7.0], "Retry-After should win over the default ladder"


def test_an_absurd_retry_after_is_capped(api, clock):
    api.submit_flaky = [429]
    api.retry_after = "3600"
    renderful.RENDERFUL_API_BASE = api.base

    renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    assert clock.slept == [float(renderful.RATE_LIMIT_MAX_WAIT)]


def test_a_throttle_that_outlasts_the_ladder_still_stops_the_run(api, clock):
    """Absorbing a blip is right; hammering a sustained throttle is not."""
    api.submit_error = (429, {"message": "too many requests"})
    renderful.RENDERFUL_API_BASE = api.base

    with pytest.raises(renderful.FatalAPIError, match="429"):
        renderful.submit("a prompt", "k", "seedream-5.0-pro", {})


@pytest.mark.parametrize("code,message", [
    (401, "bad key"),
    (402, "payment required"),
    (403, "forbidden"),
    (429, "monthly credit limit reached"),
])
def test_the_verdicts_are_still_fatal_on_the_first_attempt(api, clock, code, message):
    """Out of credit and a rejected key do not improve with waiting -- including
    a 429 whose body is really the out-of-credit message."""
    api.submit_error = (code, {"message": message})
    renderful.RENDERFUL_API_BASE = api.base

    with pytest.raises(renderful.FatalAPIError):
        renderful.submit("a prompt", "k", "seedream-5.0-pro", {})
    assert api.submit_count == 1, "a verdict should not be retried"
    assert clock.slept == []


def test_a_throttled_batch_finishes_instead_of_stopping_halfway(client, claude, api):
    """The failure this was really about: one throttle used to leave the rest of
    a batch unrendered."""
    api.submit_flaky = [429]
    pid = segmented(client, scenes=3)
    assert client.post(f"/api/projects/{pid}/render",
                       json={"confirm": True}).status_code == 200
    wait_for_job(pid)

    scenes = client.get(f"/api/projects/{pid}").json()["scenes"]
    assert [s["status"] for s in scenes] == ["done"] * 3


# --------------------------------------------------------------------------- #
# 3. More workers
# --------------------------------------------------------------------------- #

def test_the_pool_is_wide_enough_to_be_worth_it():
    assert config.WORKERS >= 4, "three workers is four rounds for a 12-scene story"


def test_a_batch_actually_runs_the_whole_pool_at_once(client, claude, api):
    """Sized off WORKERS rather than a literal: a six-scene batch cannot show a
    seven-wide pool, so a test with a hardcoded count quietly stops proving
    anything the moment the dial moves."""
    api.polls_before_complete = 3          # hold them open long enough to overlap
    pid = segmented(client, scenes=config.WORKERS + 1)
    assert client.post(f"/api/projects/{pid}/render",
                       json={"confirm": True}).status_code == 200
    wait_for_job(pid)

    assert api.peak_in_flight == config.WORKERS, (
        f"{api.peak_in_flight} generations overlapped, not the full "
        f"{config.WORKERS}: the pool is not being filled")
