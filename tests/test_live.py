"""Live integration tests: real Claude, real Renderful, real money.

Run explicitly:

    .venv\\Scripts\\python -m pytest -m live --live -s

Without --live these are skipped, so a plain `pytest` can never spend anything.

Budget: the whole module renders IMAGE_BUDGET images (default 1) at 1K, which is
one image of the Renderful free tier's 10/day, plus one Claude segmentation and
one narration call. The final test asserts the budget was not exceeded, so a
regression that fans out cannot quietly bill you for a batch.
"""

from __future__ import annotations

import os

import pytest
from conftest import images, project, wait_for_job
from fastapi.testclient import TestClient
from shoulico import config, engines, renderful, store
from shoulico.app import app

pytestmark = pytest.mark.live

IMAGE_BUDGET = int(os.environ.get("SHOULICO_LIVE_IMAGE_BUDGET", "1"))

STORY = (
    "We drove to the coast for a card game my cousin swore we could not lose. "
    "We got there at dusk, sat down at midnight, and did not get up until the sun "
    "came back. We lost the first hand and won everything after that."
)

# Cheapest settings that still exercise the real path, and a fixed seed so a
# re-run is comparable.
PARAMS = {"resolution": "1K", "aspect_ratio": "16:9", "seed": 20260812}


def test_live_end_to_end(capsys):
    assert config.renderful_key(), "no Renderful key on this machine"
    assert config.anthropic_key(), "no Anthropic key on this machine"
    assert renderful.RENDERFUL_API_BASE.startswith("https://api.renderful.ai"), \
        "the live tests must run against the real API base"

    with TestClient(app) as client:
        pid = client.post("/api/projects",
                          json={"name": "Live Test", "story": STORY}).json()["id"]
        client.patch(f"/api/projects/{pid}", json={"params": PARAMS})

        # --- Claude: segmentation ------------------------------------- #
        r = client.post(f"/api/projects/{pid}/segment",
                        json={"scene_count": 2,
                              "style_hint": "bold graphic anime linework, warm night palette"})
        assert r.status_code == 200, r.text
        p = r.json()
        assert [s["n"] for s in p["scenes"]] == [1, 2]
        assert p["language"]["code"].lower().startswith("en"), p["language"]
        assert len(p["style_profile"]) > 200, "the style block came back too thin to be useful"
        for scene in p["scenes"]:
            assert scene["slug"] and len(scene["body"]) > 80
            # The guardrails have to hold against whatever the model actually wrote.
            assert "--" not in scene["compiled_prompt"]
            assert '"' not in scene["compiled_prompt"]
            assert scene["compiled_prompt"].endswith(p["style_profile"])

        # --- Claude: narration ---------------------------------------- #
        r = client.post(f"/api/projects/{pid}/narration",
                        json={"voice": "first person, looking back, dry",
                              "seconds_per_scene": 8})
        assert r.status_code == 200, r.text
        p = r.json()
        assert all(4 <= s["narration_words"] <= 60 for s in p["scenes"]), \
            [s["narration_words"] for s in p["scenes"]]
        written = client.post(f"/api/projects/{pid}/narration/save", json={}).json()["written"]
        assert written == [f"{pid}_001_{p['scenes'][0]['slug']}.txt",
                           f"{pid}_002_{p['scenes'][1]['slug']}.txt",
                           f"{pid}_full-voiceover.txt"]

        # --- Renderful: preview, then spend --------------------------- #
        # Renderful's moderation intermittently rejects plainly adult prompts with
        # "451 Prompt references minors" -- seen on one that opened "Exactly two
        # adults ... a man of twenty-eight ... a woman of thirty-four", minutes
        # after a near-identical prompt rendered fine. The app handles that
        # correctly (that scene fails, the batch survives -- covered offline in
        # test_render.py), but a test that pays real money must not flip on a
        # third-party classifier, so the one submission we pay for uses a fixed
        # prompt with no people in it. What Claude actually wrote is asserted
        # above, for free.
        client.patch(f"/api/projects/{pid}", json={
            "style_profile": "Bold graphic anime illustration, thick inked outlines, flat "
                             "cel shading, warm dusk palette. No lettering anywhere.",
            "scenes": [{"n": 1, "body": "A stubby white lighthouse on black rocks above a "
                                        "calm sea at dusk, no people anywhere in the frame, "
                                        "gulls as simple ink strokes, wide shot."}],
        })

        scenes = list(range(1, IMAGE_BUDGET + 1))
        plan = client.post(f"/api/projects/{pid}/plan", json={"scenes": scenes}).json()
        assert plan["count"] == IMAGE_BUDGET
        assert plan["model"] == "seedream-5.0-pro" and plan["verified_engine"]

        assert client.post(f"/api/projects/{pid}/render",
                           json={"scenes": scenes}).status_code == 400   # no confirm

        started = client.post(f"/api/projects/{pid}/render",
                              json={"scenes": scenes, "confirm": True}).json()
        assert started["started"] is True
        job = wait_for_job(pid, timeout=renderful.POLL_TIMEOUT)
        assert job.fatal is None, job.fatal

        p = project(client, pid)
        done = [s for s in p["scenes"] if s["status"] == "done"]
        assert len(done) == IMAGE_BUDGET, [
            (s["n"], s["status"], s["detail"]) for s in p["scenes"]]

        # --- What the real API actually gave back --------------------- #
        scene = done[0]
        stem = f"{pid}_001_{scene['slug']}_v01_seed{PARAMS['seed']}"
        # The contract is that the extension follows the bytes actually delivered
        # (FR-905), not the format we asked for. Renderful has shipped both.
        blob = (store.images_dir(pid) / scene["asset"]).read_bytes()
        kind = renderful.sniff(blob)
        assert kind in ("jpg", "png"), kind
        assert scene["asset"] == f"{stem}.{kind}", scene["asset"]
        assert len(blob) > 20_000, len(blob)
        assert scene["generation_id"] and scene["asset_prompt"] == scene["compiled_prompt"]

        manifest = client.get(f"/api/projects/{pid}/manifest").json()
        entry = manifest[stem]
        assert entry["model"] == "seedream-5.0-pro" and entry["seed"] == PARAMS["seed"]
        assert entry["source_url"].startswith("http") and entry["prompt"]
        assert entry["params"]["resolution"] == "1K"

        # The local estimate is only an estimate; the manifest carries the truth.
        actual = p["spend"]["actual"]
        print(f"\nlive: estimate ${plan['estimate']:.4f}  actual ${actual}  "
              f"cost field {entry['cost']!r}  delivered {kind} ({len(blob)} bytes) "
              f"for output_format={PARAMS.get('output_format', 'png')}")

        # --- Resume must not re-charge -------------------------------- #
        again = client.post(f"/api/projects/{pid}/plan", json={"scenes": scenes}).json()
        assert again["count"] == 0 and "already matches" in again["skip"][0]["reason"]
        assert client.post(f"/api/projects/{pid}/render",
                           json={"scenes": scenes, "confirm": True}).json()["started"] is False

        # --- Export --------------------------------------------------- #
        client.post(f"/api/projects/{pid}/export", json={}).json()
        exported = sorted(f.name for f in store.export_dir(pid).iterdir())
        assert f"{pid}_001_{scene['slug']}.{kind}" in exported
        assert f"{pid}_001_{scene['slug']}.txt" in exported
        assert "manifest.json" in exported

        assert len(images(pid)) == IMAGE_BUDGET, "more images were rendered than budgeted"


def test_live_bad_key_is_fatal_and_costs_nothing():
    """A bad key must trip the fatal path, not the retry ladder. Free to run."""
    params = engines.validate("seedream-5.0-pro", PARAMS)
    with pytest.raises(renderful.FatalAPIError) as excinfo:
        renderful.submit("a single grey square", "sk-not-a-real-key",
                         "seedream-5.0-pro", params)
    assert "401" in str(excinfo.value) or "403" in str(excinfo.value)
