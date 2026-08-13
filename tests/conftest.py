"""Fixtures: throwaway projects dir, fake Renderful on loopback, fake Claude.

No test spends money or touches the real accounts, and an autouse guard makes
any non-loopback request an error rather than a charge.
"""

from __future__ import annotations

import sys
import time as _time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
APP_ROOT = TESTS_DIR.parent
for _p in (str(APP_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402
from shoulico import compiler, config, engines, orchestrator, renderful, store  # noqa: E402
from shoulico.app import app  # noqa: E402

import fake_ffmpeg  # noqa: E402
from fake_claude import FakeClaude  # noqa: E402
from fake_renderful import FakeRenderful  # noqa: E402

STORY = (
    "The summer I turned twenty-two, my cousin talked me into driving to the coast "
    "for a card game he swore we could not lose. We got there at dusk. We sat down at "
    "midnight and did not get up until the sun came back. We lost the first hand."
)


class _FastClock:
    """time.sleep with the waits scaled down; time.time left alone."""

    cap = 0.02

    def sleep(self, seconds):
        _time.sleep(min(seconds, self.cap))

    def time(self):
        return _time.time()


def _stop_all_jobs() -> None:
    for job in list(orchestrator._jobs.values()):
        job.stop.set()
        if job.thread is not None:
            job.thread.join(timeout=15)
    orchestrator._jobs.clear()


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true",
                     help="run the live tests: real Renderful + real Claude, real money")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live test: needs --live (spends real money)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def _is_live(request) -> bool:
    return request.node.get_closest_marker("live") is not None


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch, request):
    if _is_live(request):
        return
    real = urllib.request.urlopen

    def guarded(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        host = urllib.parse.urlsplit(url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"test tried to reach {host!r}")
        return real(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch, request):
    # Live tests keep the real keys, the real API base and the real poll cadence;
    # only the projects directory is redirected, so nothing lands in the app dir.
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "ENGINES_FILE", tmp_path / "engines.json")
    monkeypatch.setattr(config, "I18N_DIR", tmp_path / "i18n")
    monkeypatch.setattr(engines, "_cache", None)          # re-materialise per test
    if not _is_live(request):
        monkeypatch.setattr(config, "renderful_key", lambda: "test-renderful-key")
        monkeypatch.setattr(config, "anthropic_key", lambda: "sk-ant-test-key")
        # Unreachable by default: a test that renders must install the fake server.
        monkeypatch.setattr(renderful, "RENDERFUL_API_BASE", "http://127.0.0.1:1/api/v1")
        monkeypatch.setattr(renderful, "time", _FastClock())
        monkeypatch.setattr(renderful, "POLL_SECONDS", 0.01)
        # The Claude retry ladder waits tens of seconds between attempts; the
        # ladder still runs, it just doesn't cost the suite half a minute.
        monkeypatch.setattr(compiler, "time", _FastClock())
    yield
    _stop_all_jobs()
    engines._cache = None


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(compiler, "_client", lambda api_key=None: fake)
    return fake


@pytest.fixture
def ffmpeg(monkeypatch):
    """A fake ffmpeg. Without this fixture the suite behaves as if none is installed."""
    from shoulico import video
    return fake_ffmpeg.install(monkeypatch, video)


@pytest.fixture
def api(monkeypatch):
    fake = FakeRenderful().start()
    monkeypatch.setattr(renderful, "RENDERFUL_API_BASE", fake.base)
    yield fake
    _stop_all_jobs()          # drain workers before the socket goes away
    fake.stop()


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def new_project(client, name="Test Story", story=STORY, **body) -> str:
    r = client.post("/api/projects", json={"name": name, "story": story, **body})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def segmented(client, scenes=3, **body) -> str:
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": scenes, **body})
    assert r.status_code == 200, r.text
    return pid


def render(client, pid, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/render", json={"confirm": True, **body})
    assert r.status_code == 200, r.text
    return r.json()


def speak(client, pid, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/narration/speak",
                    json={"confirm": True, **body})
    assert r.status_code == 200, r.text
    return r.json()


def assemble(client, pid) -> dict:
    """Assembly spends nothing, so unlike render and speak there is no confirm."""
    r = client.post(f"/api/projects/{pid}/video/assemble")
    assert r.status_code == 200, r.text
    return r.json()


def wait_for_job(pid, timeout=60, kind=orchestrator.KIND_RENDER):
    job = orchestrator.job_for(pid, kind)
    assert job is not None, f"no {kind} job was started"
    deadline = _time.time() + timeout
    while job.running and _time.time() < deadline:
        _time.sleep(0.02)
    assert not job.running, f"{kind} job for {pid} still running after {timeout}s"
    return job


def images(pid) -> list[str]:
    d = store.images_dir(pid)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def audio_files(pid) -> list[str]:
    d = store.audio_dir(pid)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def project(client, pid) -> dict:
    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 200, r.text
    return r.json()
