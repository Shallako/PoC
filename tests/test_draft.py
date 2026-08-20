"""The page opens on a blank story, not on whatever was worked on last.

Boot loaded the most recent project, so starting the app put you back inside
the last thing you did -- with its story in the box and its scenes in step 2 --
when the reason to open it is almost always the next story. And "New project"
wrote a directory the moment it was pressed, so changing your mind left one
behind.

A draft is a real project shape with no id. Nothing is written until something
has to be remembered: a setting the server validates, or a story to segment.
"""

from __future__ import annotations

from conftest import new_project

from shoulico import config, store


def test_a_draft_is_offered_without_being_written(client):
    pid = new_project(client)                     # so the directory exists at all
    before = sorted(d.name for d in config.PROJECTS_DIR.iterdir())

    assert client.get("/api/draft").json()["id"] == ""

    assert sorted(d.name for d in config.PROJECTS_DIR.iterdir()) == before == [pid]
    assert [p["id"] for p in client.get("/api/projects").json()] == [pid]


def test_a_draft_is_shaped_like_the_project_it_becomes(client):
    """Assembled in the page instead, a draft would be missing whatever key was
    added last -- and the page would throw on its first redraw, at boot, with
    nothing on screen to say why."""
    draft = client.get("/api/draft").json()
    real = client.get(f"/api/projects/{new_project(client)}").json()
    missing = set(real) - set(draft)
    assert not missing, f"a draft could not be drawn: {sorted(missing)}"


def test_a_draft_starts_empty_and_at_the_server_s_defaults(client):
    draft = client.get("/api/draft").json()
    assert draft["story"] == "" and draft["scenes"] == [] and draft["cast"] == []
    assert draft["scene_count"] == config.DEFAULT_SCENE_COUNT
    assert draft["spend"] == {"images": 0, "lines": 0, "actual": 0.0}
    assert draft["name"] == "", "a draft is unnamed until somebody names it"


def test_a_draft_has_no_job_running(client):
    """Every job lookup is by project id, and a draft's is empty. If this ever
    finds one it is another project's, shown against a story that is not it."""
    draft = client.get("/api/draft").json()
    assert draft["job"] is None
    assert draft["audio_job"] is None and draft["video_job"] is None


def test_asking_twice_gives_the_same_answer_and_still_writes_nothing(client):
    first = client.get("/api/draft").json()
    second = client.get("/api/draft").json()
    for key in ("id", "engine", "params", "scene_count", "consistency"):
        assert first[key] == second[key]
    assert client.get("/api/projects").json() == []


# --------------------------------------------------------------------- #
# One definition of what a new project is
# --------------------------------------------------------------------- #

def test_create_writes_exactly_what_blank_describes(client):
    """store.create() builds its project by calling store.blank(). Two
    definitions drift the moment either gains a key, and the one that drifts
    is the draft, because nothing writes it down."""
    made = store.create("A name", "A story.")
    drafted = store.blank("A name", "A story.", pid=made["id"])
    # created_at/updated_at are stamped per call.
    for stamp in ("created_at", "updated_at"):
        drafted[stamp] = made[stamp]
    assert made == drafted


def test_a_draft_carries_no_directory_with_it(client):
    """create() makes the folders; blank() must not, or asking for a draft
    would leave an empty project behind under whatever id it guessed."""
    new_project(client)                           # the projects dir now exists
    before = sorted(d.name for d in config.PROJECTS_DIR.iterdir())

    store.blank()
    store.blank("Named", "And with a story.")

    assert sorted(d.name for d in config.PROJECTS_DIR.iterdir()) == before
