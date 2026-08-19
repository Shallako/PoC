"""The image count belongs to config.py, and the page is told it.

`min="1" max="40" value="12"` was typed into the markup while config.py held
MAX_SCENE_COUNT and DEFAULT_SCENE_COUNT and /api/status published neither. Two
copies of a number, and the copy that decides is not the copy on screen: tune
the constant and the page keeps offering the old bounds, silently, exactly the
way the story limit used to before it was published rather than repeated.

The server also used to clamp -- ask for eighty scenes and the project quietly
became forty, while the box on screen still said eighty. So the same red
counter on the same panel meant "this will be refused" for the story and "this
will be quietly changed" for the count. It refuses now, in the same words.
"""

from __future__ import annotations

from conftest import new_project, project

from shoulico import config


# --------------------------------------------------------------------- #
# Published, not repeated
# --------------------------------------------------------------------- #

def test_the_page_is_told_both_ends_of_the_control(client):
    status = client.get("/api/status").json()
    assert status["max_scene_count"] == config.MAX_SCENE_COUNT
    assert status["default_scene_count"] == config.DEFAULT_SCENE_COUNT


def test_changing_the_constant_changes_what_is_published(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_SCENE_COUNT", 9)
    monkeypatch.setattr(config, "DEFAULT_SCENE_COUNT", 4)
    status = client.get("/api/status").json()
    assert status["max_scene_count"] == 9 and status["default_scene_count"] == 4


# --------------------------------------------------------------------- #
# Refused, not moved
# --------------------------------------------------------------------- #

def test_asking_for_more_images_than_the_limit_is_refused(client):
    r = client.post("/api/projects", json={"name": "Too many", "story": "A story.",
                                           "scene_count": config.MAX_SCENE_COUNT + 1})
    assert r.status_code == 400
    assert str(config.MAX_SCENE_COUNT) in r.json()["detail"]


def test_asking_for_none_is_refused_too(client):
    r = client.post("/api/projects", json={"name": "None at all", "story": "A story.",
                                           "scene_count": 0})
    assert r.status_code == 400


def test_settings_refuse_an_out_of_range_count_rather_than_moving_it(client):
    pid = new_project(client)
    r = client.patch(f"/api/projects/{pid}", json={"scene_count": 999})
    assert r.status_code == 400
    # And the project is untouched: a refused request changes nothing.
    assert project(client, pid)["scene_count"] == config.DEFAULT_SCENE_COUNT


def test_a_count_inside_the_range_is_stored_exactly_as_asked(client):
    pid = new_project(client)
    p = client.patch(f"/api/projects/{pid}",
                     json={"scene_count": config.MAX_SCENE_COUNT}).json()
    assert p["scene_count"] == config.MAX_SCENE_COUNT
    assert project(client, pid)["scene_count"] == config.MAX_SCENE_COUNT


def test_segmenting_with_a_bad_count_is_refused_before_claude_is_paid(client, claude):
    """The whole reason this is checked at the door: segmentation is the first
    billed call of the run, and a request that was going to be clamped anyway
    should never reach it."""
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment",
                    json={"scene_count": config.MAX_SCENE_COUNT + 1})
    assert r.status_code == 400
    assert claude.calls == []


def test_the_limit_moves_with_the_constant(client, monkeypatch):
    """If the check read its own copy of the number this would pass while the
    page and the server disagreed -- which is the bug, not the fix."""
    pid = new_project(client)
    assert client.patch(f"/api/projects/{pid}", json={"scene_count": 4}).status_code == 200

    monkeypatch.setattr(config, "MAX_SCENE_COUNT", 3)
    assert client.patch(f"/api/projects/{pid}", json={"scene_count": 4}).status_code == 400
    assert client.patch(f"/api/projects/{pid}", json={"scene_count": 3}).status_code == 200


# --------------------------------------------------------------------- #
# The floor under the compiler stays
# --------------------------------------------------------------------- #

def test_the_compiler_still_clamps_whatever_reaches_it(client, claude, monkeypatch):
    """The endpoint is the contract with the caller; the clamp is the floor
    under the Claude call, and it has to hold a project saved before the
    contract existed."""
    pid = new_project(client)
    from shoulico import store
    store.mutate(pid, lambda proj: proj.update({"scene_count": 5000}))

    r = client.post(f"/api/projects/{pid}/segment", json={})
    assert r.status_code == 200, r.text
    asked = claude.calls[0]["messages"][0]["content"]
    assert str(config.MAX_SCENE_COUNT) in asked and "5000" not in asked
