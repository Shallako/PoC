"""Project + engine + Claude-backed endpoints, end to end through the app."""

from __future__ import annotations

import pytest
from conftest import STORY, new_project, project, segmented
from fake_claude import APIError, Reply, default_segment, overloaded
from shoulico import compiler, config, engines, i18n, narration, store


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


def test_the_story_limit_is_published_rather_than_repeated(client, monkeypatch):
    """The page draws its counter and its placeholder from this number.

    It used to carry its own copy in four places, so raising the limit produced
    a server that accepted the story and a counter that turned red on it. The
    browser check asserts the other half -- that the page actually reads this.
    """
    assert client.get("/api/status").json()["max_story_chars"] == config.MAX_STORY_CHARS

    monkeypatch.setattr(config, "MAX_STORY_CHARS", 1234)
    body = client.get("/api/status").json()
    assert body["max_story_chars"] == 1234, "the endpoint must follow the config"

    # And the limit it publishes is the one it actually enforces.
    r = client.post("/api/projects", json={"name": "Too long", "story": "x" * 1235})
    assert r.status_code == 400 and "1234" in r.json()["detail"]


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


# --------------------------------------------------------------------- #
# Engines that also generate video
# --------------------------------------------------------------------- #

CLIP_ENGINES = [("nano-banana-pro", "google-veo-3.1"), ("gpt-image-2", "sora-2")]


@pytest.mark.parametrize("key, clip_model", CLIP_ENGINES)
def test_a_video_engine_declares_its_sibling_and_both_halves_of_the_price(client, key,
                                                                         clip_model):
    entry = client.get("/api/engines").json()["engines"][key]
    assert entry["clip"]["model"] == clip_model
    assert not entry["verified"], "nothing here has been proven against a live account"
    # The clip price has to dwarf the image price, or the confirm dialog lies.
    assert entry["clip"]["price_per_clip"] > entry["price_per_image"] * 5
    assert {i["key"] for i in entry["clip"]["inputs"]} >= {
        "aspect_ratio", "resolution", "duration", "audio", "spoken_language"}


def test_an_image_only_engine_has_no_clip_and_refuses_clip_settings(client):
    entry = client.get("/api/engines").json()["engines"]["seedream-5.0-pro"]
    assert entry["clip"] is None
    pid = new_project(client)
    assert project(client, pid)["clip_params"] == {}
    r = client.patch(f"/api/projects/{pid}", json={"clip_params": {"duration": "8"}})
    assert r.status_code == 400 and "does not generate video" in r.json()["detail"]


def test_clip_settings_are_validated_the_same_way_image_parameters_are(client):
    pid = new_project(client)
    p = client.patch(f"/api/projects/{pid}", json={"engine": "gpt-image-2"}).json()
    assert p["clip_params"]["duration"] == "8" and p["clip_params"]["audio"] is True

    p = client.patch(f"/api/projects/{pid}",
                     json={"clip_params": {"duration": "20", "audio": False}}).json()
    assert p["clip_params"]["duration"] == "20" and p["clip_params"]["audio"] is False

    r = client.patch(f"/api/projects/{pid}", json={"clip_params": {"duration": "7"}})
    assert r.status_code == 400 and "not one of" in r.json()["detail"]
    r = client.patch(f"/api/projects/{pid}", json={"clip_params": {"fps": 30}})
    assert r.status_code == 400 and "unknown parameter" in r.json()["detail"]


def test_switching_engine_replaces_the_clip_settings_rather_than_carrying_them(client):
    """Sora offers 20-second clips; Veo stops at 8. A carried-over 20 would be invalid."""
    pid = new_project(client)
    client.patch(f"/api/projects/{pid}", json={"engine": "gpt-image-2"})
    client.patch(f"/api/projects/{pid}", json={"clip_params": {"duration": "20"}})
    p = client.patch(f"/api/projects/{pid}", json={"engine": "nano-banana-pro"}).json()
    assert p["clip_params"]["duration"] == "8"
    assert p["clip_params"]["resolution"] == "720p"


def test_the_clip_estimate_follows_resolution_and_never_reads_low(client):
    pid = new_project(client)
    client.patch(f"/api/projects/{pid}", json={"engine": "nano-banana-pro"})
    cheap = client.patch(f"/api/projects/{pid}",
                         json={"clip_params": {"resolution": "720p"}}).json()
    dear = client.patch(f"/api/projects/{pid}",
                        json={"clip_params": {"resolution": "1080p"}}).json()
    assert cheap["price_per_clip"] == 2.82 and dear["price_per_clip"] == 5.64
    # Anything off the table falls back to the ceiling, not the floor.
    assert engines.price_per_clip("nano-banana-pro", {"resolution": "4k"}) == 5.64
    assert engines.price_per_clip("seedream-5.0-pro") == 0.0


def test_a_missing_engines_json_is_regenerated_exactly(client):
    """Why it is not in the repository.

    The tracked copy held nothing DEFAULT_REGISTRY does not, so it added nothing
    a fresh clone could not produce -- while the app rewrites the file in place
    whenever a shipped entry's schema_version moves, which showed up as a
    working-tree change nobody made. If this ever stops holding, engines.json
    carries something only the repository has, and untracking it lost it.
    """
    import json
    assert not config.ENGINES_FILE.exists()       # the sandbox starts clean
    client.get("/api/engines")
    text = config.ENGINES_FILE.read_text(encoding="utf-8")
    assert json.loads(text) == engines.DEFAULT_REGISTRY
    # And written the way a migration writes it. Two spellings of one registry
    # means the first migration rewrites every line with an em dash in it, and
    # the diff then says nothing about what actually changed.
    assert json.dumps(engines.DEFAULT_REGISTRY, indent=2, ensure_ascii=False) == text
    assert "\\u2014" not in text                   # the dash itself, not an escape


def test_a_hand_written_engines_json_gains_new_engines_without_losing_edits(client):
    """The file exists, so defaults are never consulted again -- unless we add them."""
    import json
    client.get("/api/engines")                    # materialises the file on disk
    reg = json.loads(config.ENGINES_FILE.read_text(encoding="utf-8"))
    reg["engines"].pop("gpt-image-2")
    reg["engines"]["seedream-5.0-pro"]["name"] = "My Renamed Seedream"
    config.ENGINES_FILE.write_text(json.dumps(reg), encoding="utf-8")

    fresh = engines.registry(reload=True)["engines"]
    assert "gpt-image-2" in fresh, "a newly shipped engine never reached an existing file"
    assert fresh["seedream-5.0-pro"]["name"] == "My Renamed Seedream", "clobbered a user edit"


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


def test_guardrails_strip_quoted_dialogue_on_every_compile(client, claude):
    claude.segment = {
        "style_profile": "Shared style block.",
        "scenes": [{"ordinal": 1, "title": "Deal", "beat": "b",
                    "prompt": 'Four men at a table, "Deal the cards"'}],
    }
    pid = segmented(client, scenes=1)
    prompt = project(client, pid)["scenes"][0]["compiled_prompt"]
    assert "Deal the cards" not in prompt
    assert prompt == "Four men at a table Shared style block."

    # A hand edit cannot reintroduce it either -- the pass runs on every compile.
    p = client.patch(f"/api/projects/{pid}",
                     json={"scenes": [{"n": 1, "body": 'Porch at dusk, "come inside"'}]}).json()
    assert "come inside" not in p["scenes"][0]["compiled_prompt"]


def test_prompt_text_is_otherwise_passed_through_as_written(client, claude):
    """Nothing but quoted dialogue is edited out -- what you type is what is sent."""
    claude.segment = {
        "style_profile": "Shared style block.",
        "scenes": [{"ordinal": 1, "title": "Deal", "beat": "b",
                    "prompt": "Four men at a table --ar 16:9"}],
    }
    pid = segmented(client, scenes=1)
    prompt = project(client, pid)["scenes"][0]["compiled_prompt"]
    assert prompt == "Four men at a table --ar 16:9 Shared style block."


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


# --------------------------------------------------------------------- #
# Overload (HTTP 529) and the rest of the failure ladder
# --------------------------------------------------------------------- #

def test_an_overload_is_ridden_out_and_the_call_still_succeeds(client, claude):
    tries = []

    def busy_at_first(kwargs):
        tries.append(kwargs["model"])
        return overloaded() if len(tries) < 3 else default_segment(kwargs)

    claude.segment = busy_at_first
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})

    assert r.status_code == 200, r.text
    assert tries == [config.DEFAULT_CLAUDE_MODEL] * 3, "it gave up on the first model too early"
    assert r.json()["claude_fell_back"] is False
    assert r.json()["claude_model"] == config.DEFAULT_CLAUDE_MODEL


def test_a_sustained_overload_falls_back_to_the_other_model(client, claude):
    def only_the_fallback_answers(kwargs):
        if kwargs["model"] == config.DEFAULT_CLAUDE_MODEL:
            return overloaded()
        return default_segment(kwargs)

    claude.segment = only_the_fallback_answers
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})

    assert r.status_code == 200, r.text
    p = r.json()
    assert len(p["scenes"]) == 2
    assert p["claude_model"] == config.FALLBACK_CLAUDE_MODEL and p["claude_fell_back"] is True
    # The primary was tried its full allowance before anything else was asked.
    assert len(claude.calls) == compiler.CLAUDE_ATTEMPTS + 1


def test_a_total_overload_is_a_503_that_says_nothing_was_charged(client, claude):
    claude.segment = lambda kwargs: overloaded("req_011CdzqUtHhQBNhyeJ8EdANk")
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "overloaded" in detail and "nothing was charged" in detail
    assert "req_011" not in detail, "the request id belongs in the header, not the sentence"
    assert r.headers["retry-after"] == "30"
    assert r.headers["x-claude-request-id"] == "req_011CdzqUtHhQBNhyeJ8EdANk"
    assert r.headers["x-claude-status"] == "529"
    assert project(client, pid)["scenes"] == []


def test_a_rate_limit_uses_the_servers_own_retry_after(client, claude):
    class Limited(APIError):
        def __init__(self):
            super().__init__(429, "rate_limit_error", "rate limited")
            self.response = type("R", (), {"headers": {"retry-after": "7"}})()

    claude.segment = lambda kwargs: Limited()
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})

    assert r.status_code == 503 and r.headers["retry-after"] == "7"
    assert "rate limited" in r.json()["detail"]


def test_a_rejected_key_fails_immediately_instead_of_laddering(client, claude):
    claude.segment = lambda kwargs: APIError(401, "authentication_error", "invalid x-api-key")
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})

    assert r.status_code == 502 and "rejected the API key" in r.json()["detail"]
    assert len(claude.calls) == 1, "retrying a bad key wastes the user's time"


def test_a_bad_request_is_surfaced_verbatim_and_never_retried(client, claude):
    claude.segment = lambda kwargs: APIError(400, "invalid_request_error", "max_tokens too large")
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})

    assert r.status_code == 502 and "max_tokens too large" in r.json()["detail"]
    assert len(claude.calls) == 1


def test_narration_and_interface_translation_get_the_same_treatment(client, claude):
    pid = segmented(client, scenes=2)

    claude.narration = lambda kwargs: overloaded()
    r = client.post(f"/api/projects/{pid}/narration", json={"seconds_per_scene": 8})
    assert r.status_code == 503 and "overloaded" in r.json()["detail"]

    claude.ui = lambda kwargs: overloaded()
    r = client.post("/api/ui/strings",
                    json={"code": "fr", "name": "French", "strings": {"a": "Render"}})
    assert r.status_code == 503 and "overloaded" in r.json()["detail"]
    assert not i18n.load("fr"), "a failed translation must not be cached"


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


def test_narration_is_asked_for_in_the_language_of_the_story(client, claude):
    claude.segment = {
        "language": {"code": "fr", "name": "French", "native_name": "Français"},
        "style_profile": "Illustration à la ligne claire.",
        "scenes": [{"ordinal": 1, "title": "Le quai", "beat": "b",
                    "prompt": "Plan large du quai à l'aube"}],
    }
    pid = segmented(client, scenes=1)
    assert project(client, pid)["language"]["native_name"] == "Français"

    client.post(f"/api/projects/{pid}/narration", json={})
    user = claude.last_user_turn()
    assert "Write the narration in French (Français)" in user
    assert "Write in the language the story is written in" in claude.last_system()


def test_prompt_language_can_be_forced_to_english_and_is_validated(client, claude):
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/segment", json={"scene_count": 1})
    assert "Exception to the language rule" not in claude.last_user_turn()

    client.patch(f"/api/projects/{pid}", json={"prompt_language": "en"})
    client.post(f"/api/projects/{pid}/segment", json={"scene_count": 1})
    assert "write `prompt` and `style_profile` in English" in claude.last_user_turn()
    assert project(client, pid)["prompt_language"] == "en"

    r = client.patch(f"/api/projects/{pid}", json={"prompt_language": "français"})
    assert r.status_code == 400 and "prompt_language" in r.json()["detail"]


def test_a_story_in_any_script_still_gets_portable_filenames(client, claude):
    claude.segment = {
        "language": {"code": "ja", "name": "Japanese", "native_name": "日本語"},
        "style_profile": "アニメ調",
        "scenes": [{"ordinal": 1, "title": "夜の埠頭", "beat": "b", "prompt": "夜の埠頭"},
                   {"ordinal": 2, "title": "Café à Montréal", "beat": "b", "prompt": "café"}],
    }
    pid = segmented(client, scenes=2)
    slugs = [s["slug"] for s in project(client, pid)["scenes"]]
    assert slugs == ["scene", "cafe-a-montreal"]
    assert all(s.isascii() for s in slugs)


def test_unspaced_scripts_are_measured_in_characters_not_words(client, claude):
    pid = segmented(client, scenes=1)
    p = client.patch(f"/api/projects/{pid}", json={
        "scenes": [{"n": 1, "narration": "夏の終わりに私たちは海へ向かった。負けるはずのない賭けだった。"}],
    }).json()
    scene = p["scenes"][0]
    assert scene["narration_unit"] == "characters" and scene["narration_words"] > 20
    assert scene["narration_seconds"] > 0
    assert narration.measure("two plain words")[1] == "words"


def test_quoted_dialogue_is_stripped_whatever_the_language_quotes_with(client, claude):
    claude.segment = {
        "language": {"code": "fr", "name": "French", "native_name": "Français"},
        "style_profile": "Style partagé.",
        "scenes": [{"ordinal": 1, "title": "Le quai", "beat": "b",
                    "prompt": "Deux hommes sur le quai, « Donne les cartes », à l'aube"}],
    }
    pid = segmented(client, scenes=1)
    prompt = project(client, pid)["scenes"][0]["compiled_prompt"]
    assert "Donne les cartes" not in prompt and "«" not in prompt
    assert prompt.startswith("Deux hommes sur le quai") and "à l'aube" in prompt


# --------------------------------------------------------------------- #
# Interface language
# --------------------------------------------------------------------- #

def test_ui_strings_translate_once_and_then_come_from_the_cache(client, claude):
    body = {"code": "fr", "name": "French", "native_name": "Français",
            "strings": {"nav.1": "1 · Story", "cb.render": "Render — confirm spend"}}
    first = client.post("/api/ui/strings", json=body).json()
    assert first["strings"]["nav.1"] == "[fr] 1 · Story"
    assert first["dir"] == "ltr" and first["translated"] == 2
    assert i18n.cache_file("fr").is_file()

    calls = len(claude.calls)
    again = client.post("/api/ui/strings", json=body).json()
    assert again["strings"] == first["strings"] and again["translated"] == 0
    assert len(claude.calls) == calls                      # no second call

    # Editing one English string re-translates that key only.
    body["strings"]["nav.1"] = "1 · The story"
    third = client.post("/api/ui/strings", json=body).json()
    assert third["translated"] == 1 and third["strings"]["nav.1"] == "[fr] 1 · The story"


def test_english_ui_never_calls_claude_and_rtl_is_reported(client, claude):
    r = client.post("/api/ui/strings", json={"code": "en-GB", "strings": {"a": "b"}}).json()
    assert r == {"code": "en", "dir": "ltr", "strings": {}, "translated": 0, "missing": 0}
    assert claude.calls == []

    r = client.post("/api/ui/strings",
                    json={"code": "ar", "name": "Arabic", "strings": {"a": "b"}}).json()
    assert r["dir"] == "rtl"

    assert client.get("/api/ui/languages").json()["cached"] == ["ar"]
    assert client.delete("/api/ui/strings/ar").json() == {"forgotten": True}
    assert client.get("/api/ui/languages").json()["cached"] == []


def test_ui_translation_failure_is_a_502_and_leaves_no_cache(client, claude):
    claude.ui = Reply(text="", stop_reason="end_turn")
    r = client.post("/api/ui/strings", json={"code": "de", "strings": {"a": "b"}})
    assert r.status_code == 502 and "translate the interface" in r.json()["detail"]
    assert not i18n.cache_file("de").is_file()


def test_narration_files_share_the_image_stem(client, claude):
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    client.patch(f"/api/projects/{pid}", json={"scenes": [{"n": 2, "narration": "  "}]})

    written = client.post(f"/api/projects/{pid}/narration/save", json={}).json()["written"]
    assert written == [f"{pid}_001_beat-1.txt", f"{pid}_full-voiceover.txt"]
    text = (store.narration_dir(pid) / written[0]).read_text(encoding="utf-8")
    assert text.startswith("Line 1.") and text.endswith("\n")
