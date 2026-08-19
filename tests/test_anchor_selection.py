"""A portrait is rendered for the scenes that reference it.

An anchor is a dependency of the scenes that name that character, and it is a
billed image in its own right. Asking for two scenes used to queue *every*
out-of-date portrait in the cast, including characters neither scene has in it
-- so narrowing a render to save money could quietly cost more than the scenes
it dropped.

Only when a selection was made. Ask for the whole batch and you get the whole
batch, including a portrait no scene currently references -- the batch button is
the only thing that will render one, and segmentation prunes a character nobody
appears with, so that case arrives by editing afterwards.
"""

from __future__ import annotations

from conftest import render, wait_for_job
from test_anchor_gating import CAST, segmented_with

from shoulico import orchestrator, store


def plan(client, pid, **body):
    r = client.post(f"/api/projects/{pid}/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def names(rows):
    return sorted(r["name"] for r in rows)


def two_cast(client, claude):
    """Scene 1 is Marta's, scene 2 is the driver's, scene 3 has nobody in it."""
    return segmented_with(client, claude, [["Marta"], ["the driver"], []])


# --------------------------------------------------------------------- #
# The whole batch is still the whole batch
# --------------------------------------------------------------------- #

def test_the_whole_batch_plans_every_portrait(client, claude, api):
    pid = two_cast(client, claude)
    assert names(plan(client, pid)["anchors"]) == names(CAST)


def test_a_portrait_no_scene_references_is_still_rendered_by_the_whole_batch(
        client, claude, api):
    """The only way to get one, and the reason the whole batch is exempt.

    Segmentation prunes a character nobody appears with, so this arises from
    editing afterwards -- taking a character out of the one scene that had them
    leaves the cast member behind, and the batch button is the only thing that
    will render their face.
    """
    pid = two_cast(client, claude)
    store.mutate(pid, lambda proj: proj["scenes"][1].update({"cast": []}))

    whole = plan(client, pid)
    assert names(whole["anchors"]) == names(CAST), "a portrait became unreachable"
    assert whole["anchors_unused"] == []


# --------------------------------------------------------------------- #
# A selection gets what it needs, and nothing else
# --------------------------------------------------------------------- #

def test_a_selection_plans_only_the_portraits_it_uses(client, claude, api):
    pid = two_cast(client, claude)
    part = plan(client, pid, scenes=[1])
    assert names(part["anchors"]) == ["Marta"]
    assert names(part["anchors_unused"]) == ["the driver"]


def test_a_selection_with_nobody_in_it_plans_no_portraits(client, claude, api):
    pid = two_cast(client, claude)
    part = plan(client, pid, scenes=[3])
    assert part["anchors"] == []
    assert names(part["anchors_unused"]) == names(CAST)


def test_the_ones_left_out_are_reported_rather_than_dropped(client, claude, api):
    """They are still out of date. Silence would read as "nothing to do"."""
    part = plan(client, two_cast(client, claude), scenes=[1])
    left = part["anchors_unused"][0]
    assert left["reason_key"] == "unselected" and left["reason"]
    assert left in part["anchors_skip"], "not in the list the panel reads"


def test_the_estimate_follows(client, claude, api):
    """The money is the whole point: one scene plus one portrait, not one scene
    plus the entire cast."""
    pid = two_cast(client, claude)
    part = plan(client, pid, scenes=[1])
    assert part["count"] == 1 and part["anchor_count"] == 1
    assert part["estimate"] == round(2 * part["price_per_image"], 4)

    whole = plan(client, pid)
    assert whole["estimate"] == round(5 * whole["price_per_image"], 4)


# --------------------------------------------------------------------- #
# And the run does what the plan said
# --------------------------------------------------------------------- #

def test_a_selection_renders_only_the_portraits_it_uses(client, claude, api):
    pid = two_cast(client, claude)
    render(client, pid, scenes=[1])
    wait_for_job(pid)

    cast = {m["name"]: m for m in store.load(pid)["cast"]}
    assert cast["Marta"].get("asset"), "the scene's own portrait was not rendered"
    assert not cast["the driver"].get("asset"), "paid for a face nobody asked for"


def test_the_scene_is_still_rendered_against_its_own_portrait(client, claude, api):
    """The narrowing must not take a dependency with it. A scene submitted
    without its portrait renders the wrong face and bills for it."""
    pid = two_cast(client, claude)
    part = plan(client, pid, scenes=[1])
    row = part["render"][0]
    assert row["references"], "the scene lost its reference"
    assert orchestrator._slug_of(row["references"][0]) == part["anchors"][0]["slug"]


def test_versions_are_unchanged_by_the_narrowing(client, claude, api):
    """The version map covers the whole cast whether or not a portrait is being
    rendered this time -- a scene compares against it to know it is stale."""
    pid = two_cast(client, claude)
    whole = plan(client, pid)
    part = plan(client, pid, scenes=[1])
    by_n = {r["n"]: r for r in whole["render"]}
    assert part["render"][0]["references"] == by_n[1]["references"]
