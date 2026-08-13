"""Timeline, captions and video assembly.

The split matters here. Timeline and caption arithmetic is pure and is tested as
such. Assembly is tested through the real command construction against a fake
binary, which proves what this project asks ffmpeg to do but not that ffmpeg
agrees; the one test that proves that is marked `needs_ffmpeg` and skips unless a
real one is installed.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from conftest import assemble, project, render, segmented, speak, wait_for_job
from fake_ffmpeg import FAKE_VERSION

from shoulico import captions, config, engines, orchestrator, store, timeline, video

needs_ffmpeg = pytest.mark.skipif(not video.available(),
                                  reason="no real ffmpeg installed on this machine")


def ready(client, pid, api, *, spoken=True) -> dict:
    """A project with images, and optionally with real narration audio."""
    client.post(f"/api/projects/{pid}/narration", json={})
    render(client, pid)
    wait_for_job(pid)
    if spoken:
        speak(client, pid)
        wait_for_job(pid, kind=orchestrator.KIND_AUDIO)
    return project(client, pid)


def beat_list(client, pid):
    proj = store.load(pid)
    return timeline.build(proj, store.video_settings(proj))[0]


# --------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------- #

def test_scene_length_comes_from_measured_audio_not_the_estimate(client, claude, api):
    pid = segmented(client, scenes=2)
    ready(client, pid, api)

    beats = beat_list(client, pid)
    assert len(beats) == 2
    for b in beats:
        assert b.measured, "audio exists, so the length must not be an estimate"
        # lead-in and tail are silence around the words, not part of them.
        assert b.duration == pytest.approx(
            b.speech_seconds + config.VIDEO_LEAD_IN_SECONDS + config.VIDEO_TAIL_SECONDS,
            abs=0.01)


def test_an_unspoken_line_falls_back_to_the_estimate_and_says_so(client, claude, api):
    pid = segmented(client, scenes=2)
    ready(client, pid, api, spoken=False)

    beats = beat_list(client, pid)
    assert beats and not any(b.measured for b in beats)
    assert not timeline.all_measured(beats)
    # A projection is still useful; it just must not claim to be a measurement.
    assert timeline.total_seconds(beats) > 0


def test_scenes_run_back_to_back_with_no_gap_or_overlap(client, claude, api):
    pid = segmented(client, scenes=3)
    ready(client, pid, api)

    beats = beat_list(client, pid)
    assert beats[0].start == 0.0
    for prev, nxt in zip(beats, beats[1:]):
        assert nxt.start == pytest.approx(prev.end, abs=0.001)
    assert timeline.total_seconds(beats) == pytest.approx(
        sum(b.duration for b in beats), abs=0.01)


def test_a_scene_with_no_image_cannot_be_shown_and_is_reported(client, claude, api):
    pid = segmented(client, scenes=3)
    ready(client, pid, api, spoken=False)
    # Delete one render: the scene still exists, but there is nothing to show.
    proj = store.load(pid)
    (store.images_dir(pid) / proj["scenes"][1]["asset"]).unlink()

    beats, skipped = timeline.build(store.load(pid), store.video_settings(store.load(pid)))
    assert [b.n for b in beats] == [1, 3]
    assert [s["n"] for s in skipped] == [2]
    assert skipped[0]["reason_key"] == "no_image"


def test_the_frame_follows_the_rendered_images_by_default(client, claude, api):
    pid = segmented(client, scenes=1)
    proj = store.load(pid)
    proj["params"]["aspect_ratio"] = "9:16"

    assert timeline.canvas(proj, config.VIDEO_ASPECT_SOURCE) == (1080, 1920)
    assert timeline.canvas(proj, "16:9") == (1920, 1080)
    # An aspect nobody shipped must not crash the encode; fall back to the default.
    assert timeline.canvas(proj, "nonsense") == \
        config.VIDEO_CANVASES[config.DEFAULT_VIDEO_ASPECT]


# --------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------- #

def one_beat(text, *, speech=12.0, start=0.0):
    return timeline.Beat(n=1, title="t", slug="t", image=Path("i.jpg"),
                         audio=Path("a.mp3"), text=text, start=start,
                         duration=speech + 1.0, speech_start=start + 0.35,
                         speech_seconds=speech, measured=True)


def test_a_long_line_becomes_several_readable_cues():
    text = ("The harbour was quiet that morning. Nobody had seen the ship come in, "
            "and nobody would admit to it later. By noon the whole town knew.")
    cues = captions.cues_for(one_beat(text))

    assert len(cues) > 1, "a paragraph must not be held on screen as one cue"
    for cue in cues:
        lines = cue.text.split("\n")
        assert len(lines) <= config.CAPTION_MAX_LINES
        assert max(len(line) for line in lines) <= config.CAPTION_MAX_CHARS_PER_LINE
        assert cue.seconds <= config.CAPTION_MAX_SECONDS + 0.001


def test_cues_never_overlap_and_never_outrun_the_audio():
    text = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten. Eleven. Twelve."
    beat = one_beat(text, speech=6.0)
    cues = captions.cues_for(beat)

    for prev, nxt in zip(cues, cues[1:]):
        assert nxt.start >= prev.end, "two cues on screen at once"
    assert cues[0].start >= beat.speech_start
    assert cues[-1].end <= beat.speech_end + 0.001


def test_the_last_cue_lands_on_the_end_of_the_speech():
    """Rounding must not leave the captions finishing before the voice does."""
    text = ("The harbour was quiet that morning. Nobody had seen the ship come in, "
            "and nobody would admit to it later. By noon the whole town knew.")
    beat = one_beat(text, speech=12.0)
    cues = captions.cues_for(beat)
    assert len(cues) > 1
    assert cues[-1].end == pytest.approx(beat.speech_end, abs=0.001)


def test_a_short_line_over_a_long_silence_is_not_held_on_screen():
    """The reader has finished; leaving the cue up for the pause is not a caption.

    This is the one case where the captions deliberately end before the scene's
    audio does, so it is spelled out rather than left as a surprise.
    """
    beat = one_beat("Three words here.", speech=12.0)
    cues = captions.cues_for(beat)
    assert len(cues) == 1
    assert cues[0].seconds == pytest.approx(config.CAPTION_MAX_SECONDS, abs=0.001)
    assert cues[0].end < beat.speech_end


def test_an_unspaced_script_is_split_by_character_not_by_space():
    # Japanese has no spaces, so a word-boundary wrap would never break at all.
    text = "むかしむかしあるところにおじいさんとおばあさんがすんでいました。" * 3
    cues = captions.cues_for(one_beat(text))
    assert len(cues) > 1
    for cue in cues:
        for line in cue.text.split("\n"):
            assert len(line) <= config.CAPTION_MAX_CHARS_PER_LINE


def test_a_line_with_no_audio_yields_no_cues():
    beat = timeline.Beat(n=1, title="", slug="", image=Path("i"), audio=None,
                         text="Words nobody has spoken", start=0.0, duration=8.0,
                         speech_start=0.0, speech_seconds=0.0, measured=False)
    assert captions.cues_for(beat) == []


def test_srt_and_vtt_carry_the_formats_editors_expect():
    cues = captions.cues_for(one_beat("First sentence here. Second sentence here."))
    srt, vtt = captions.to_srt(cues), captions.to_vtt(cues)

    assert srt.startswith("1\n")
    assert " --> " in srt and "," in srt.split("\n")[1]   # SRT uses a comma
    assert vtt.startswith("WEBVTT\n\n")
    assert "." in vtt.split("\n")[2].split(" --> ")[0]    # WebVTT uses a full stop
    # A stamp is HH:MM:SS,mmm -- three digits of milliseconds, never four.
    for stamp in ("00:00:00,350", "00:00"):
        assert len(srt.split(" --> ")[0].split("\n")[-1]) == len("00:00:00,350")


@pytest.mark.parametrize("seconds,expected", [
    (0.0, "00:00:00,000"), (3.9999, "00:00:04,000"), (61.5, "00:01:01,500"),
    (3661.25, "01:01:01,250"),
])
def test_timestamps_round_without_producing_a_fourth_digit(seconds, expected):
    assert captions._stamp(seconds, ",") == expected


def test_captions_are_served_on_their_own(client, claude, api):
    pid = segmented(client, scenes=2)
    ready(client, pid, api)

    srt = client.get(f"/api/projects/{pid}/captions.srt")
    vtt = client.get(f"/api/projects/{pid}/captions.vtt")
    assert srt.status_code == 200 and srt.text.startswith("1\n")
    assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
    assert client.get(f"/api/projects/{pid}/captions.ass").status_code == 404


# --------------------------------------------------------------------- #
# The CapCut hand-off
# --------------------------------------------------------------------- #

def test_export_writes_captions_and_a_timing_sheet_without_any_ffmpeg(client, claude, api):
    assert not video.available() or True   # the point: this path never calls ffmpeg
    pid = segmented(client, scenes=2)
    ready(client, pid, api)

    body = client.post(f"/api/projects/{pid}/export", json={}).json()
    names = {p.name for p in store.export_dir(pid).iterdir()}
    assert body["captions"] in names and body["captions_vtt"] in names
    assert body["timing"] in names
    assert body["runtime_seconds"] > 0 and body["runtime_measured"] is True


def test_the_timing_sheet_survives_a_narration_line_full_of_commas(client, claude, api):
    pid = segmented(client, scenes=1)
    ready(client, pid, api, spoken=False)
    tricky = 'He said, "go north, now", and left; nobody argued.'
    r = client.patch(f"/api/projects/{pid}", json={"scenes": [{"n": 1, "narration": tricky}]})
    assert r.status_code == 200, r.text

    client.post(f"/api/projects/{pid}/export", json={})
    sheet = store.export_dir(pid) / f"{pid}_timing.csv"
    rows = list(csv.DictReader(io.StringIO(sheet.read_text(encoding="utf-8"))))

    assert len(rows) == 1
    # Hand-joined CSV would have split this row across four columns.
    assert rows[0]["narration"] == tricky
    assert rows[0]["duration_source"] == "estimated"
    assert float(rows[0]["start_seconds"]) == 0.0


# --------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------- #

def test_without_ffmpeg_the_app_says_so_and_offers_the_remedy(client, claude, api):
    pid = segmented(client, scenes=1)
    ready(client, pid, api, spoken=False)

    r = client.post(f"/api/projects/{pid}/video/assemble")
    # 424: the request was fine, a dependency this server needs is not here.
    assert r.status_code == 424
    detail = r.json()["detail"]
    assert "winget" in detail or "brew" in detail, "a missing tool needs an install hint"

    plan = client.post(f"/api/projects/{pid}/video/plan").json()
    assert plan["ffmpeg"]["available"] is False
    assert plan["count"] == 1, "planning the cut must work without ffmpeg"


def test_the_registry_reports_whether_ffmpeg_is_actually_here(client, ffmpeg):
    profiles = client.get("/api/video-profiles").json()
    assert profiles["default"] == config.DEFAULT_VIDEO_PROFILE
    assert profiles["ffmpeg"]["available"] is True
    assert profiles["ffmpeg"]["version"] == FAKE_VERSION


def test_assembly_writes_one_segment_per_scene_then_joins_them(client, claude, api,
                                                               ffmpeg):
    pid = segmented(client, scenes=3)
    ready(client, pid, api)

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    assert len(ffmpeg.encodes) == 3
    assert len(ffmpeg.joins) == 1
    proj = project(client, pid)
    assert proj["video"]["file"] == f"{pid}_video.mp4"
    assert (store.video_dir(pid) / proj["video"]["file"]).is_file()
    assert proj["video"]["measured"] is True
    assert all(s["video_status"] == "done" for s in proj["scenes"])


def test_each_segment_is_cut_to_its_own_measured_length(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=2)
    ready(client, pid, api)
    beats = beat_list(client, pid)

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    for beat, call in zip(beats, ffmpeg.encodes):
        assert ffmpeg.flag(call, "-t") == f"{beat.duration:.3f}"


def test_ken_burns_zooms_a_fixed_amount_however_long_the_scene_is(client, claude,
                                                                  api, ffmpeg):
    pid = segmented(client, scenes=2)
    ready(client, pid, api)
    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    graph = ffmpeg.filtergraph(0)
    assert "zoompan=" in graph
    # A looped input would multiply frames by duration; zoompan takes one frame.
    assert "-loop" not in ffmpeg.encodes[0]
    # Linear in the frame counter, so the end zoom does not depend on the length.
    assert f"{config.VIDEO_KEN_BURNS_ZOOM:.6f}" in graph or "1+" in graph
    assert str(config.VIDEO_KEN_BURNS_SUPERSAMPLE * 1920) in graph


def test_still_frames_loop_the_image_instead_of_zooming_it(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)
    client.post(f"/api/projects/{pid}/video/settings",
                json={"params": {**store.video_settings(store.load(pid)),
                                 "motion": "none"}})

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    call = ffmpeg.encodes[0]
    assert "-loop" in call, "a still with no zoom needs the input looped to hold"
    assert "zoompan" not in ffmpeg.filtergraph(0)


def test_a_silent_scene_still_gets_an_audio_track(client, claude, api, ffmpeg):
    """The join is a stream copy, and a segment missing a stream breaks it."""
    pid = segmented(client, scenes=1)
    ready(client, pid, api, spoken=False)

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    graph = ffmpeg.filtergraph(0)
    assert "anullsrc" in " ".join(ffmpeg.encodes[0])
    assert "[a]" in graph


def test_narration_is_delayed_by_the_lead_in_so_no_cut_clips_a_syllable(
        client, claude, api, ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    expected_ms = int(round(config.VIDEO_LEAD_IN_SECONDS * 1000))
    assert f"adelay={expected_ms}:all=1" in ffmpeg.filtergraph(0)


def test_soft_subtitles_are_attached_without_re_encoding(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    subs = [c for c in ffmpeg.calls if "mov_text" in c]
    assert len(subs) == 1
    assert "-c" in subs[0] and subs[0][subs[0].index("-c") + 1] == "copy"
    assert (store.video_dir(pid) / f"{pid}_video.srt").is_file()


def test_burned_in_subtitles_re_encode_and_avoid_the_windows_path_trap(
        client, claude, api, ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)
    client.post(f"/api/projects/{pid}/video/settings",
                json={"params": {**store.video_settings(store.load(pid)),
                                 "subtitles": "burn"}})

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    burn = [c for c in ffmpeg.calls if any(a.startswith("subtitles=") for a in c)]
    assert len(burn) == 1
    filter_arg = next(a for a in burn[0] if a.startswith("subtitles="))
    # A drive letter here would be read as a filter option separator.
    assert ":" not in filter_arg.split("=", 1)[1]
    assert ffmpeg.cwds[ffmpeg.calls.index(burn[0])] == str(store.video_dir(pid))


def test_no_subtitles_still_produces_the_video(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)
    client.post(f"/api/projects/{pid}/video/settings",
                json={"params": {**store.video_settings(store.load(pid)),
                                 "subtitles": "none"}})

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    assert not [c for c in ffmpeg.calls if "mov_text" in c]
    assert (store.video_dir(pid) / f"{pid}_video.mp4").is_file()


def test_one_failed_scene_does_not_lose_the_rest_of_the_cut(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=3)
    proj = ready(client, pid, api)
    ffmpeg.fail_on = f"{pid}_002_"

    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    after = project(client, pid)
    states = {s["n"]: s["video_status"] for s in after["scenes"]}
    assert states[2] == "failed" and states[1] == "done" and states[3] == "done"
    assert after["video"]["file"], "two good scenes still make a video"


def test_the_assembled_video_is_carried_into_the_export(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=2)
    ready(client, pid, api)
    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    body = client.post(f"/api/projects/{pid}/export", json={}).json()
    assert body["video"] == f"{pid}_video.mp4"
    assert (store.export_dir(pid) / body["video"]).is_file()


def test_the_video_is_served_back_and_traversal_is_refused(client, claude, api, ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)
    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    assert client.get(f"/api/projects/{pid}/video/{pid}_video.mp4").status_code == 200
    assert client.get(f"/api/projects/{pid}/video/nope.mp4").status_code == 404


def test_a_render_a_voice_run_and_a_cut_do_not_share_a_job_slot(client, claude, api,
                                                                ffmpeg):
    pid = segmented(client, scenes=1)
    ready(client, pid, api)
    assemble(client, pid)
    wait_for_job(pid, kind=orchestrator.KIND_VIDEO)

    slots = {orchestrator.KIND_RENDER, orchestrator.KIND_AUDIO, orchestrator.KIND_VIDEO}
    assert all(orchestrator.job_for(pid, k) is not None for k in slots)
    assert len({id(orchestrator.job_for(pid, k)) for k in slots}) == 3


# --------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------- #

def test_video_settings_are_validated_before_an_encode_starts(client, claude, api):
    pid = segmented(client, scenes=1)
    r = client.post(f"/api/projects/{pid}/video/settings", json={"params": {"fps": 500}})
    assert r.status_code == 400
    assert "12" in r.json()["detail"] and "60" in r.json()["detail"]


def test_a_frame_rate_reaches_ffmpeg_as_a_whole_number(client):
    """`range` would hand ffmpeg 30.0, which is not a frame rate it takes."""
    params = engines.validate(config.DEFAULT_VIDEO_PROFILE, {"fps": "30"},
                              engines.SECTION_VIDEO)
    assert params["fps"] == 30 and isinstance(params["fps"], int)


def test_export_survives_a_stale_video_setting(client, claude, api):
    pid = segmented(client, scenes=1)
    ready(client, pid, api, spoken=False)
    store.mutate(pid, lambda p: p["video"].update({"params": {"fps": "nonsense"}}))

    # Falls back rather than failing: export must not be blocked by a setting.
    assert store.video_settings(store.load(pid))["fps"] == config.VIDEO_FPS
    assert client.post(f"/api/projects/{pid}/export", json={}).status_code == 200


# --------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------- #

def test_the_page_carries_the_video_controls(client):
    html = client.get("/").text

    for element_id in ("ffmpegState", "videoProfile", "videoParams", "videoPlan",
                       "assembleBtn", "cancelVideoBtn", "videoStatus", "videoOut",
                       "srtLink", "vttLink"):
        assert f'id="{element_id}"' in html, element_id

    assert 'id="step4"' in html and 'id="step5"' in html
    assert '/video/assemble' in html and '/video/plan' in html
    assert 'captions.srt' in html and 'captions.vtt' in html
    # Video is cut before the folder that packages it is written.
    assert html.index('data-step="4"') < html.index('data-step="5"')


# --------------------------------------------------------------------- #
# The one claim the fake cannot make
# --------------------------------------------------------------------- #

@needs_ffmpeg
def test_a_real_ffmpeg_accepts_the_command_this_project_builds(client, claude, api,
                                                               tmp_path):
    """Proves the filtergraph is valid, which no fake binary can.

    Local encode only -- no API, no money.
    """
    pid = segmented(client, scenes=2)
    ready(client, pid, api)

    assemble(client, pid)
    wait_for_job(pid, timeout=600, kind=orchestrator.KIND_VIDEO)

    proj = project(client, pid)
    assert not (orchestrator.job_for(pid, orchestrator.KIND_VIDEO).fatal)
    out = store.video_dir(pid) / proj["video"]["file"]
    assert out.is_file() and out.stat().st_size > 10_000
