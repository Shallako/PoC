"""Character consistency: cast anchors and reference-conditioned scenes.

The bug this whole feature exists to prevent is a face that changes between
scenes. The bug the feature can *introduce* is subtler and worse: a scene left
pointing at a reference portrait that no longer exists, so the picture on screen
is not the picture the project would produce now, and nothing says so. Most of
what is asserted below is that second one.
"""

from __future__ import annotations

import json

import pytest

from shoulico import config, engines, renderful, store

from conftest import new_project, render, segmented, wait_for_job

REF = renderful.GEN_TYPE_IMAGE_REF
T2I = renderful.GEN_TYPE_IMAGE


def cast_of(client, pid) -> list[dict]:
    return client.get(f"/api/projects/{pid}").json()["cast"]


def scenes_of(client, pid) -> list[dict]:
    return client.get(f"/api/projects/{pid}").json()["scenes"]


def plan(client, pid, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Segmentation
# --------------------------------------------------------------------------- #

def test_segmenting_records_the_cast_and_who_is_in_each_scene(client, claude):
    pid = segmented(client, scenes=3, consistency="cast")
    body = client.get(f"/api/projects/{pid}").json()

    assert [c["name"] for c in body["cast"]] == ["the narrator", "the cousin"]
    assert [c["slug"] for c in body["cast"]] == ["the-narrator", "the-cousin"]
    # Beat 2 is the narrator alone in the fake, which is what proves the mapping
    # is per scene rather than per project.
    assert body["scenes"][1]["cast"] == ["the narrator"]
    assert body["scenes"][0]["cast"] == ["the narrator", "the cousin"]


def test_a_character_named_by_a_scene_but_not_in_the_cast_is_dropped(client, claude):
    """An unknown name has no anchor to reference, so carrying it would make
    every consumer of the field re-check it against the cast."""
    def segment(kwargs):
        from fake_claude import default_segment
        data = default_segment(kwargs)
        data["scenes"][0]["cast"] = ["the narrator", "a ghost nobody cast"]
        return data
    claude.segment = segment

    pid = segmented(client, scenes=2, consistency="cast")
    assert scenes_of(client, pid)[0]["cast"] == ["the narrator"]


def test_a_character_no_scene_actually_shows_is_not_anchored(client, claude):
    """An anchor nobody references is a rendered, billed image with no consumer."""
    def segment(kwargs):
        from fake_claude import default_segment
        data = default_segment(kwargs)
        for scene in data["scenes"]:
            scene["cast"] = ["the narrator"]
        return data
    claude.segment = segment

    pid = segmented(client, scenes=2, consistency="cast")
    assert [c["name"] for c in cast_of(client, pid)] == ["the narrator"]


def test_the_cast_is_capped(client, claude):
    """Every entry is one more billed image, and a story claiming a dozen leads
    has mistaken 'appears twice' for 'is a main character'."""
    def segment(kwargs):
        from fake_claude import default_segment
        data = default_segment(kwargs)
        data["cast"] = [{"name": f"person {i}", "description": f"A person of {20 + i}."}
                        for i in range(config.MAX_CAST + 4)]
        for scene in data["scenes"]:
            scene["cast"] = [c["name"] for c in data["cast"]]
        return data
    claude.segment = segment

    pid = segmented(client, scenes=2, consistency="cast")
    assert len(cast_of(client, pid)) == config.MAX_CAST


# --------------------------------------------------------------------------- #
# Planning and price
# --------------------------------------------------------------------------- #

def test_the_plan_prices_the_anchors_as_the_billed_images_they_are(client, claude):
    pid = segmented(client, scenes=3, consistency="cast")
    p = plan(client, pid)

    assert p["consistency_active"] is True
    assert p["anchor_count"] == 2
    assert p["count"] == 3
    # Three scenes plus two portraits, not three.
    assert p["estimate"] == pytest.approx(
        p["price_per_image"] * 5)


def test_consistency_is_on_by_default(client, claude):
    """The shipped default, asserted where it is set rather than trusted."""
    pid = new_project(client)
    assert client.get(f"/api/projects/{pid}").json()["consistency"] == "cast"


def test_an_engine_with_no_reference_model_cannot_claim_consistency(client, claude):
    pid = new_project(client)
    r = client.patch(f"/api/projects/{pid}", json={"engine": "custom"})
    assert r.status_code == 200, r.text
    assert r.json()["consistency"] == "off"
    assert r.json()["supports_references"] is False

    r = client.patch(f"/api/projects/{pid}", json={"consistency": "cast"})
    assert r.status_code == 400
    assert "reference-image model" in r.json()["detail"]


def test_an_unknown_consistency_mode_is_refused(client, claude):
    pid = new_project(client)
    r = client.patch(f"/api/projects/{pid}", json={"consistency": "sometimes"})
    assert r.status_code == 400
    assert "sometimes" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_anchors_render_first_and_scenes_reference_them(client, claude, api):
    pid = segmented(client, scenes=3, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    # Every anchor was submitted before every scene. Not a nicety: a scene sent
    # before its reference exists renders the wrong face and bills for it.
    types = [s["payload"]["type"] for s in api.submits]
    assert types[:2] == [T2I, T2I]
    assert set(types[2:]) == {REF}

    anchors = cast_of(client, pid)
    assert [a["status"] for a in anchors] == ["done", "done"]
    assert all(a["source_url"] for a in anchors)

    # Scene 2 is the narrator alone, so it carries one reference; the others two.
    # Looked up by prompt rather than by position: three workers run the scenes
    # concurrently, so their submission order is genuinely undefined and an
    # order-based assertion here would be flaky rather than strict.
    alone = api.payload_for_prompt("Wide shot of beat 2")
    together = api.payload_for_prompt("Wide shot of beat 1")
    assert alone["images"] == [anchors[0]["source_url"]]
    assert together["images"] == [anchors[0]["source_url"], anchors[1]["source_url"]]


def test_the_reference_model_is_the_sibling_not_the_text_to_image_one(client, claude, api):
    """The model id is the half that bills, so sending the t2i id with images
    attached would be the expensive kind of wrong."""
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    scene_payloads = [s["payload"] for s in api.submits if s["payload"].get("images")]
    assert {p["model"] for p in scene_payloads} == {
        engines.ref_model("seedream-5.0-pro")}
    anchor_payloads = [s["payload"] for s in api.submits if not s["payload"].get("images")]
    assert {p["model"] for p in anchor_payloads} == {"seedream-5.0-pro"}


def test_an_anchor_is_a_square_portrait_whatever_the_story_is(client, claude, api):
    """A 16:9 reference of one standing figure is mostly background, which is the
    part of a reference we least want the model to learn."""
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    anchors = [s["payload"] for s in api.submits if not s["payload"].get("images")]
    assert {p["aspect_ratio"] for p in anchors} == {config.ANCHOR_ASPECT_RATIO}
    scenes = [s["payload"] for s in api.submits if s["payload"].get("images")]
    assert {p["aspect_ratio"] for p in scenes} == {"16:9"}


def test_the_anchor_prompt_describes_a_person_not_a_scene(client, claude, api):
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    # By content, not position: the two anchors are rendered concurrently.
    prompt = api.payload_for_prompt("thin scar")["prompt"]
    assert "Plain flat mid-grey background" in prompt   # the reference framing
    assert "African-Anime" in prompt                    # the shared style block
    # A reference portrait is a person, not a moment in the story.
    assert "porch at dusk" not in prompt


def test_anchors_land_outside_images_so_nothing_counts_them_as_scenes(client, claude, api):
    pid = segmented(client, scenes=3, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    assert len(list(store.images_dir(pid).glob("*"))) == 3
    assert len(list(store.cast_dir(pid).glob("*"))) == 2
    assert all(f.name.startswith(f"{pid}_cast_")
               for f in store.cast_dir(pid).glob("*"))


def test_an_anchor_is_recorded_in_the_manifest_with_its_url(client, claude, api):
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    manifest = json.loads(store.manifest_file(pid).read_text(encoding="utf-8"))
    anchors = {k: v for k, v in manifest.items() if v.get("kind") == "cast"}
    assert len(anchors) == 2
    assert all(rec["source_url"] for rec in anchors.values())
    assert all(rec["cost"] for rec in anchors.values())

    # And each scene records what it was conditioned on.
    scene_recs = [v for v in manifest.values() if v.get("scene")]
    assert all(rec["references"] for rec in scene_recs)


def test_the_spend_counts_the_anchors(client, claude, api):
    pid = segmented(client, scenes=3, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    assert client.get(f"/api/projects/{pid}").json()["spend"]["images"] == 5


def test_a_reference_portrait_is_served(client, claude, api):
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    name = cast_of(client, pid)[0]["asset"]
    assert client.get(f"/api/projects/{pid}/cast/{name}").status_code == 200
    assert client.get(f"/api/projects/{pid}/cast/nope.png").status_code == 404


# --------------------------------------------------------------------------- #
# The dependency -- the part that goes wrong quietly
# --------------------------------------------------------------------------- #

def test_rerunning_with_nothing_changed_spends_nothing(client, claude, api):
    pid = segmented(client, scenes=3, consistency="cast")
    render(client, pid)
    wait_for_job(pid)
    api.reset_counters()

    p = plan(client, pid)
    assert p["count"] == 0 and p["anchor_count"] == 0
    assert api.submit_count == 0


def test_editing_a_character_restages_only_the_scenes_they_are_in(client, claude, api):
    """The whole dependency, in one test. Scene 2 is the narrator alone, so
    changing the cousin must leave it exactly as it was."""
    pid = segmented(client, scenes=3, consistency="cast")
    render(client, pid)
    wait_for_job(pid)
    before = {s["n"]: s["asset"] for s in scenes_of(client, pid)}
    api.reset_counters()

    r = client.patch(f"/api/projects/{pid}", json={
        "cast": [{"slug": "the-cousin", "description": "A broad man of thirty-one "
                                                       "in a blue cap."}]})
    assert r.status_code == 200, r.text

    p = plan(client, pid)
    assert p["anchor_count"] == 1
    assert [a["slug"] for a in p["anchors"]] == ["the-cousin"]
    assert sorted(s["n"] for s in p["render"]) == [1, 3]
    assert [s["reason_key"] for s in p["render"]] == ["restaged", "restaged"]

    render(client, pid)
    wait_for_job(pid)
    after = {s["n"]: s["asset"] for s in scenes_of(client, pid)}
    assert after[2] == before[2]
    assert after[1] != before[1] and after[3] != before[3]


def test_a_scene_whose_anchor_moved_is_marked_dirty_for_the_gallery(client, claude, api):
    """The picture on screen is not the picture this project would produce now,
    and the badge is the only thing that says so."""
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)
    assert [s["dirty"] for s in scenes_of(client, pid)] == [False, False]

    client.patch(f"/api/projects/{pid}", json={
        "cast": [{"slug": "the-narrator", "description": "A lean man of twenty-two, "
                                                         "now clean-shaven."}]})
    scenes = scenes_of(client, pid)
    assert [s["dirty"] for s in scenes] == [True, True]
    assert [s["stale_references"] for s in scenes] == [True, True]


def test_a_scene_records_the_anchors_it_was_actually_rendered_against(client, claude, api):
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    scenes = scenes_of(client, pid)
    assert scenes[0]["asset_refs"] == ["the-narrator@1", "the-cousin@1"]
    assert scenes[1]["asset_refs"] == ["the-narrator@1"]


def test_a_failed_anchor_does_not_take_the_story_down_with_it(client, claude, api):
    """A story rendered without one character pinned is worth more than no story.
    What matters is that the scene records the truth, so it re-stages later
    rather than looking settled forever."""
    api.fail_prompt_containing = "thin scar"   # the narrator's portrait
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    cast = cast_of(client, pid)
    assert cast[0]["status"] == "failed"
    assert [s["status"] for s in scenes_of(client, pid)] == ["done", "done"]

    # Scene 1 kept the cousin only; scene 2 was the narrator alone, so it fell
    # all the way back to text-to-image rather than sending an empty array.
    scenes = scenes_of(client, pid)
    assert scenes[0]["asset_refs"] == ["the-cousin@1"]
    assert scenes[1]["asset_refs"] == []
    assert api.submits[-1]["payload"]["type"] in (T2I, REF)


def test_a_failed_re_render_never_advertises_the_new_version(client, claude, api):
    """The version and the URL are two halves of one token. Bumping the version
    on a failure would leave the character advertising v2 while still holding
    v1's picture, so every scene would be conditioned on last week's face while
    recording that it used this week's -- wrong, and permanently settled."""
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)
    before = {c["slug"]: (c["version"], c["source_url"]) for c in cast_of(client, pid)}

    api.fail_prompt_containing = "clean-shaven"
    client.patch(f"/api/projects/{pid}", json={
        "cast": [{"slug": "the-narrator",
                  "description": "A lean man of twenty-two, now clean-shaven."}]})
    render(client, pid)
    wait_for_job(pid)

    narrator = next(c for c in cast_of(client, pid) if c["slug"] == "the-narrator")
    assert narrator["status"] == "failed"
    # Still v1, still v1's URL: the two halves never disagree.
    assert (narrator["version"], narrator["source_url"]) == before["the-narrator"]
    # And no scene claims a portrait that was never made.
    for scene in scenes_of(client, pid):
        assert "the-narrator@2" not in (scene["asset_refs"] or [])
    # The character is still outstanding, so a later run picks it up.
    assert plan(client, pid)["anchor_count"] == 1


def test_a_scene_with_no_references_is_sent_as_text_to_image(client, claude, api):
    """image-to-image with an empty array is a text-to-image request wearing the
    wrong model id, and the model id is the half that bills."""
    def segment(kwargs):
        from fake_claude import default_segment
        data = default_segment(kwargs)
        data["scenes"][0]["cast"] = []
        return data
    claude.segment = segment

    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    first = api.payload_for_prompt("Wide shot of beat 1")
    assert first["type"] == T2I
    assert "images" not in first


def test_submitting_a_reference_render_with_no_references_is_refused(api):
    with pytest.raises(ValueError, match="at least one reference"):
        renderful.submit("a prompt", "key", "seedream-5.0-pro-i2i", {},
                         gen_type=REF, references=[])


def test_re_segmenting_keeps_a_portrait_already_paid_for(client, claude, api):
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)
    before = {c["slug"]: c["asset"] for c in cast_of(client, pid)}
    api.reset_counters()

    client.post(f"/api/projects/{pid}/segment", json={"scene_count": 2})
    after = {c["slug"]: c["asset"] for c in cast_of(client, pid)}
    assert after == before
    assert plan(client, pid)["anchor_count"] == 0


def test_turning_consistency_off_stops_sending_references(client, claude, api):
    pid = segmented(client, scenes=2, consistency="cast")
    render(client, pid)
    wait_for_job(pid)
    api.reset_counters()

    client.patch(f"/api/projects/{pid}", json={"consistency": "off"})
    p = plan(client, pid, force=True)
    assert p["consistency_active"] is False
    assert p["anchor_count"] == 0
    render(client, pid, force=True)
    wait_for_job(pid)
    assert api.references_sent() == []
