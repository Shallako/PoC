"""A scene waits for its own characters, not for everybody's.

The barrier used to be all-or-nothing: every portrait finished before any scene
started. That is the right guarantee stated too broadly -- a landscape scene with
nobody in it queued behind six portraits it never references, and with MAX_CAST
at six and five workers that is two full rounds of nothing happening.

The guarantee that actually matters is narrower and is still enforced: a scene is
never submitted before the portraits *it names* exist, because one that is
renders the wrong face and bills for it.
"""

from __future__ import annotations

from conftest import new_project, render, wait_for_job

from shoulico import config, orchestrator, renderful

CAST = [
    {"name": "Marta", "description": "A woman of thirty-four, cropped grey hair."},
    {"name": "the driver", "description": "A man of fifty in a canvas jacket."},
]


def story_with(scene_cast: list[list[str]]) -> dict:
    """A segmentation whose scenes name exactly the characters given."""
    return {
        "language": {"code": "en", "name": "English", "native_name": "English"},
        "style_profile": "Cel-shaded, warm dusk palette, no lettering.",
        "cast": [dict(c) for c in CAST],
        "scenes": [
            {"ordinal": i, "title": f"Beat {i}", "beat": f"what happens in {i}",
             "prompt": f"Wide shot of beat {i}, a road at dusk.", "cast": names}
            for i, names in enumerate(scene_cast, start=1)
        ],
    }


def segmented_with(client, claude, scene_cast):
    claude.segment = story_with(scene_cast)
    pid = new_project(client)
    assert client.patch(f"/api/projects/{pid}",
                        json={"consistency": "cast"}).status_code == 200
    r = client.post(f"/api/projects/{pid}/segment",
                    json={"scene_count": len(scene_cast)})
    assert r.status_code == 200, r.text
    return pid


def kinds_in_order(api):
    """The run as ("submit"|"done", "anchor"|"scene"), in the order it happened.

    Peak concurrency cannot answer this question -- three scenes overlap with
    each other whether or not they waited for the portraits. What distinguishes
    the old barrier from the new gating is *ordering*: did any scene start
    before the last portrait finished?
    """
    marker = config.ANCHOR_STYLE_SUFFIX[:40]
    return [(what, "anchor" if marker in prompt else "scene")
            for what, prompt in api.timeline]


def test_a_scene_with_nobody_in_it_does_not_wait_for_the_cast(client, claude, api):
    """The change itself. Scene 3 names no one, so it has nothing to wait for and
    must be in flight alongside the portraits rather than behind them."""
    api.polls_before_complete = 4          # hold the portraits open long enough
    pid = segmented_with(client, claude, [["Marta"], ["the driver"], []])

    render(client, pid)
    wait_for_job(pid)

    order = kinds_in_order(api)
    first_scene = order.index(("submit", "scene"))
    last_portrait = len(order) - 1 - order[::-1].index(("done", "anchor"))
    assert first_scene < last_portrait, (
        "the free scene waited for every portrait to finish before starting")


def test_a_scene_still_never_starts_before_its_own_portrait(client, claude, api):
    """The guarantee that must not be lost. A scene that named a character and
    started early would have rendered text-to-image -- wrong face, still billed."""
    api.polls_before_complete = 3
    pid = segmented_with(client, claude, [["Marta"], ["the driver"], ["Marta"]])

    render(client, pid)
    wait_for_job(pid)

    # Two portraits (text-to-image) and three scenes that all carry references.
    with_refs = api.submits_of_type(renderful.GEN_TYPE_IMAGE_REF)
    assert len(with_refs) == 3, "every scene naming a character must send one"
    for payload in with_refs:
        assert payload.get("images"), "an image-to-image call with no reference"


def test_a_scene_waits_only_for_the_character_it_names(client, claude, api):
    api.polls_before_complete = 3
    pid = segmented_with(client, claude, [["Marta"], ["the driver"]])

    render(client, pid)
    wait_for_job(pid)

    body = client.get(f"/api/projects/{pid}").json()
    assert [s["status"] for s in body["scenes"]] == ["done", "done"]
    # Each scene carries exactly its own character's portrait, not both.
    for payload in api.submits_of_type(renderful.GEN_TYPE_IMAGE_REF):
        assert len(payload["images"]) == 1


def test_a_portrait_that_fails_still_releases_the_scenes_behind_it(client, claude, api):
    """A failed anchor must not strand its scenes. references_for drops it and
    the scene falls back to text-to-image, which beats never rendering at all."""
    api.fail_prompt_containing = "cropped grey hair"      # Marta's portrait only
    pid = segmented_with(client, claude, [["Marta"], ["the driver"]])

    render(client, pid)
    wait_for_job(pid)

    scenes = client.get(f"/api/projects/{pid}").json()["scenes"]
    assert [s["status"] for s in scenes] == ["done", "done"], \
        "a lost portrait should not take its scenes down with it"


def test_every_scene_runs_when_there_is_no_cast_at_all(client, claude, api):
    """The no-anchors path still works: as_completed over an empty set must not
    swallow the scenes."""
    pid = segmented_with(client, claude, [[], [], []])

    render(client, pid)
    wait_for_job(pid)

    scenes = client.get(f"/api/projects/{pid}").json()["scenes"]
    assert [s["status"] for s in scenes] == ["done"] * 3


def test_the_token_helper_survives_a_hyphenated_slug():
    assert orchestrator._slug_of("the-lighthouse-keeper@3") == "the-lighthouse-keeper"
    assert orchestrator._slug_of(orchestrator._token("marta", 12)) == "marta"
