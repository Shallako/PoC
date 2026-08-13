"""Narration audio: plan, confirm, speak, measure, resume, fail, export.

Same shape as test_render.py and for the same reason -- the real client runs
against the fake Renderful server, so the payload, the poll loop, the 4xx split
and the duration parser all execute for real. The fake serves a genuine MPEG
frame stream, so `shoulico.audio` is measured against actual bytes.
"""

from __future__ import annotations

import json

import pytest
from conftest import audio_files, project, segmented, speak, wait_for_job
from fake_renderful import AUDIO_TYPE, mp3_seconds, silent_mp3

from shoulico import audio, config, engines, narration, orchestrator, renderful, store

EXPECTED_SECONDS = mp3_seconds()


def narrated(client, scenes=3, **body) -> str:
    pid = segmented(client, scenes=scenes)
    r = client.post(f"/api/projects/{pid}/narration", json=body)
    assert r.status_code == 200, r.text
    return pid


def spoken(client, pid, api, **body) -> dict:
    speak(client, pid, **body)
    wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
    return project(client, pid)


# --------------------------------------------------------------------- #
# Duration measurement
# --------------------------------------------------------------------- #

def test_duration_is_measured_from_the_bytes_not_the_word_count():
    assert audio.seconds(silent_mp3()) == pytest.approx(EXPECTED_SECONDS, abs=0.01)
    assert audio.seconds(silent_mp3(frames=76)) == pytest.approx(
        EXPECTED_SECONDS * 2, abs=0.02)


def test_unreadable_audio_returns_none_rather_than_raising():
    assert audio.seconds(b"") is None
    assert audio.seconds(b"not audio at all") is None
    assert audio.seconds(b"\xff") is None


def test_id3_tagged_mp3_is_parsed_past_the_tag():
    # ElevenLabs delivers ID3v2; the tag length is syncsafe, 7 bits per byte.
    tag_body = b"\x00" * 64
    tag = b"ID3\x04\x00\x00" + bytes([0, 0, 0, len(tag_body)]) + tag_body
    assert audio.seconds(tag + silent_mp3()) == pytest.approx(
        EXPECTED_SECONDS, abs=0.01)


def test_sniff_recognises_speech_containers():
    assert renderful.sniff(silent_mp3()) == "mp3"
    assert renderful.sniff(b"ID3\x04" + b"\x00" * 20) == "mp3"
    assert renderful.sniff(b"RIFF" + b"\x00" * 4 + b"WAVE") == "wav"


# --------------------------------------------------------------------- #
# Preview before spend
# --------------------------------------------------------------------- #

def test_plan_prices_by_character_not_by_line(client, claude):
    pid = narrated(client, scenes=3)
    plan = client.post(f"/api/projects/{pid}/narration/plan-audio", json={}).json()

    assert plan["count"] == 3
    assert plan["voice_engine"] == config.DEFAULT_VOICE
    assert plan["verified_engine"] is True

    # A 70-character line billed 0.0035 live, i.e. $0.05/1000 chars.
    expected = plan["chars"] * plan["price_per_1k_chars"] / engines.CHARS_PER_PRICE_UNIT
    assert plan["estimate"] == pytest.approx(expected, abs=0.0001)
    assert all(row["reason_key"] == "new" for row in plan["speak"])


def test_plan_reports_scenes_with_no_line_to_speak(client, claude):
    pid = narrated(client, scenes=3)
    client.patch(f"/api/projects/{pid}", json={"scenes": [{"n": 2, "narration": "  "}]})
    plan = client.post(f"/api/projects/{pid}/narration/plan-audio", json={}).json()

    assert [row["n"] for row in plan["speak"]] == [1, 3]
    assert [row["n"] for row in plan["missing_narration"]] == [2]


def test_speaking_without_confirmation_spends_nothing(client, claude, api):
    pid = narrated(client, scenes=2)
    r = client.post(f"/api/projects/{pid}/narration/speak", json={})
    assert r.status_code == 400
    assert "confirmation" in r.json()["detail"]
    assert api.submit_count == 0


# --------------------------------------------------------------------- #
# The spend path
# --------------------------------------------------------------------- #

def test_speaking_writes_audio_under_the_image_stem_with_measured_durations(
        client, claude, api):
    pid = narrated(client, scenes=3)
    p = spoken(client, pid, api)

    assert audio_files(pid) == [f"{pid}_{n:03d}_beat-{n}.mp3" for n in (1, 2, 3)]
    assert all(s["audio_status"] == "done" for s in p["scenes"])
    assert all(s["audio_measured"] is True for s in p["scenes"])
    assert all(s["audio_seconds"] == pytest.approx(EXPECTED_SECONDS, abs=0.01)
               for s in p["scenes"])
    assert p["audio_seconds_total"] == pytest.approx(EXPECTED_SECONDS * 3, abs=0.02)
    assert p["audio_lines_done"] == 3
    assert p["spend"]["lines"] == 3
    assert p["spend"]["actual"] == pytest.approx(api.audio_cost * 3, abs=0.0001)


def test_the_payload_is_a_speech_job_and_carries_no_image_parameters(client, claude, api):
    pid = narrated(client, scenes=1)
    spoken(client, pid, api)

    sent = api.submits_of_type(AUDIO_TYPE)
    assert len(sent) == 1
    assert sent[0]["model"] == config.DEFAULT_VOICE
    assert sent[0]["prompt"].startswith("Line 1.")
    # An opaque voice id, never a display name: ElevenLabs answers a name with
    # "A voice with voice_id 'George' was not found", and it answers it after
    # the request has been accepted.
    assert sent[0]["voice"] == engines.DEFAULT_VOICE_ID
    assert sent[0]["voice"] in engines.VOICE_LIBRARY
    # An aspect ratio means nothing to a speech model, and a rejected request bills.
    for image_only in ("aspect_ratio", "resolution", "num_outputs", "output_format"):
        assert image_only not in sent[0]


def test_the_manifest_records_provenance_for_every_spoken_line(client, claude, api):
    pid = narrated(client, scenes=2)
    spoken(client, pid, api)

    manifest = client.get(f"/api/projects/{pid}/manifest").json()
    lines = {k: v for k, v in manifest.items() if v.get("kind") == "narration"}
    assert len(lines) == 2

    entry = lines[f"{pid}_001_beat-1_narration"]
    assert entry["model"] == config.DEFAULT_VOICE
    assert entry["file"] == f"{pid}_001_beat-1.mp3"
    assert entry["text"].startswith("Line 1.")
    assert entry["cost"] == api.audio_cost
    assert entry["seconds"] == pytest.approx(EXPECTED_SECONDS, abs=0.01)
    assert entry["seconds_measured"] is True


def test_audio_is_served_back_over_the_api(client, claude, api):
    pid = narrated(client, scenes=1)
    spoken(client, pid, api)

    r = client.get(f"/api/projects/{pid}/audio/{pid}_001_beat-1.mp3")
    assert r.status_code == 200
    assert audio.seconds(r.content) == pytest.approx(EXPECTED_SECONDS, abs=0.01)
    assert client.get(f"/api/projects/{pid}/audio/nope.mp3").status_code == 404


# --------------------------------------------------------------------- #
# Idempotent resume
# --------------------------------------------------------------------- #

def test_nothing_is_respoken_when_no_line_changed(client, claude, api):
    pid = narrated(client, scenes=3)
    spoken(client, pid, api)
    api.reset_counters()

    r = client.post(f"/api/projects/{pid}/narration/speak", json={"confirm": True}).json()
    assert r["started"] is False
    assert api.submit_count == 0


def test_editing_one_line_respeaks_only_that_line(client, claude, api):
    pid = narrated(client, scenes=3)
    spoken(client, pid, api)
    api.reset_counters()

    client.patch(f"/api/projects/{pid}",
                 json={"scenes": [{"n": 2, "narration": "A different line entirely."}]})
    plan = client.post(f"/api/projects/{pid}/narration/plan-audio", json={}).json()
    assert [row["n"] for row in plan["speak"]] == [2]
    assert plan["speak"][0]["reason_key"] == "changed"

    spoken(client, pid, api)
    assert len(api.submits_of_type(AUDIO_TYPE)) == 1
    assert api.submits_of_type(AUDIO_TYPE)[0]["prompt"] == "A different line entirely."


def test_a_missing_file_respeaks_even_when_the_text_is_unchanged(client, claude, api):
    pid = narrated(client, scenes=2)
    spoken(client, pid, api)
    (store.audio_dir(pid) / f"{pid}_001_beat-1.mp3").unlink()
    api.reset_counters()

    plan = client.post(f"/api/projects/{pid}/narration/plan-audio", json={}).json()
    assert [row["n"] for row in plan["speak"]] == [1]


def test_force_respeaks_everything(client, claude, api):
    pid = narrated(client, scenes=2)
    spoken(client, pid, api)
    api.reset_counters()

    spoken(client, pid, api, force=True)
    assert len(api.submits_of_type(AUDIO_TYPE)) == 2


# --------------------------------------------------------------------- #
# Failure classes -- the image ladder, unchanged
# --------------------------------------------------------------------- #

def test_credit_exhaustion_stops_the_whole_run(client, claude, api):
    pid = narrated(client, scenes=3)
    api.submit_error = (402, {"message": "credit limit reached"})

    speak(client, pid)
    job = wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
    p = project(client, pid)

    assert job.fatal is not None and "limit reached" in job.fatal
    assert all(s["audio_status"] in ("failed", "pending") for s in p["scenes"])
    assert audio_files(pid) == []


def test_one_rejected_line_does_not_stop_the_batch(client, claude, api):
    pid = narrated(client, scenes=3)
    api.fail_generation.add("gen-2")

    p = spoken(client, pid, api)
    done = [s for s in p["scenes"] if s["audio_status"] == "done"]
    failed = [s for s in p["scenes"] if s["audio_status"] == "failed"]

    assert len(done) == 2 and len(failed) == 1
    assert orchestrator.job_for(pid, orchestrator.KIND_AUDIO).fatal is None
    assert len(audio_files(pid)) == 2


def test_an_empty_line_fails_that_scene_before_it_is_submitted(client, claude, api):
    pid = narrated(client, scenes=2)
    speak(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
    api.reset_counters()

    client.patch(f"/api/projects/{pid}", json={"scenes": [{"n": 1, "narration": "  "}]})
    plan = client.post(f"/api/projects/{pid}/narration/plan-audio", json={}).json()
    assert [row["n"] for row in plan["missing_narration"]] == [1]
    assert api.submit_count == 0


def test_audio_that_cannot_be_parsed_falls_back_to_the_estimate(client, claude, api):
    pid = narrated(client, scenes=1)
    api.audio_payload = b"this is not a container we understand"

    p = spoken(client, pid, api)
    scene = p["scenes"][0]

    # The line still lands -- it was paid for -- but it is flagged as unmeasured.
    assert scene["audio_status"] == "done"
    assert scene["audio_measured"] is False
    assert scene["audio_seconds"] == pytest.approx(
        narration.estimate_seconds(scene["narration"]), abs=0.01)
    assert scene["audio"].endswith(".audio")


def test_a_render_and_a_voice_run_do_not_share_a_job_slot(client, claude, api):
    pid = narrated(client, scenes=2)
    spoken(client, pid, api)

    assert orchestrator.job_for(pid, orchestrator.KIND_AUDIO) is not None
    assert orchestrator.job_for(pid, orchestrator.KIND_RENDER) is None
    assert project(client, pid)["audio_job"]["kind"] == orchestrator.KIND_AUDIO


# --------------------------------------------------------------------- #
# Voice settings
# --------------------------------------------------------------------- #

def test_the_voice_registry_is_offered_to_the_browser(client):
    voices = client.get("/api/voices").json()
    assert voices["default"] == config.DEFAULT_VOICE
    assert voices["voices"][config.DEFAULT_VOICE]["verified"] is True
    assert voices["voices"]["speech-2.6-turbo"]["verified"] is False


def test_every_offered_voice_is_an_id_rather_than_a_display_name(client):
    """The bug this guards shipped once and cost a paid request to discover.

    Renderful forwards `voice` to ElevenLabs untouched, and ElevenLabs resolves
    only opaque ids. A display name is accepted by our own validation, accepted
    by Renderful, and rejected by the provider -- the one place that costs money.
    """
    spec = next(i for i in client.get("/api/voices").json()
                ["voices"][config.DEFAULT_VOICE]["inputs"] if i["key"] == "voice")

    assert spec["options"], "a voice picker with no voices is not a picker"
    for option in spec["options"]:
        assert option in engines.VOICE_LIBRARY, option
        assert option not in engines.LEGACY_VOICE_NAMES, option
        # The label carries the human name, so the value never has to.
        assert spec["labels"][option].split(engines.LABEL_SEPARATOR)[0] != option

    # Every option is labelled, or the dropdown reads as 21 lines of noise.
    assert set(spec["labels"]) >= set(spec["options"])
    assert spec["default"] in spec["options"]


@pytest.mark.parametrize("legacy", sorted(engines.LEGACY_VOICE_NAMES))
def test_a_project_saved_with_an_old_voice_name_still_loads(client, claude, legacy):
    """Old display names sit in saved projects and must not strand them.

    The settings endpoint revalidates the params it reads back, so a stored
    value it rejects cannot be corrected through the UI -- picking a voice
    replays the same broken params and fails again.
    """
    resolved = engines.validate(config.DEFAULT_VOICE, {"voice": legacy},
                                engines.SECTION_VOICES)["voice"]
    assert resolved in engines.VOICE_LIBRARY

    pid = segmented(client, scenes=1)
    store.mutate(pid, lambda p: p["narration"].update({"voice_params": {"voice": legacy}}))

    r = client.post(f"/api/projects/{pid}/narration/voice", json={})
    assert r.status_code == 200, r.text
    assert r.json()["narration"]["voice_params"]["voice"] == resolved


def test_the_page_is_shown_the_voice_the_run_would_use(client, claude):
    """A stored value matching no option makes a select display the first one.

    That is worse than an error: the page would name one voice while the run
    used another, and nothing anywhere would look wrong.
    """
    pid = segmented(client, scenes=1)
    store.mutate(pid, lambda p: p["narration"].update({"voice_params": {"voice": "George"}}))

    shown = client.get(f"/api/projects/{pid}").json()["narration"]["voice_params"]["voice"]
    assert shown == engines.LEGACY_VOICE_NAMES["George"]

    spec = next(i for i in client.get("/api/voices").json()
                ["voices"][config.DEFAULT_VOICE]["inputs"] if i["key"] == "voice")
    assert shown in spec["options"]


def test_a_stale_voice_list_on_disk_is_repaired_not_left_alone(tmp_path, monkeypatch):
    """engines.json is written on first run, so a bad shipped list persists there.

    Filling in only missing sections would have left every existing install
    broken for good.
    """
    path = tmp_path / "engines.json"
    stale = json.loads(json.dumps(engines.DEFAULT_REGISTRY))
    voice = stale["voices"][config.DEFAULT_VOICE]
    voice["schema_version"] = 1
    next(i for i in voice["inputs"] if i["key"] == "voice")["options"] = ["George"]
    stale["voices"]["mine-cloned"] = {"name": "My clone", "inputs": []}
    path.write_text(json.dumps(stale), encoding="utf-8")

    monkeypatch.setattr(config, "ENGINES_FILE", path)
    reg = engines.registry(reload=True)

    shipped = next(i for i in reg["voices"][config.DEFAULT_VOICE]["inputs"]
                   if i["key"] == "voice")
    assert shipped["options"] == list(engines.VOICE_LIBRARY)
    assert reg["voices"][config.DEFAULT_VOICE]["schema_version"] == \
        engines.VOICE_SCHEMA_VERSION
    # A voice the user added is theirs; repair must not touch it.
    assert reg["voices"]["mine-cloned"]["name"] == "My clone"
    assert json.loads(path.read_text(encoding="utf-8")) == reg


def test_voice_parameters_are_validated_before_anything_is_spent(client, claude, api):
    pid = narrated(client, scenes=1)
    r = client.post(f"/api/projects/{pid}/narration/voice",
                    json={"voice_params": {"voice": "Nobody"}})
    assert r.status_code == 400
    assert "Nobody" in r.json()["detail"]
    assert api.submit_count == 0


def test_a_blank_language_code_follows_the_story(client, claude, api):
    pid = narrated(client, scenes=1)

    def apply(proj):
        proj["language"] = {"code": "fr", "name": "French", "native_name": "Français"}
    store.mutate(pid, apply)

    spoken(client, pid, api)
    assert api.submits_of_type(AUDIO_TYPE)[0]["language_code"] == "fr"


# --------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------- #

def test_the_page_carries_the_voice_controls(client):
    """The UI is one static file, so a dropped control fails silently in a browser.

    This does not execute the script; it guards the contract between the page and
    the endpoints it calls.
    """
    html = client.get("/").text

    for element_id in ("voiceList", "voiceParams", "ttsPlan", "speakBtn",
                       "forceSpeak", "cancelSpeakBtn", "ttsStatus"):
        assert f'id="{element_id}"' in html, element_id

    assert "/narration/plan-audio" in html
    assert "/narration/speak" in html
    assert "/narration/cancel-audio" in html
    assert "/api/voices" in html or '"/voices"' in html

    # The speak button must not be wired through data-scene: collectScenePatch()
    # sweeps those into the save payload and a button has no field to give it.
    assert "data-speak=" in html

    # The panel that used to promise no synthesis would now be a lie.
    assert "Script only — no synthesis" not in html


# --------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------- #

def test_export_pairs_image_text_and_voice_under_one_stem(client, claude, api):
    from conftest import render

    pid = narrated(client, scenes=2)
    render(client, pid)
    wait_for_job(pid)
    spoken(client, pid, api)

    result = client.post(f"/api/projects/{pid}/export", json={"flatten": True}).json()
    stem = f"{pid}_001_beat-1"
    row = next(r for r in result["files"] if r["scene"] == 1)

    assert row["image"] == f"{stem}.jpg"
    assert row["narration"] == f"{stem}.txt"
    assert row["audio"] == f"{stem}.mp3"
    assert row["seconds"] == pytest.approx(EXPECTED_SECONDS, abs=0.01)

    exported = sorted(p.name for p in store.export_dir(pid).iterdir())
    assert f"{stem}.mp3" in exported
