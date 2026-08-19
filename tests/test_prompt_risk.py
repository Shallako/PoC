"""Reading the prompts that will actually be sent, before they are paid for.

screening.py was wired to one field: the style direction at step 1, read once,
before Claude has written anything. What is submitted is the *compiled* prompt,
and every part of it is editable afterwards -- the shared style block Claude
writes from that direction, the scene body, the character description. Any of
them can put back exactly the phrase the step-1 check exists to catch, and none
of them was ever looked at again.

The incident behind screening.py cost $2.88 for nothing, because a refused
generation is billed like any other. So the reading happens again on the strings
themselves, at the last moment before the money.
"""

from __future__ import annotations

from conftest import images, render, segmented, wait_for_job

from shoulico import store

# The phrasing from the incident, verbatim: a negative wardrobe instruction with
# no age stated anywhere near it.
BAD = "no pants or jacket, summer outfits"


def styled(client, text, scenes=3):
    pid = segmented(client, scenes=scenes)
    store.mutate(pid, lambda proj: proj.update({"style_profile": text}))
    return pid


def plan(client, pid, **body):
    r = client.post(f"/api/projects/{pid}/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- #
# What it reads
# --------------------------------------------------------------------- #

def test_an_ordinary_plan_says_nothing(client, claude):
    pid = styled(client, "Flat colour, hard shadows, muted palette.")
    risk = plan(client, pid)["risk"]
    assert risk["findings"] == [] and risk["worst"] is None


def test_the_style_block_is_read_even_though_claude_wrote_it(client, claude):
    """The step-1 check never saw this string. Claude produced it, and it is
    hand-editable afterwards -- and it is in every prompt in the batch."""
    pid = styled(client, f"Flat colour, {BAD}.")
    risk = plan(client, pid)["risk"]
    codes = {f["code"] for f in risk["findings"]}
    assert "undress" in codes and "minor-and-undress" in codes
    assert risk["worst"] == "warn"


def test_a_scene_body_is_read_too(client, claude):
    """Nothing screened a scene body before. It is compiled into the prompt the
    same as the style block is -- and unlike the block it is in one prompt, so
    this is also what proves the scene numbers are real."""
    pid = styled(client, "Flat colour, hard shadows.", scenes=3)
    store.mutate(pid, lambda proj: proj["scenes"][1].update(
        {"body": f"A courier at a steel sink, {BAD}."}))
    undress = next(f for f in plan(client, pid)["risk"]["findings"]
                   if f["code"] == "undress")
    assert undress["scenes"] == [2], "read the wrong scene, or read the source field"


def test_it_reads_the_compiled_prompt_not_the_fields_it_came_from(client, claude):
    """The distinction that makes this worth doing. Screening each source field
    separately would miss a phrase that is only dangerous once assembled: the
    undress language in one field and the absent age in another."""
    pid = segmented(client, scenes=2)
    store.mutate(pid, lambda proj: proj.update({"style_profile": "no jacket, summer."}))
    risk = plan(client, pid)["risk"]
    assert "minor-and-undress" in {f["code"] for f in risk["findings"]}


# --------------------------------------------------------------------- #
# Said once
# --------------------------------------------------------------------- #

def test_a_phrase_in_the_style_block_is_reported_once_not_once_per_scene(client, claude):
    """Twelve copies of one problem reads as twelve problems, and buries the one
    fact that matters -- that it is in the block, not in any scene."""
    pid = styled(client, f"Flat colour, {BAD}.", scenes=6)
    risk = plan(client, pid)["risk"]
    undress = [f for f in risk["findings"] if f["code"] == "undress"]
    assert len(undress) == 1
    assert sorted(undress[0]["scenes"]) == [1, 2, 3, 4, 5, 6]


def test_a_finding_carries_the_words_that_triggered_it(client, claude):
    pid = styled(client, f"Flat colour, {BAD}.")
    undress = next(f for f in plan(client, pid)["risk"]["findings"]
                   if f["code"] == "undress")
    assert "no pants" in undress["matched"]
    assert undress["message"] and undress["suggestion"]


def test_a_warning_sorts_above_a_note(client, claude):
    pid = styled(client, f"Lego bricks everywhere, {BAD}.")
    findings = plan(client, pid)["risk"]["findings"]
    assert findings[0]["severity"] == "warn"
    assert {f["code"] for f in findings} >= {"undress", "brand"}


# --------------------------------------------------------------------- #
# It follows the selection, and it never blocks
# --------------------------------------------------------------------- #

def test_leaving_the_risky_scene_out_leaves_the_risk_out(client, claude):
    """Unticking it is a real way to deal with this, so the panel has to agree
    that it worked."""
    pid = styled(client, "Flat colour, hard shadows.", scenes=3)
    store.mutate(pid, lambda proj: proj["scenes"][2].update(
        {"body": f"A courier, {BAD}."}))
    assert plan(client, pid)["risk"]["findings"]
    assert plan(client, pid, scenes=[1, 2])["risk"]["findings"] == []


def test_nothing_is_blocked(client, claude, api):
    """A pre-flight check that refused to let you fly would be worse than the
    problem. It is a guess at a classifier, not the classifier."""
    pid = styled(client, f"Flat colour, {BAD}.")
    assert plan(client, pid)["risk"]["worst"] == "warn"

    render(client, pid)
    wait_for_job(pid)
    assert len(images(pid)) == 3


def test_the_reading_costs_nothing(client, claude, api):
    """It is regexes on strings this process already has. If it ever needs a
    model call it stops being something that can sit in front of a button."""
    pid = styled(client, f"Flat colour, {BAD}.")
    submits, asks = len(api.submits), len(claude.calls)
    plan(client, pid)
    plan(client, pid)
    assert len(api.submits) == submits
    assert len(claude.calls) == asks
