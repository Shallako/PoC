"""Work that no longer matches what it was made from, and saying so.

The project has always *known* this. plan() will not skip a scene whose prompt
moved; plan_audio() re-speaks a line that was edited after it was spoken. But
that knowledge lived only where the work was made. Step 4 cut a video and step 5
wrote an export folder without consulting it, so a picture kept from before a
prompt was rewritten, and the recording of a sentence that is no longer in the
script, went out under the current project's name and said nothing.

Export is the last place the project can still speak: after it, the files are in
an editor and the only remaining evidence is that something looks wrong.

Nothing here blocks. Keeping a render you liked after tweaking its prompt is a
normal thing to want, and a gate would only be clicked through. These tests are
about the telling, and about the export still happening.
"""

from __future__ import annotations

from conftest import (audio_files, project, render, segmented, speak,
                      wait_for_job)

from shoulico import activity, orchestrator, store


def rendered(client, pid, api, **body):
    render(client, pid, **body)
    wait_for_job(pid)
    return project(client, pid)


def spoken(client, pid, **body):
    speak(client, pid, **body)
    wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
    return project(client, pid)


def ready(client, api, scenes=2):
    """A project with a picture and a voice for every scene: the state somebody
    is in when they open step 5."""
    pid = segmented(client, scenes=scenes)
    assert client.post(f"/api/projects/{pid}/narration", json={}).status_code == 200
    rendered(client, pid, api)
    spoken(client, pid)
    return pid


def edit(client, pid, n, **fields):
    r = client.patch(f"/api/projects/{pid}", json={"scenes": [{"n": n, **fields}]})
    assert r.status_code == 200, r.text
    return r.json()


def scene(p, n):
    return next(s for s in p["scenes"] if s["n"] == n)


def export(client, pid, **body):
    r = client.post(f"/api/projects/{pid}/export", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- #
# An edited line dates its recording
# --------------------------------------------------------------------- #

def test_a_spoken_line_is_not_stale_while_it_still_says_what_was_spoken(client, claude, api):
    pid = ready(client, api, scenes=2)
    p = project(client, pid)
    assert audio_files(pid) and all(not s["audio_dirty"] for s in p["scenes"])
    assert p["stale"]["audio"] == []


def test_editing_a_line_after_it_was_spoken_dates_the_recording(client, claude, api):
    pid = ready(client, api, scenes=2)
    p = edit(client, pid, 2, narration="Something else entirely.")

    assert scene(p, 2)["audio_dirty"] is True
    assert scene(p, 1)["audio_dirty"] is False
    assert p["stale"]["audio"] == [2]
    # The audio file is still there and still playable -- it is the *claim* that
    # it is this project's line that has expired, not the recording.
    assert scene(p, 2)["audio"] in audio_files(pid)


def test_the_server_already_agreed_it_needed_re_speaking(client, claude, api):
    """audio_dirty is not a second opinion. If it ever disagreed with the plan
    the page would badge a line the run would skip, or the reverse."""
    pid = ready(client, api, scenes=2)
    edit(client, pid, 2, narration="Something else entirely.")

    plan = client.post(f"/api/projects/{pid}/narration/plan-audio", json={}).json()
    assert [row["n"] for row in plan["speak"]] == [2]
    assert plan["speak"][0]["reason_key"] == "changed"


def test_a_line_that_was_never_spoken_is_not_called_stale(client, claude, api):
    """Missing and out-of-date are different problems with different fixes, and
    only one of them means the file on disk is lying."""
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    p = project(client, pid)
    assert all(not s["audio_dirty"] for s in p["scenes"])
    assert p["stale"]["unspoken"] == [1, 2]


def test_whitespace_alone_does_not_date_a_recording(client, claude, api):
    """The line is compared the way it was spoken -- stripped -- because that is
    what plan_audio compares. A trailing newline is not a rewrite."""
    pid = ready(client, api, scenes=1)
    line = scene(project(client, pid), 1)["narration"]
    p = edit(client, pid, 1, narration=f"  {line}\n")
    assert scene(p, 1)["audio_dirty"] is False


# --------------------------------------------------------------------- #
# One summary, read by everything downstream
# --------------------------------------------------------------------- #

def test_the_project_publishes_what_it_would_disown(client, claude, api):
    pid = ready(client, api, scenes=3)
    edit(client, pid, 1, body="A completely different beat.")
    p = edit(client, pid, 3, narration="Rewritten after the fact.")

    assert p["stale"] == {"images": [1], "audio": [3],
                          "missing_images": [], "unspoken": []}


def test_a_project_with_nothing_made_yet_reports_every_scene_missing(client, claude):
    pid = segmented(client, scenes=2)
    p = project(client, pid)
    assert p["stale"]["missing_images"] == [1, 2]
    assert p["stale"]["images"] == [] and p["stale"]["audio"] == []


def test_a_fresh_project_has_nothing_to_warn_about(client, claude, api):
    pid = ready(client, api, scenes=2)
    st = project(client, pid)["stale"]
    assert st == {"images": [], "audio": [], "missing_images": [], "unspoken": []}


# --------------------------------------------------------------------- #
# The export says it too, per file
# --------------------------------------------------------------------- #

def test_export_marks_the_rows_it_copied_out_of_date(client, claude, api):
    pid = ready(client, api, scenes=3)
    edit(client, pid, 1, body="A completely different beat.")
    edit(client, pid, 3, narration="Rewritten after the fact.")

    body = export(client, pid)
    rows = {row["scene"]: row for row in body["files"]}
    assert rows[1]["stale_image"] is True and rows[1]["stale_audio"] is False
    assert rows[3]["stale_audio"] is True and rows[3]["stale_image"] is False
    assert rows[2]["stale_image"] is False and rows[2]["stale_audio"] is False
    assert body["stale"]["images"] == [1] and body["stale"]["audio"] == [3]


def test_export_still_exports_all_of_it(client, claude, api):
    """The warning is the feature; refusing would not be. Somebody keeping a
    render they liked after tweaking its prompt is doing a normal thing."""
    pid = ready(client, api, scenes=2)
    edit(client, pid, 1, body="A completely different beat.")

    body = export(client, pid)
    assert len(body["files"]) == 2
    names = {p.name for p in store.export_dir(pid).iterdir()}
    assert f"{pid}_001_beat-1.jpg" in names and f"{pid}_001_beat-1.txt" in names


def test_a_scene_with_no_image_is_reported_rather_than_quietly_dropped(client, claude, api):
    """A twelve-scene project writing nine files is the same failure as shipping
    a stale one, and harder to notice: nothing is wrong with what is there."""
    pid = ready(client, api, scenes=3)
    (store.images_dir(pid) / scene(project(client, pid), 2)["asset"]).unlink()

    body = export(client, pid)
    assert [row["scene"] for row in body["files"]] == [1, 3]
    assert body["stale"]["missing_images"] == [2]


def test_a_line_with_no_audio_is_reported_as_unspoken_not_as_stale(client, claude, api):
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    rendered(client, pid, api)

    body = export(client, pid)
    assert body["stale"]["unspoken"] == [1, 2]
    assert body["stale"]["audio"] == []
    assert all(row["narration"] and not row["audio"] for row in body["files"])


def test_the_export_endpoint_is_told_the_same_staleness_the_page_is(client, claude, api):
    """export() reports what _decorate computed rather than working it out
    again. If the endpoint ever stops decorating, every row reads as fresh --
    silently, which is exactly the failure this was written against."""
    pid = ready(client, api, scenes=2)
    edit(client, pid, 1, body="A completely different beat.")

    page = project(client, pid)["stale"]
    written = export(client, pid)["stale"]
    assert written["images"] == page["images"] == [1]


# --------------------------------------------------------------------- #
# And the ledger keeps it
# --------------------------------------------------------------------- #

def test_the_activity_log_records_that_stale_work_went_out(client, claude, api):
    """Money is not the only thing worth being able to reconstruct afterwards:
    "why does the video show the old picture" is answered here."""
    pid = ready(client, api, scenes=3)
    edit(client, pid, 1, body="A completely different beat.")
    edit(client, pid, 3, narration="Rewritten after the fact.")
    export(client, pid)

    written = [e for e in activity.read(pid, limit=200)
               if e.get("event") == "export.written"]
    assert written and written[-1]["stale_images"] == 1
    assert written[-1]["stale_audio"] == 1
    assert written[-1]["missing_images"] == 0


def test_a_clean_export_says_so_rather_than_saying_nothing(client, claude, api):
    pid = ready(client, api, scenes=2)
    export(client, pid)

    written = [e for e in activity.read(pid, limit=200)
               if e.get("event") == "export.written"][-1]
    assert written["stale_images"] == 0 and written["stale_audio"] == 0
    assert written["files"] == 2
