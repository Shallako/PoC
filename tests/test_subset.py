"""Rendering and speaking some of the scenes, in one job.

The page could ask for two things: everything, or one scene from the arrow on
its card. `start()` refuses while a job is running, so four scenes that failed
were four presses and four jobs one after another -- serial, on a machine with
WORKERS workers idle. The endpoints have always taken a list; nothing sent one.

The half of this that spends money is what an *empty* list means. It used to be
read as "all of them", because an empty list is falsy -- which never came up
while the only subset was a single scene from an arrow, and becomes a batch
nobody asked for the moment a selection can be emptied by unticking it.
"""

from __future__ import annotations

from conftest import images, project, render, segmented, speak, wait_for_job

from shoulico import orchestrator


def narrated(client, scenes=4):
    pid = segmented(client, scenes=scenes)
    assert client.post(f"/api/projects/{pid}/narration", json={}).status_code == 200
    return pid


def plan(client, pid, **body):
    r = client.post(f"/api/projects/{pid}/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def plan_audio(client, pid, **body):
    r = client.post(f"/api/projects/{pid}/narration/plan-audio", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- #
# An empty selection is none of them
# --------------------------------------------------------------------- #

def test_an_empty_selection_plans_nothing_rather_than_everything(client, claude):
    pid = segmented(client, scenes=4)
    assert plan(client, pid)["count"] == 4
    assert plan(client, pid, scenes=[])["count"] == 0
    assert plan(client, pid, scenes=[])["render"] == []


def test_an_empty_selection_spends_nothing(client, claude, api):
    """The one that costs money if it is wrong."""
    pid = segmented(client, scenes=4)
    r = client.post(f"/api/projects/{pid}/render",
                    json={"scenes": [], "confirm": True}).json()
    assert r["started"] is False
    assert images(pid) == []
    assert api.submits == [], "an empty selection reached Renderful"


def test_an_empty_selection_speaks_nothing(client, claude, api):
    pid = narrated(client, scenes=4)
    assert plan_audio(client, pid, scenes=[])["count"] == 0
    r = client.post(f"/api/projects/{pid}/narration/speak",
                    json={"scenes": [], "confirm": True}).json()
    assert r["started"] is False


def test_asking_for_nothing_is_not_the_same_as_asking_for_everything(client, claude):
    """The distinction the bug erased: null means "the whole batch", an empty
    list means "none of it", and they are different requests."""
    pid = segmented(client, scenes=3)
    assert plan(client, pid, scenes=None)["count"] == 3
    assert plan(client, pid, scenes=[])["count"] == 0


# --------------------------------------------------------------------- #
# A subset is one job, not several
# --------------------------------------------------------------------- #

def test_a_subset_renders_exactly_those_scenes_in_one_job(client, claude, api):
    pid = segmented(client, scenes=4)
    render(client, pid, scenes=[2, 4])
    job = wait_for_job(pid)

    assert sorted(job.scenes) == [2, 4]
    done = {s["n"]: s for s in project(client, pid)["scenes"]}
    assert done[2]["asset"] and done[4]["asset"]
    assert not done[1].get("asset") and not done[3].get("asset")
    assert len(images(pid)) == 2


def test_a_subset_is_priced_by_the_server_not_by_multiplying(client, claude):
    """The page shows the plan's own estimate for the selection rather than
    working one out. A subset can drag in a character portrait two of the
    chosen scenes share, and page arithmetic would miss it."""
    pid = segmented(client, scenes=4)
    whole = plan(client, pid)
    part = plan(client, pid, scenes=[2, 4])
    assert part["count"] == 2 and whole["count"] == 4
    assert part["estimate"] < whole["estimate"]
    # And the estimate is the server's own multiplication of its own price.
    assert part["estimate"] == round(part["count"] * part["price_per_image"], 4)


def test_a_subset_speaks_exactly_those_lines_in_one_job(client, claude, api):
    pid = narrated(client, scenes=4)
    speak(client, pid, scenes=[1, 3])
    job = wait_for_job(pid, kind=orchestrator.KIND_AUDIO)

    assert sorted(job.scenes) == [1, 3]
    done = {s["n"]: s for s in project(client, pid)["scenes"]}
    assert done[1]["audio"] and done[3]["audio"]
    assert not done[2].get("audio") and not done[4].get("audio")


def test_the_whole_batch_still_means_the_whole_batch(client, claude, api):
    """The path every existing press takes. A selection of all of them collapses
    back to null on the page, so this request has to stay exactly what it was."""
    pid = segmented(client, scenes=3)
    render(client, pid)
    job = wait_for_job(pid)
    assert sorted(job.scenes) == [1, 2, 3]
    assert len(images(pid)) == 3


# --------------------------------------------------------------------- #
# What the table is built from
# --------------------------------------------------------------------- #

def test_a_subset_plan_names_only_the_subset(client, claude):
    """Why the page asks twice. The table has to be the whole plan -- ask for a
    subset and the rows for everything else are simply absent, and there would
    be nothing left to tick to get them back."""
    pid = segmented(client, scenes=4)
    part = plan(client, pid, scenes=[2])
    assert [r["n"] for r in part["render"]] == [2]
    assert [r["n"] for r in plan(client, pid)["render"]] == [1, 2, 3, 4]


def test_a_line_left_out_of_the_selection_is_not_reported_as_missing(client, claude):
    """`missing_narration` is about a scene with no script, not about one that
    was not asked for. Confusing them would put a warning on the panel every
    time somebody narrowed the selection."""
    pid = segmented(client, scenes=3)
    client.post(f"/api/projects/{pid}/narration", json={})
    part = plan_audio(client, pid, scenes=[2])
    assert [r["n"] for r in part["speak"]] == [2]
    assert part["missing_narration"] == []
