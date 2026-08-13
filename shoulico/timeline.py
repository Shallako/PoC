"""When each scene holds the screen, and when its words are spoken.

One timeline, two consumers. Captions and the video assembly both derive every
number from here, because the alternative is two nearly-identical calculations
that drift by a frame and put the subtitles out of sync with the voice -- the
one defect in a finished video that is obvious to everyone and invisible in a
test that checks each half on its own.

Durations come from measured audio wherever audio exists. That is the whole
reason narration audio was built first: a word-count estimate ran ~30% long, and
a video cut to an estimate drifts further out of sync with every scene.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config, narration, store


@dataclass(frozen=True)
class Beat:
    """One scene's slot on the master timeline. All times in seconds."""

    n: int
    title: str
    slug: str
    image: Path
    audio: Path | None
    text: str
    start: float
    duration: float
    speech_start: float
    speech_seconds: float
    measured: bool

    @property
    def end(self) -> float:
        return round(self.start + self.duration, 3)

    @property
    def speech_end(self) -> float:
        return round(self.speech_start + self.speech_seconds, 3)


def canvas(project: dict, aspect: str) -> tuple[int, int]:
    """Pixel size for an aspect key, following the rendered images by default.

    A story rendered 9:16 that assembled into a 16:9 frame would be pillarboxed
    down the middle of its own video, which nobody wants and nobody asked for.
    """
    if aspect == config.VIDEO_ASPECT_SOURCE:
        aspect = str((project.get("params") or {}).get("aspect_ratio")
                     or config.DEFAULT_VIDEO_ASPECT)
    return config.VIDEO_CANVASES.get(aspect,
                                     config.VIDEO_CANVASES[config.DEFAULT_VIDEO_ASPECT])


def scene_seconds(scene: dict, *, lead_in: float, tail: float) -> tuple[float, float, bool]:
    """(total, speech, measured) for one scene.

    Measured audio wins. Where a line has no audio yet the estimate stands in, so
    a preview of the timeline works before a voice run and simply gets more
    accurate after one. A scene with no line at all still has to be looked at.
    """
    seconds = scene.get("audio_seconds")
    measured = bool(scene.get("audio_measured")) and scene.get("audio") is not None
    if not seconds:
        text = (scene.get("narration") or "").strip()
        seconds = narration.estimate_seconds(text) if text else 0.0
        measured = False
    speech = float(seconds or 0.0)

    total = speech + lead_in + tail if speech else float(config.DEFAULT_SECONDS_PER_SCENE)
    return max(total, config.VIDEO_MIN_SCENE_SECONDS), speech, measured


def build(project: dict, settings: dict) -> tuple[list[Beat], list[dict]]:
    """(beats, skipped). Only scenes with an image on disk can be shown."""
    pid = project["id"]
    lead_in = float(settings.get("lead_in", config.VIDEO_LEAD_IN_SECONDS))
    tail = float(settings.get("tail", config.VIDEO_TAIL_SECONDS))

    beats: list[Beat] = []
    skipped: list[dict] = []
    clock = 0.0

    for scene in sorted(project.get("scenes", []), key=lambda s: s["n"]):
        asset = scene.get("asset")
        image = store.images_dir(pid) / asset if asset else None
        if image is None or not image.is_file():
            skipped.append({"n": scene["n"], "title": scene.get("title", ""),
                            "reason_key": "no_image",
                            "reason": "no rendered image for this scene"})
            continue

        spoken = scene.get("audio")
        audio = store.audio_dir(pid) / spoken if spoken else None
        if audio is not None and not audio.is_file():
            audio = None

        total, speech, measured = scene_seconds(scene, lead_in=lead_in, tail=tail)
        beats.append(Beat(
            n=scene["n"],
            title=scene.get("title", ""),
            slug=scene.get("slug", ""),
            image=image,
            audio=audio,
            text=(scene.get("narration") or "").strip(),
            start=round(clock, 3),
            duration=round(total, 3),
            # Silence before the first word, so a cut never lands on a syllable.
            speech_start=round(clock + (lead_in if speech else 0.0), 3),
            speech_seconds=round(speech, 3),
            measured=measured,
        ))
        clock += total

    return beats, skipped


def total_seconds(beats: list[Beat]) -> float:
    return round(sum(b.duration for b in beats), 3)


def all_measured(beats: list[Beat]) -> bool:
    """True when every spoken beat's length came from real audio.

    False means the runtime is a projection, and the UI must say so rather than
    printing an estimate in the same typeface as a fact.
    """
    return all(b.measured for b in beats if b.speech_seconds)
