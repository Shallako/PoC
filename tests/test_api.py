"""Project + engine + Claude-backed endpoints, end to end through the app."""

from __future__ import annotations

import pytest
from conftest import STORY, new_project, project, segmented
from fake_claude import Reply
from shoulico import compiler, config, store


# --------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------- #

def test_index_serves_the_wizard(client):
    r = client.get("/")
    assert r.status_code == 200 and "Shoulico" in r.text


def test_status_reports_keys_without_leaking_them(client):
    body = client.get("/api/status").json()
    assert body["keys"] == {"renderful": True, "anthropic": True, "anthropic_warning": None}
    assert "test-renderful-key" not in client.get("/api/status").text


def test_malformed_anthropic_key_is_flagged_not_shown_green(client, monkeypatch):
    monkeypatch.setattr(config, "anthropic_key", lambda: "4f896804-8a6c-42a6")
    keys = client.get("/api/status").json()["keys"]
    assert keys["anthropic"] and "sk-ant-" in keys["anthropic_warning"]


def test_engine_registry_materialises_and_defaults_to_seedream(client):
    body = client.get("/api/engines").json()
    assert body["default"] == "seedream-5.0-pro"
    seedream = body["engines"]["seedream-5.0-pro"]
    assert seedream["verified"] and seedream["price_per_image"] == 0.09
    assert [i["key"] for i in seedream["inputs"]] == [
        "aspect_ratio", "resolution", "seed", "output_format"]
    assert config.ENGINES_FILE.is_file()          # written on first load


# --------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------- #

def test_create_lays_out_the_project_on_disk(client):
    pid = new_project(client, "Boston on MV")
    assert pid == "boston-on-mv"
    for sub in ("images", "narration", "export"):
        assert (store.project_dir(pid) / sub).is_dir()
    assert store.manifest_file(pid).is_file()
    p = project(client, pid)
    assert p["engine"] == "seedream-5.0-pro"
    assert p["params"] == {"aspect_ratio": "16:9", "resolution": "2K",
                           "seed": None, "output_format": "png"}
    assert client.get("/api/projects").json()[0]["id"] == pid


def test_duplicate_names_get_their_own_id(client):
    assert new_project(client, "Same") == "same"
    assert new_project(client, "Same") == "same-2"


def test_create_rejects_empty_name_and_oversized_story(client):
    assert client.post("/api/projects", json={"name": "  "}).status_code == 400
    long_story = "x" * (config.MAX_STORY_CHARS + 1)
    assert client.post("/api/projects",
                       json={"name": "Big", "story": long_story}).status_code == 400


def test_unknown_project_is_404_everywhere(client):
    assert client.get("/api/projects/nope").status_code == 404
    assert client.patch("/api/projects/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/projects/nope").status_code == 404
    assert client.post("/api/projects/nope/plan", json={}).status_code == 404


def test_delete_removes_the_directory(client):
    pid = new_project(client)
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert not store.project_dir(pid).exists()


# --------------------------------------------------------------------- #
# Engine parameters: rejected locally, because a rejected request still bills
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("params, fragment", [
    ({"aspect_ratio": "21:9"}, "not one of"),
    ({"resolution": "8K"}, "not one of"),
    ({"seed": "banana"}, "whole number"),
    ({"seed": -1}, "between"),
    ({"steps": 30}, "unknown parameter"),
])
def test_bad_params_never_reach_the_api(client, params, fragment):
    pid = new_project(client)
    r = client.patch(f"/api/projects/{pid}", json={"params": params})
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_good_params_persist_and_normalise(client):
    pid = new_project(client)
    p = client.patch(f"/api/projects/{pid}",
                     json={"params": {"seed": "12345", "resolution": "1K"}}).json()
    assert p["params"]["seed"] == 12345 and p["params"]["resolution"] == "1K"


def test_switching_engine_resets_params_to_that_engine(client):
    pid = new_project(client)
    client.patch(f"/api/projects/{pid}", json={"params": {"seed": 7}})
    p = client.patch(f"/api/projects/{pid}", json={"engine": "custom"}).json()
    assert p["engine"] == "custom" and p["params"]["seed"] is None
    assert "model_id" in p["params"]
    assert client.patch(f"/api/projects/{pid}",
                        json={"engine": "nope"}).status_code == 400


def test_custom_engine_without_a_model_id_cannot_be_planned(client, claude):
    pid = segmented(client)
    client.patch(f"/api/projects/{pid}", json={"engine": "custom"})
    r = client.post(f"/api/projects/{pid}/plan", json={})
    assert r.status_code == 400 and "Model id" in r.json()["detail"]
    client.patch(f"/api/projects/{pid}", json={"params": {"model_id": "flux-1.1-pro"}})
    assert client.post(f"/api/projects/{pid}/plan", json={}).json()["model"] == "flux-1.1-pro"


# --------------------------------------------------------------------- #
# Segmentation
# --------------------------------------------------------------------- #

def test_segment_compiles_scene_body_then_style_block(client, claude):
    pid = segmented(client, scenes=3)
    p = project(client, pid)
    assert [s["n"] for s in p["scenes"]] == [1, 2, 3]
    assert all(s["slug"] for s in p["scenes"])
    first = p["scenes"][0]
    assert first["compiled_prompt"].startswith("Wide shot of beat 1")
    assert first["compiled_prompt"].endswith(p["style_profile"])
    assert first["prompt_chars"] == len(first["compiled_prompt"])
    assert not first["dirty"]


def test_guardrails_strip_flags_and_quoted_dialogue_on_every_compile(client, claude):
    claude.segment = {
        "style_profile": "Shared style block.",
        "scenes": [{"ordinal": 1, "title": "Deal", "beat": "b",
                    "prompt": 'Four men at a table, "Deal the cards" --ar 16:9 --v 6'}],
    }
    pid = segmented(client, scenes=1)
    prompt = project(client, pid)["scenes"][0]["compiled_prompt"]
    assert "--" not in prompt and "Deal the cards" not in prompt
    assert prompt == "Four men at a table Shared style block."

    # A hand edit cannot reintroduce them either -- the pass runs on every compile.
    p = client.patch(f"/api/projects/{pid}",
                     json={"scenes": [{"n": 1, "body": 'Porch at dusk --stylize 400'}]}).json()
    assert "--stylize" not in p["scenes"][0]["compiled_prompt"]


def test_model_ordinals_are_renumbered_dense_and_ordered(client, claude):
    claude.segment = {
        "style_profile": "S.",
        "scenes": [{"ordinal": 9, "title": "Last", "beat": "b", "prompt": "c"},
                   {"ordinal": 4, "title": "First", "beat": "b", "prompt": "a"}],
    }
    scenes = project(client, segmented(client, scenes=2))["scenes"]
    assert [s["n"] for s in scenes] == [1, 2]
    assert [s["title"] for s in scenes] == ["First", "Last"]


def test_resegmenting_keeps_narration_and_the_rendered_asset(client, claude):
    pid = segmented(client, scenes=2)

    def fake_render(proj):
        proj["scenes"][0].update({"narration": "kept line", "asset": "img_v01.jpg",
                                  "asset_prompt": "old prompt", "version": 3,
                                  "status": "done"})
    store.mutate(pid, fake_render)

    scene = project(client, segmented_again(client, pid))["scenes"][0]
    assert scene["narration"] == "kept line"
    assert scene["asset"] == "img_v01.jpg" and scene["version"] == 3
    # The body changed under it, so the UI must show it as stale.
    assert scene["dirty"] is True


def segmented_again(client, pid):
    assert client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2}).status_code == 200
    return pid


def test_segment_surfaces_claude_failures_without_writing_scenes(client, claude):
    pid = new_project(client, story="")
    assert client.post(f"/api/projects/{pid}/segment", json={}).status_code == 400

    pid = new_project(client)
    claude.segment = Reply(stop_reason="refusal", refusal_category="policy")
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})
    assert r.status_code == 502 and "declined" in r.json()["detail"]

    claude.segment = Reply(text="", stop_reason="end_turn")
    assert client.post(f"/api/projects/{pid}/segment", json={}).status_code == 502
    assert project(client, pid)["scenes"] == []


def test_oversized_story_is_refused_before_the_call(claude):
    with pytest.raises(ValueError, match="limit is"):
        compiler.segment("x" * (config.MAX_STORY_CHARS + 1), 3)
    assert claude.calls == []


# --------------------------------------------------------------------- #
# Narration (script only -- no TTS)
# --------------------------------------------------------------------- #

def test_narration_fills_every_scene_and_estimates_duration(client, claude):
    pid = segmented(client, scenes=3)
    p = client.post(f"/api/projects/{pid}/narration",
                    json={"voice": "dry, looking back", "seconds_per_scene": 8}).json()
    assert all(s["narration"].startswith(f"Line {s['n']}.") for s in p["scenes"])
    assert all(s["narration_words"] > 0 and s["narration_seconds"] > 0 for s in p["scenes"])
    assert p["narration_full"].count("\n\n") == 2
    assert p["narration"]["voice"] == "dry, looking back"

    user = claude.last_user_turn()
    assert "about 20 words" in user and "dry, looking back" in user   # 8s at 150 wpm


def test_narration_falls_back_to_position_when_ordinals_do_not_line_up(client, claude):
    claude.narration = {"lines": [{"ordinal": 71, "text": "one"},
                                  {"ordinal": 72, "text": "two"}]}
    pid = segmented(client, scenes=2)
    p = client.post(f"/api/projects/{pid}/narration", json={}).json()
    assert [s["narration"] for s in p["scenes"]] == ["one", "two"]


def test_narration_requires_scenes_and_clamps_the_pacing(client, claude):
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/narration", json={})
    assert r.status_code == 400 and "Segment" in r.json()["detail"]

    pid = segmented(client, scenes=1)
    p = client.post(f"/api/projects/{pid}/narration", json={"seconds_per_scene": 900}).json()
    assert p["narration"]["seconds_per_scene"] == 60


def test_narration_files_share_the_image_stem(client, claude):
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    client.patch(f"/api/projects/{pid}", json={"scenes": [{"n": 2, "narration": "  "}]})

    written = client.post(f"/api/projects/{pid}/narration/save", json={}).json()["written"]
    assert written == [f"{pid}_001_beat-1.txt", f"{pid}_full-voiceover.txt"]
    text = (store.narration_dir(pid) / written[0]).read_text(encoding="utf-8")
    assert text.startswith("Line 1.") and text.endswith("\n")
