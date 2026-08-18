"""The business activity log.

The thing worth proving here is not that a successful render writes a line --
the manifest already recorded those. It is that **the failures do**, because
that was the gap: money spent with nothing to show for it and nothing written
down. So most of what follows drives a real failure through the real client
against the fake server and then reads the ledger back.
"""

from __future__ import annotations

import json

from conftest import (assemble, images, new_project, project, render,
                      segmented, speak, wait_for_job)
from shoulico import activity, config, orchestrator, renderful, store


def lines(pid, event=None, kind=None) -> list[dict]:
    out = activity.read(pid)
    if event:
        out = [ln for ln in out if ln.get("event") == event]
    if kind:
        out = [ln for ln in out if ln.get("kind") == kind]
    return out


def attempts(pid, kind=None) -> list[dict]:
    return lines(pid, event="attempt", kind=kind)


def narrated(client, pid) -> dict:
    r = client.post(f"/api/projects/{pid}/narration", json={})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------- #

def test_the_ledger_lives_beside_the_manifest_and_is_json_per_line(client, claude, api):
    pid = segmented(client, scenes=2)
    render(client, pid)
    wait_for_job(pid)

    path = activity.file_for(pid)
    assert path.parent == store.project_dir(pid)
    raw = path.read_text(encoding="utf-8").strip().splitlines()
    assert raw, "nothing was written"
    for line in raw:
        parsed = json.loads(line)                    # every line, on its own
        assert parsed["ts"] and parsed["project"] == pid


def test_a_torn_last_line_does_not_lose_the_rest_of_the_file(client, claude, api):
    """A hard kill mid-append leaves half a line. The ledger is an append-only
    log, so the fix is to skip it -- not to refuse to read the file."""
    pid = segmented(client, scenes=1)
    render(client, pid)
    wait_for_job(pid)
    before = len(activity.read(pid))

    with activity.file_for(pid).open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-08-18T00:00:00Z", "event": "att')

    assert len(activity.read(pid)) == before


def test_deleting_a_project_takes_its_ledger_with_it(client, claude, api):
    pid = segmented(client, scenes=1)
    render(client, pid)
    wait_for_job(pid)
    assert activity.file_for(pid).is_file()

    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert not activity.file_for(pid).exists()


# --------------------------------------------------------------------- #
# Successes
# --------------------------------------------------------------------- #

def test_every_rendered_scene_gets_a_priced_line(client, claude, api):
    pid = segmented(client, scenes=3)
    render(client, pid)
    wait_for_job(pid)

    rows = attempts(pid, kind="image")
    assert len(rows) == 3
    assert {r["scene"] for r in rows} == {1, 2, 3}
    for row in rows:
        assert row["outcome"] == activity.OK
        assert row["cost"] == api.cost
        # The estimate rides on the same row as the actual, so reconciling is a
        # read rather than a join against the plan that produced it.
        assert row["estimate"] == 0.09
        assert row["generation_id"] and row["bytes"] > 0
        assert row["latency_ms"] >= 0


def test_one_run_id_ties_a_batch_together_anchors_included(client, claude, api):
    pid = segmented(client, scenes=3, consistency="cast")
    render(client, pid)
    wait_for_job(pid)

    rows = [r for r in attempts(pid) if r["kind"] in ("image", "anchor")]
    run_ids = {r["run_id"] for r in rows}
    assert len(run_ids) == 1, "one batch should be one run"
    assert {r["kind"] for r in rows} == {"image", "anchor"}
    # The segmentation that preceded it belongs to no run: it is not a batch,
    # and giving it one would make "what did that render cost" wrong.
    assert "run_id" not in attempts(pid, kind="claude")[0]

    # And the run's own bookends carry it too, so `?run_id=` returns the whole
    # story of one render rather than only its attempts.
    run_id = run_ids.pop()
    events = [ln["event"] for ln in activity.read(pid, run_id=run_id)]
    assert events[0] == "render.started" and events[-1] == "render.finished"


def test_the_prompt_is_fingerprinted_never_written_down(client, claude, api):
    """The story is the user's. A hash correlates two rows; it tells a reader
    who picks up the file nothing at all."""
    pid = segmented(client, scenes=1)
    render(client, pid)
    wait_for_job(pid)

    scene = project(client, pid)["scenes"][0]
    row = attempts(pid, kind="image")[0]
    assert row["prompt_sha256"] == activity.digest(scene["asset_prompt"])
    assert len(row["prompt_sha256"]) == 64

    body = activity.file_for(pid).read_text(encoding="utf-8")
    for fragment in ("porch at dusk", "moths around a bare bulb", "Beat 1"):
        assert fragment not in body
    assert "test-renderful-key" not in body


def test_a_spoken_line_is_recorded_with_its_measured_duration(client, claude, api):
    pid = segmented(client, scenes=2)
    narrated(client, pid)
    speak(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_AUDIO)

    rows = attempts(pid, kind="speech")
    assert len(rows) == 2
    for row in rows:
        assert row["outcome"] == activity.OK
        assert row["cost"] == api.audio_cost
        assert row["estimate"] > 0 and row["chars"] > 0
        assert row["seconds"] > 0 and row["measured"] is True


# --------------------------------------------------------------------- #
# The gap this exists to close: billed, and nothing to show for it
# --------------------------------------------------------------------- #

def test_a_content_rejection_is_on_the_record_as_billed_waste(client, claude, api):
    """The incident: a 451 was billed, produced no asset, and left no trace
    anywhere. `project.json` kept a detail string until the next attempt
    overwrote it, and the manifest never heard about it at all."""
    pid = segmented(client, scenes=1)
    api.submit_error = (451, {"message": "content policy"})
    render(client, pid)
    wait_for_job(pid)

    assert images(pid) == []                    # nothing was produced
    assert store.read_manifest(pid) == {}       # and the manifest agrees

    row = attempts(pid, kind="image")[0]        # the ledger does not
    assert row["outcome"] == activity.REJECTED
    assert row["http_status"] == 451
    assert row["stage"] == activity.SUBMIT
    assert "content policy" in row["error"]

    report = activity.report(pid)
    assert report["wasted"] == 0.09             # priced off the estimate
    assert report["billed"] == 0.09 and report["waste_ratio"] == 1.0


def test_a_download_that_dies_after_the_picture_was_made_is_waste(client, claude, api):
    """The generation completed and was billed. We just never got the bytes --
    which is exactly the case a success-only record cannot represent."""
    pid = segmented(client, scenes=1)
    api.download_status = 404
    render(client, pid)
    wait_for_job(pid)

    row = attempts(pid, kind="image")[0]
    assert row["outcome"] == activity.FAILED
    assert row["stage"] == activity.DOWNLOAD
    assert row["generation_id"]
    # The real cost, not the estimate: the generation finished and told us.
    assert row["cost"] == api.cost
    assert activity.was_billed(row)
    assert activity.report(pid)["wasted"] == api.cost


def test_an_engine_failure_after_submission_counts_against_the_run(client, claude, api):
    pid = segmented(client, scenes=1)
    api.fail_all_generations = True
    render(client, pid)
    wait_for_job(pid)

    row = attempts(pid, kind="image")[0]
    assert row["outcome"] == activity.FAILED
    assert row["stage"] == activity.POLL
    assert activity.was_billed(row)


def test_a_connection_failure_before_submission_is_not_counted_as_spend(
        client, claude, api, monkeypatch):
    """The other half of honesty. Nothing reached Renderful, so nothing was
    billed, and reporting it as waste would overstate the only figure anyone
    reads this file for."""
    pid = segmented(client, scenes=1)
    monkeypatch.setattr(renderful, "RENDERFUL_API_BASE", "http://127.0.0.1:1/api/v1")
    render(client, pid)
    wait_for_job(pid)

    row = attempts(pid, kind="image")[0]
    assert row["outcome"] == activity.FAILED
    assert row["stage"] == activity.SUBMIT
    assert not activity.was_billed(row)
    assert activity.report(pid)["wasted"] == 0.0


def test_running_out_of_credit_is_its_own_verdict(client, claude, api):
    pid = segmented(client, scenes=2)
    api.submit_error = (402, {"message": "credit limit reached"})
    render(client, pid)
    wait_for_job(pid)

    rows = attempts(pid, kind="image")
    assert rows and all(r["outcome"] == activity.FATAL for r in rows)
    assert all(r["http_status"] == 402 for r in rows)
    # Fatal at submission means the account was refused, not that a generation
    # ran, so it is not waste.
    assert activity.report(pid)["wasted"] == 0.0


def test_a_cancelled_scene_says_so_rather_than_reading_as_a_failure(client, claude, api):
    pid = segmented(client, scenes=3)
    api.polls_before_complete = 50              # long enough to press cancel
    render(client, pid)
    client.post(f"/api/projects/{pid}/cancel")
    wait_for_job(pid)

    rows = attempts(pid, kind="image")
    assert rows, "the attempts were opened before the cancel landed"
    assert any(r["outcome"] == activity.CANCELLED for r in rows)
    # A cancel after submission still cost money -- the generation was running.
    cancelled = [r for r in rows if r["outcome"] == activity.CANCELLED]
    assert all(activity.was_billed(r) for r in cancelled if r["stage"] != activity.SUBMIT)


def test_an_attempt_is_written_even_when_the_caller_forgets_a_verdict(client, claude):
    """The guard that makes this a ledger rather than a convention. A billable
    call that leaves no trace is the one bug this module cannot tolerate, so the
    context manager writes a line whatever the body did."""
    pid = new_project(client)
    with activity.attempt(pid, "image", scene=1):
        pass

    row = attempts(pid)[0]
    assert row["outcome"] == activity.FAILED
    assert "without a recorded outcome" in row["error"]


def test_an_unwritable_ledger_never_takes_down_the_render(client, claude, api,
                                                          monkeypatch):
    """It is an observer. A full disk is a reason to write nothing, never a
    reason to lose the picture that was being paid for."""
    pid = segmented(client, scenes=1)

    # A path whose parent is a regular file. Every write raises the same OSError
    # a full disk does, through the real code rather than around it.
    blocker = store.project_dir(pid) / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(activity, "file_for",
                        lambda pid: blocker / activity.FILE_NAME)

    render(client, pid)
    wait_for_job(pid)
    assert len(images(pid)) == 1
    assert project(client, pid)["scenes"][0]["status"] == "done"


# --------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------- #

def test_a_claude_call_records_which_model_answered_and_what_it_thought_with(
        client, claude):
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/segment", json={"scene_count": 3})
    assert r.status_code == 200, r.text

    row = attempts(pid, kind="claude")[0]
    assert row["task"] == "segment"
    assert row["outcome"] == activity.OK
    assert row["model"] == config.SEGMENT_MODEL
    assert row["answered_by"] == config.SEGMENT_MODEL
    assert row["effort"] == config.SEGMENT_EFFORT
    assert row["fell_back"] is False
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0


def test_a_silent_fallback_to_sonnet_stops_being_silent(client, claude):
    """The incident: Opus saturated, Sonnet answered, and the only trace was a
    boolean on the project. Which run it happened on, and what it cost, were
    both unrecoverable."""
    from fake_claude import overloaded

    seen: list[str] = []
    real = claude.segment

    def flaky(kwargs):
        seen.append(kwargs["model"])
        if kwargs["model"] == config.SEGMENT_MODEL:
            raise overloaded()
        return real(kwargs)
    claude.segment = flaky

    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/segment",
                       json={"scene_count": 2}).status_code == 200

    row = attempts(pid, kind="claude")[0]
    assert row["fell_back"] is True
    assert row["answered_by"] == config.FALLBACK_CLAUDE_MODEL
    assert row["attempts"] == len(seen)


def test_a_claude_call_that_never_answered_is_recorded_too(client, claude):
    from fake_claude import overloaded

    claude.segment = overloaded()
    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/segment",
                       json={"scene_count": 2}).status_code == 503

    row = attempts(pid, kind="claude")[0]
    assert row["outcome"] == activity.FAILED
    assert row["task"] == "segment"
    # No generation ran, so it is not counted as money -- Claude is priced in
    # tokens here, and the report keeps the two apart.
    assert activity.report(pid)["billed"] == 0.0


def test_a_refusal_is_not_recorded_as_a_success(client, claude):
    """It arrives as a perfectly good HTTP response that cost full output tokens
    and produced nothing usable -- the exact shape this file exists to catch."""
    from fake_claude import Reply

    claude.segment = Reply("", stop_reason="refusal", refusal_category="violence")
    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/segment",
                       json={"scene_count": 2}).status_code == 502

    row = attempts(pid, kind="claude")[0]
    assert row["outcome"] == activity.REJECTED
    assert row["stop_reason"] == "refusal"
    assert row["output_tokens"] >= 0


def test_hitting_the_output_limit_is_recorded_as_a_failure(client, claude):
    from fake_claude import Reply

    claude.segment = Reply("{}", stop_reason="max_tokens")
    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/segment",
                       json={"scene_count": 40}).status_code == 502

    row = attempts(pid, kind="claude")[0]
    assert row["outcome"] == activity.FAILED
    assert row["stop_reason"] == "max_tokens"


def test_claude_tokens_are_totalled_separately_from_dollars(client, claude, api):
    pid = segmented(client, scenes=2)
    narrated(client, pid)
    render(client, pid)
    wait_for_job(pid)

    report = activity.report(pid)
    assert report["claude_input_tokens"] > 0
    assert report["claude_output_tokens"] > 0
    # Two images; the two Claude calls contribute nothing to the money column.
    assert report["billed"] == round(api.cost * 2, 4)
    assert report["by_kind"]["claude"] == 2 and report["by_kind"]["image"] == 2


# --------------------------------------------------------------------- #
# Milestones
# --------------------------------------------------------------------- #

def test_the_workflow_leaves_a_trail_from_creation_to_export(client, claude, api,
                                                             ffmpeg):
    pid = segmented(client, scenes=2)
    narrated(client, pid)
    render(client, pid)
    wait_for_job(pid)
    speak(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)
    assert client.post(f"/api/projects/{pid}/export", json={}).status_code == 200

    events = [ln["event"] for ln in activity.read(pid)]
    for milestone in ("project.created", "story.segmented", "narration.written",
                      "render.started", "render.finished", "audio.started",
                      "audio.finished", "video.started", "video.assembled",
                      "export.written"):
        assert milestone in events, milestone
    assert events[0] == "project.created"
    assert events[-1] == "export.written"


def test_a_finished_run_totals_itself_from_its_own_lines(client, claude, api):
    pid = segmented(client, scenes=3)
    api.fail_prompt_containing = "beat 2"
    render(client, pid)
    wait_for_job(pid)

    closing = lines(pid, event="render.finished")[-1]
    assert closing["attempts"] == 3
    assert closing["by_outcome"] == {activity.OK: 2, activity.FAILED: 1}
    assert closing["billed"] == round(api.cost * 2 + 0.09, 4)
    # The failed one never reported a cost, so it is valued at what the plan
    # quoted for it. Guessing zero would understate exactly the figure this
    # column exists to show.
    assert closing["wasted"] == 0.09
    assert closing["seconds"] >= 0


def test_segmenting_records_what_the_model_produced(client, claude):
    pid = new_project(client)
    client.patch(f"/api/projects/{pid}", json={"consistency": "cast"})
    client.post(f"/api/projects/{pid}/segment", json={"scene_count": 4})

    row = lines(pid, event="story.segmented")[-1]
    assert row["scenes"] == 4 and row["cast"] == 2
    assert row["language"] == "en"
    assert row["story_chars"] > 0


# --------------------------------------------------------------------- #
# Reading it back
# --------------------------------------------------------------------- #

def test_the_endpoint_answers_what_this_project_cost(client, claude, api):
    pid = segmented(client, scenes=2)
    render(client, pid)
    wait_for_job(pid)

    body = client.get(f"/api/projects/{pid}/activity").json()
    report = body["report"]
    assert report["attempts"] == 3               # one segment call, two images
    # Quoted 0.09 an image, actually billed 0.04. Estimate accuracy is a read
    # across one row, which is the whole reason both numbers are on it.
    assert report["estimated"] == 0.18
    assert report["billed"] == round(api.cost * 2, 4)
    assert report["wasted"] == 0.0 and report["waste_ratio"] == 0.0
    assert report["runs"] == 1
    assert report["by_outcome"][activity.OK] == 3
    assert body["events"] and body["events"][0]["event"] == "project.created"


def test_the_endpoint_can_be_narrowed_to_one_run(client, claude, api):
    pid = segmented(client, scenes=2)
    render(client, pid)
    first = wait_for_job(pid).run_id
    render(client, pid, force=True)
    second = wait_for_job(pid).run_id
    assert first != second

    body = client.get(f"/api/projects/{pid}/activity?run_id={second}").json()
    assert {ln["run_id"] for ln in body["events"]} == {second}
    assert len([ln for ln in body["events"] if ln["event"] == "attempt"]) == 2
    # The report is the project's whole life, not the filtered slice.
    assert body["report"]["attempts"] == 5


def test_the_endpoint_refuses_a_project_that_is_not_there(client, claude):
    # A route that does not exist also answers 404, so the real project comes
    # first: without it this test would pass against an app with no endpoint.
    pid = segmented(client, scenes=1)
    assert client.get(f"/api/projects/{pid}/activity").status_code == 200

    assert client.get("/api/projects/nope/activity").status_code == 404
    assert client.get("/api/projects/..%2F..%2Fetc/activity").status_code in (400, 404)


def test_the_ledger_rotates_instead_of_growing_without_end(client, claude,
                                                            monkeypatch):
    pid = new_project(client)
    monkeypatch.setattr(activity, "MAX_BYTES", 400)
    for i in range(40):
        activity.record(pid, "test.tick", index=i)

    rolled = sorted(p.name for p in store.project_dir(pid).glob("activity*.jsonl"))
    assert "activity.jsonl" in rolled
    assert len(rolled) <= activity.KEEP + 1
    assert activity.file_for(pid).stat().st_size < 4 * activity.MAX_BYTES


# --------------------------------------------------------------------- #
# Saying why, once
#
# Fourteen scenes refused for one reason is one problem. What used to happen
# was fourteen identical rows and no explanation on any of them.
# --------------------------------------------------------------------- #

REFUSED = {"message": "Prompt references minors. Content involving minors is not allowed."}


def refused(client, api, scenes=12, consistency="cast"):
    pid = segmented(client, scenes=scenes, consistency=consistency)
    api.submit_error = (451, REFUSED)
    render(client, pid)
    wait_for_job(pid)
    return pid


def test_a_whole_run_refused_for_one_reason_is_explained_once(client, claude, api):
    pid = refused(client, api)
    rows = [r for r in attempts(pid) if r["kind"] in ("image", "anchor")]
    assert len(rows) == 14                      # every one still billed, every one recorded

    # One explanation per (cause, kind), because a portrait and a scene need
    # different edits -- and nothing beyond that.
    explained = [r for r in rows if "hint" in r]
    assert len(explained) == 2
    assert {r["kind"] for r in explained} == {"image", "anchor"}

    # The other twelve say what they were and count themselves, nothing more.
    repeats = [r for r in rows if "hint" not in r]
    assert len(repeats) == 12
    assert all(r["reason"] == activity.CONTENT_POLICY for r in repeats)
    assert all(r["repeat"] >= 2 for r in repeats)
    assert all("prompt" not in r for r in repeats)


def test_the_advice_points_at_the_text_that_actually_failed(client, claude, api):
    pid = refused(client, api)
    by_kind = {r["kind"]: r for r in attempts(pid) if "hint" in r}

    # An anchor is built from a character description alone; no scene reaches it.
    assert "character's description" in by_kind["anchor"]["hint"]
    assert "step 2" in by_kind["anchor"]["hint"]
    # A scene is three texts concatenated, and any of them could be the problem.
    assert "style block" in by_kind["image"]["hint"]
    assert "characters it names" in by_kind["image"]["hint"]
    # And the part that stops the next render from costing the same again.
    for row in by_kind.values():
        assert "fails again and bills again" in row["hint"]
        assert row["detail"] == REFUSED["message"]        # theirs, without HTTP noise


def test_the_rejected_text_is_written_down_because_the_text_is_the_bug(
        client, claude, api):
    """The one exception to hashing everything. A fingerprint tells you two rows
    match; it cannot tell you which words to change, and on a content rejection
    that is the whole question."""
    pid = refused(client, api, scenes=2)
    first = next(r for r in attempts(pid) if r["kind"] == "image" and "hint" in r)
    assert "prompt" in first
    assert first["prompt_sha256"] == activity.digest(first["prompt"])


def test_only_a_content_rejection_writes_the_text_down(client, claude, api):
    """A 5xx or a dead download says nothing about the wording, so the story
    stays out of the file for every failure except the one it explains."""
    pid = segmented(client, scenes=1)
    api.download_status = 404
    render(client, pid)
    wait_for_job(pid)

    row = attempts(pid, kind="image")[0]
    assert row["outcome"] == activity.FAILED and "prompt" not in row
    assert "porch at dusk" not in activity.file_for(pid).read_text(encoding="utf-8")


def test_the_text_capture_can_be_turned_off(client, claude, api, monkeypatch):
    monkeypatch.setattr(config, "LOG_REJECTED_PROMPTS", False)
    pid = refused(client, api, scenes=2)

    assert all("prompt" not in r for r in attempts(pid))
    assert "porch at dusk" not in activity.file_for(pid).read_text(encoding="utf-8")
    # The diagnosis survives; only the evidence is withheld.
    assert any("hint" in r for r in attempts(pid))


def test_the_captured_text_is_bounded(client, claude, api, monkeypatch):
    monkeypatch.setattr(config, "LOG_PROMPT_CHARS", 40)
    pid = refused(client, api, scenes=2)
    first = next(r for r in attempts(pid) if r.get("prompt"))
    assert len(first["prompt"]) == 40


def test_a_second_run_explains_itself_again(client, claude, api):
    """Dedup is per run. Silencing a reason forever would mean the same problem
    tomorrow arrived as a count with no cause."""
    pid = refused(client, api, scenes=2, consistency="off")
    render(client, pid, force=True)
    wait_for_job(pid)

    by_run: dict[str, int] = {}
    for row in attempts(pid, kind="image"):
        if "hint" in row:
            by_run[row["run_id"]] = by_run.get(row["run_id"], 0) + 1
    assert len(by_run) == 2 and set(by_run.values()) == {1}


# --------------------------------------------------------------------- #
# The run summary, and stdout
# --------------------------------------------------------------------- #

def test_the_run_summary_groups_the_failures_by_cause(client, claude, api):
    pid = refused(client, api)
    closing = lines(pid, event="render.finished")[-1]

    assert closing["by_outcome"] == {activity.REJECTED: 14}
    assert closing["wasted"] == closing["billed"] == round(0.09 * 14, 4)

    failures = {f["kind"]: f for f in closing["failures"]}
    assert set(failures) == {"image", "anchor"}
    assert failures["image"]["count"] == 12
    assert failures["image"]["scenes"] == list(range(1, 13))
    assert failures["anchor"]["count"] == 2
    assert sorted(failures["anchor"]["characters"]) == ["the-cousin", "the-narrator"]
    # Each group carries its own advice rather than whichever arrived first.
    assert "character's description" in failures["anchor"]["hint"]
    assert "style block" in failures["image"]["hint"]


def test_the_failure_reaches_standard_out(client, claude, api, capsys):
    pid = refused(client, api, scenes=3)
    printed = capsys.readouterr().out

    assert "content-policy" in printed
    assert REFUSED["message"] in printed
    assert "fails again and bills again" in printed
    assert "sent:" in printed                       # the text, so it can be read
    # Once per cause and kind, not once per scene.
    assert printed.count("content-policy") == 2


def test_a_success_says_nothing_at_all(client, claude, api, capsys):
    """A log nobody can ignore is a log that stays quiet when things work."""
    pid = segmented(client, scenes=2)
    render(client, pid)
    wait_for_job(pid)

    printed = capsys.readouterr().out
    assert printed.strip() == "", printed


# --------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------- #

def test_each_failure_gets_a_reason_worth_grouping_by(client, claude, api):
    cases = [
        ((402, {"message": "credit limit reached"}), activity.OUT_OF_CREDIT),
        ((401, {"message": "invalid api key"}), activity.BAD_KEY),
        ((400, {"message": "unknown parameter"}), activity.BAD_REQUEST),
    ]
    for error, expected in cases:
        pid = segmented(client, scenes=1)
        api.submit_error = error
        render(client, pid)
        wait_for_job(pid)
        assert attempts(pid, kind="image")[0]["reason"] == expected, expected


def test_an_engine_failure_and_a_cancel_are_not_the_same_reason(client, claude, api):
    pid = segmented(client, scenes=1)
    api.fail_all_generations = True
    render(client, pid)
    wait_for_job(pid)
    assert attempts(pid, kind="image")[0]["reason"] == activity.ENGINE_FAILED

    other = segmented(client, scenes=3)
    api.fail_all_generations = False
    api.polls_before_complete = 50
    render(client, other)
    client.post(f"/api/projects/{other}/cancel")
    wait_for_job(other)
    reasons = {r.get("reason") for r in attempts(other, kind="image")}
    assert activity.STOPPED in reasons
