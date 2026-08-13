"""The spend path: plan, confirm, render, resume, fail, cancel, export.

Every test here drives the real client against the fake Renderful server, so the
retry ladder, the poll loop and the 4xx split all execute.
"""

from __future__ import annotations

import json

from conftest import (images, new_project, project, render, segmented,
                      wait_for_job)
from shoulico import config, orchestrator, store


def _rendered(client, pid, api, **body):
    render(client, pid, **body)
    wait_for_job(pid)
    return project(client, pid)


# --------------------------------------------------------------------- #
# Preview before spend
# --------------------------------------------------------------------- #

def test_plan_prices_the_batch_without_touching_the_api(client, claude, api):
    pid = segmented(client, scenes=3)
    plan = client.post(f"/api/projects/{pid}/plan", json={}).json()
    assert plan["count"] == 3 and plan["estimate"] == 0.27
    assert plan["price_per_image"] == 0.09 and plan["model"] == "seedream-5.0-pro"
    assert plan["verified_engine"] and plan["warnings"] == []
    assert [s["reason"] for s in plan["render"]] == ["not yet rendered"] * 3
    assert api.submit_count == 0


def test_plan_warns_about_values_no_live_account_has_confirmed(client, claude):
    pid = segmented(client, scenes=1)
    client.patch(f"/api/projects/{pid}", json={"params": {"aspect_ratio": "4:3"}})
    plan = client.post(f"/api/projects/{pid}/plan", json={}).json()
    assert plan["warnings"] and "Aspect ratio = 4:3" in plan["warnings"][0]


def test_plan_can_be_narrowed_to_selected_scenes(client, claude):
    pid = segmented(client, scenes=4)
    plan = client.post(f"/api/projects/{pid}/plan", json={"scenes": [2, 4]}).json()
    assert [s["n"] for s in plan["render"]] == [2, 4] and plan["estimate"] == 0.18


def test_render_without_confirmation_spends_nothing(client, claude, api):
    pid = segmented(client, scenes=2)
    r = client.post(f"/api/projects/{pid}/render", json={})
    assert r.status_code == 400 and "confirmation" in r.json()["detail"]
    assert api.submit_count == 0 and images(pid) == []


def test_render_refuses_without_scenes_or_a_key(client, claude, api, monkeypatch):
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/render", json={"confirm": True})
    assert r.status_code == 409 and "Segment" in r.json()["detail"]

    pid = segmented(client, scenes=1)
    monkeypatch.setattr(config, "renderful_key", lambda: None)
    r = client.post(f"/api/projects/{pid}/render", json={"confirm": True})
    assert r.status_code == 409 and "API key" in r.json()["detail"]
    assert api.submit_count == 0


# --------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------- #

def test_full_render_writes_images_manifest_and_spend(client, claude, api):
    pid = segmented(client, scenes=3)
    client.patch(f"/api/projects/{pid}", json={"params": {"seed": 4242}})
    p = _rendered(client, pid, api)

    assert images(pid) == [f"{pid}_{n:03d}_beat-{n}_v01_seed4242.jpg" for n in (1, 2, 3)]
    assert all(s["status"] == "done" and not s["dirty"] for s in p["scenes"])
    assert all(s["asset_prompt"] == s["compiled_prompt"] for s in p["scenes"])
    assert p["spend"] == {"images": 3, "actual": 0.12}

    sent = api.submits[0]["payload"]
    assert sent["model"] == "seedream-5.0-pro" and sent["type"] == "text-to-image"
    assert sent["aspect_ratio"] == "16:9" and sent["resolution"] == "2K"
    assert sent["num_outputs"] == 1 and sent["seed"] == 4242
    assert sent["prompt"].endswith(p["style_profile"])
    assert api.submits[0]["auth"] == "Bearer test-renderful-key"

    manifest = client.get(f"/api/projects/{pid}/manifest").json()
    assert len(manifest) == 3
    entry = manifest[f"{pid}_001_beat-1_v01_seed4242"]
    assert entry["engine"] == "seedream-5.0-pro" and entry["model"] == "seedream-5.0-pro"
    assert entry["seed"] == 4242 and entry["cost"] == 0.04 and entry["version"] == 1
    assert entry["file"] == f"{pid}_001_beat-1_v01_seed4242.jpg"
    assert entry["prompt"] and entry["generation_id"].startswith("gen-")
    assert json.loads(store.manifest_file(pid).read_text(encoding="utf-8")) == manifest


def test_bytes_are_saved_as_delivered_not_as_requested(client, claude, api):
    """output_format=png is what we ask for; Renderful sends JPEG (FR-905)."""
    pid = segmented(client, scenes=1)
    _rendered(client, pid, api)
    assert api.submits[0]["payload"]["output_format"] == "png"
    written = store.images_dir(pid) / images(pid)[0]
    assert written.suffix == ".jpg" and written.read_bytes()[:3] == b"\xff\xd8\xff"


def test_workers_run_in_parallel_but_stay_under_the_cap(client, claude, api):
    api.polls_before_complete = 4
    pid = segmented(client, scenes=6)
    _rendered(client, pid, api)
    assert len(images(pid)) == 6
    assert 2 <= api.peak_in_flight <= config.WORKERS


# --------------------------------------------------------------------- #
# Idempotent resume -- by stored prompt, never by timestamp
# --------------------------------------------------------------------- #

def test_rerunning_skips_scenes_whose_prompt_is_unchanged(client, claude, api):
    pid = segmented(client, scenes=3)
    _rendered(client, pid, api)
    api.reset_counters()

    plan = client.post(f"/api/projects/{pid}/plan", json={}).json()
    assert plan["count"] == 0 and plan["estimate"] == 0.0
    assert "already matches this prompt" in plan["skip"][0]["reason"]

    again = render(client, pid)
    assert again["started"] is False and "Nothing to render" in again["message"]
    assert api.submit_count == 0


def test_editing_one_scene_rerenders_only_it_and_bumps_the_version(client, claude, api):
    pid = segmented(client, scenes=3)
    _rendered(client, pid, api)
    api.reset_counters()

    p = client.patch(f"/api/projects/{pid}",
                     json={"scenes": [{"n": 2, "body": "A rewritten second beat."}]}).json()
    assert p["scenes"][1]["dirty"] and not p["scenes"][0]["dirty"]

    plan = client.post(f"/api/projects/{pid}/plan", json={}).json()
    assert plan["count"] == 1 and plan["render"][0]["reason"] == "prompt changed since last render"

    p = _rendered(client, pid, api)
    assert api.submit_count == 1
    assert api.prompts()[0].startswith("A rewritten second beat.")
    assert p["scenes"][1]["version"] == 2 and not p["scenes"][1]["dirty"]
    assert f"{pid}_002_beat-2_v01.jpg" in images(pid)      # the old render is kept
    assert f"{pid}_002_beat-2_v02.jpg" in images(pid)
    assert len(client.get(f"/api/projects/{pid}/manifest").json()) == 4
    assert p["spend"]["images"] == 4


def test_force_rerenders_everything(client, claude, api):
    pid = segmented(client, scenes=2)
    _rendered(client, pid, api)
    api.reset_counters()
    plan = client.post(f"/api/projects/{pid}/plan", json={"force": True}).json()
    assert plan["count"] == 2 and plan["render"][0]["reason"] == "re-render (forced)"
    _rendered(client, pid, api, force=True)
    assert api.submit_count == 2 and len(images(pid)) == 4


def test_a_missing_file_on_disk_invalidates_the_skip(client, claude, api):
    pid = segmented(client, scenes=1)
    _rendered(client, pid, api)
    (store.images_dir(pid) / images(pid)[0]).unlink()
    assert client.post(f"/api/projects/{pid}/plan", json={}).json()["count"] == 1


# --------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------- #

def test_out_of_credit_stops_the_whole_run(client, claude, api):
    api.submit_error = (402, {"message": "credit limit reached"})
    pid = segmented(client, scenes=6)
    render(client, pid)
    job = wait_for_job(pid)

    assert job.fatal and "402" in job.fatal
    assert api.submit_count <= config.WORKERS       # the rest never got submitted
    statuses = {s["status"] for s in project(client, pid)["scenes"]}
    assert statuses <= {"failed", "pending"} and images(pid) == []


def test_a_rejected_prompt_fails_one_scene_and_the_batch_continues(client, claude, api):
    api.fail_generation.add("gen-2")
    pid = segmented(client, scenes=3)
    p = _rendered(client, pid, api)
    by_n = {s["n"]: s for s in p["scenes"]}
    failed = [s for s in p["scenes"] if s["status"] == "failed"]
    assert len(failed) == 1 and "rejected this prompt" in failed[0]["detail"]
    assert sum(1 for s in p["scenes"] if s["status"] == "done") == 2
    assert p["spend"]["images"] == 2
    assert orchestrator.job_for(pid).fatal is None
    assert len(images(pid)) == 2
    assert by_n[failed[0]["n"]]["asset"] is None


def test_transient_5xx_is_retried(client, claude, api):
    api.submit_flaky = [500, 502]
    pid = segmented(client, scenes=1)
    p = _rendered(client, pid, api)
    assert api.submit_count == 3 and p["scenes"][0]["status"] == "done"


def test_a_persistent_5xx_gives_up_after_the_retries(client, claude, api):
    api.submit_flaky = [500] * 10
    pid = segmented(client, scenes=1)
    p = _rendered(client, pid, api)
    assert api.submit_count == 3
    assert p["scenes"][0]["status"] == "failed" and "3 attempts" in p["scenes"][0]["detail"]


def test_completed_with_no_outputs_is_a_failure_not_a_silent_pass(client, claude, api):
    api.no_outputs = True
    pid = segmented(client, scenes=1)
    p = _rendered(client, pid, api)
    assert p["scenes"][0]["status"] == "failed" and "no outputs" in p["scenes"][0]["detail"]
    assert images(pid) == []


def test_a_dead_asset_url_leaves_no_half_written_file(client, claude, api):
    api.download_status = 404
    pid = segmented(client, scenes=1)
    p = _rendered(client, pid, api)
    assert p["scenes"][0]["status"] == "failed" and "download" in p["scenes"][0]["detail"]
    assert images(pid) == [] and client.get(f"/api/projects/{pid}/manifest").json() == {}


def test_cancel_stops_the_run_and_returns_scenes_to_pending(client, claude, api):
    api.polls_before_complete = 10_000            # never finishes on its own
    pid = segmented(client, scenes=4)
    render(client, pid)

    assert client.post(f"/api/projects/{pid}/render", json={"confirm": True}).status_code == 409
    assert client.delete(f"/api/projects/{pid}").status_code == 409
    assert client.post(f"/api/projects/{pid}/segment", json={}).status_code == 409

    assert client.post(f"/api/projects/{pid}/cancel", json={}).json() == {"cancelling": True}
    wait_for_job(pid)
    p = project(client, pid)
    assert all(s["status"] == "pending" for s in p["scenes"])
    assert images(pid) == [] and p["job"]["running"] is False
    assert client.post(f"/api/projects/{pid}/cancel", json={}).json() == {"cancelling": False}


# --------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------- #

def test_export_pairs_flattened_images_with_narration(client, claude, api):
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    _rendered(client, pid, api)

    body = client.post(f"/api/projects/{pid}/export", json={}).json()
    names = sorted(p.name for p in store.export_dir(pid).iterdir())
    assert names == ["manifest.json",
                     f"{pid}_001_beat-1.jpg", f"{pid}_001_beat-1.txt",
                     f"{pid}_002_beat-2.jpg", f"{pid}_002_beat-2.txt",
                     f"{pid}_full-voiceover.txt"]
    assert body["full_voiceover"] and body["files"][0]["narration"] == f"{pid}_001_beat-1.txt"
    assert json.loads((store.export_dir(pid) / "manifest.json").read_text(encoding="utf-8"))


def test_export_keeps_versioned_names_when_asked_and_clears_stale_files(client, claude, api):
    pid = segmented(client, scenes=1)
    _rendered(client, pid, api)
    client.post(f"/api/projects/{pid}/export", json={})
    (store.export_dir(pid) / "stale.jpg").write_bytes(b"old")

    client.post(f"/api/projects/{pid}/export", json={"flatten": False})
    names = sorted(p.name for p in store.export_dir(pid).iterdir())
    assert "stale.jpg" not in names and f"{pid}_001_beat-1_v01.jpg" in names


def test_images_are_served_back_and_traversal_is_refused(client, claude, api):
    pid = segmented(client, scenes=1)
    _rendered(client, pid, api)
    r = client.get(f"/api/projects/{pid}/image/{images(pid)[0]}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:3] == b"\xff\xd8\xff"

    assert client.get(f"/api/projects/{pid}/image/..%5C..%5Cproject.json").status_code == 404
    assert client.get(f"/api/projects/{pid}/image/nope.jpg").status_code == 404
    assert store.project_file(pid).is_file()


def test_the_estimate_follows_the_resolution(client, claude):
    """1K and 2K are billed differently; an estimate that is too low is the
    dangerous direction, so anything off-table falls back to the dearer price."""
    pid = segmented(client, scenes=2)
    assert client.post(f"/api/projects/{pid}/plan", json={}).json()["estimate"] == 0.18
    client.patch(f"/api/projects/{pid}", json={"params": {"resolution": "1K"}})
    plan = client.post(f"/api/projects/{pid}/plan", json={}).json()
    assert plan["price_per_image"] == 0.045 and plan["estimate"] == 0.09
