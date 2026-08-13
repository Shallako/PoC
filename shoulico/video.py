"""Local video assembly with ffmpeg.

One segment per scene, then a stream copy to join them. That shape is deliberate:
a single giant filtergraph is faster to write and impossible to resume, report
progress on, or cancel, and this app already reports per-scene progress for
renders and for speech. A scene that fails leaves the other segments on disk.

ffmpeg is the only external binary this project needs, and it is optional: every
entry point reports its absence as a fact the UI can act on rather than raising
somewhere deep in a worker thread. Captions and the CapCut hand-off are produced
without it.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config
from .timeline import Beat

# ffmpeg writes progress to stderr, so a failure's cause is at the end of a very
# long stream. This is how much of the tail is worth showing a human.
_STDERR_TAIL_LINES = 12

MOTION_NONE = "none"
MOTION_KEN_BURNS = "ken-burns"

SUBTITLES_NONE = "none"
SUBTITLES_SOFT = "soft"
SUBTITLES_BURN = "burn"

# Zoom towards the frame centre. Panning off-centre crops faces out of shot often
# enough that centred is the honest default.
_ZOOM_X = "iw/2-(iw/zoom/2)"
_ZOOM_Y = "ih/2-(ih/zoom/2)"


class FfmpegMissing(RuntimeError):
    """ffmpeg is not installed. Recoverable: captions and export still work."""


class FfmpegError(RuntimeError):
    """ffmpeg ran and failed. Carries the tail of its own diagnostics."""


@dataclass(frozen=True)
class Encoded:
    path: Path
    seconds: float


def ffmpeg_path() -> str | None:
    """Absolute path to ffmpeg, or None. Honours SHOULICO_FFMPEG."""
    return shutil.which(config.FFMPEG_BINARY)


def available() -> bool:
    return ffmpeg_path() is not None


def version() -> str | None:
    """First line of `ffmpeg -version`, or None if it is not installed."""
    exe = ffmpeg_path()
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "-version"], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    first = (done.stdout or "").splitlines()
    return first[0].strip() if first else None


def install_hint() -> str:
    """What to actually type. A missing dependency with no remedy is just a wall."""
    return ("ffmpeg is not on PATH. Install it with `winget install Gyan.FFmpeg` "
            "(Windows), `brew install ffmpeg` (macOS) or your package manager, then "
            "restart the app. Set SHOULICO_FFMPEG to the full path if it is "
            "installed somewhere PATH does not reach.")


def _run(args: list[str], *, timeout: int, cwd: Path | None = None) -> str:
    exe = ffmpeg_path()
    if not exe:
        raise FfmpegMissing(install_hint())
    try:
        done = subprocess.run(
            [exe, "-y", "-hide_banner", "-nostdin", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        raise FfmpegError(f"ffmpeg timed out after {timeout}s") from None
    except OSError as e:
        raise FfmpegError(f"could not run ffmpeg: {e}") from None

    if done.returncode != 0:
        tail = "\n".join((done.stderr or "").strip().splitlines()[-_STDERR_TAIL_LINES:])
        raise FfmpegError(tail or f"ffmpeg exited {done.returncode}")
    return done.stderr or ""


def _zoom_expression(frames: int, zoom: float, *, outward: bool) -> str:
    """Linear zoom across the scene.

    Linear in the frame counter rather than the usual `zoom+step` accumulator:
    the accumulator's final zoom depends on how many frames it happened to run
    for, so two scenes of different lengths end at different sizes.
    """
    if frames <= 1:
        return "1"
    span = zoom - 1.0
    ramp = f"{span:.6f}*on/{frames - 1}"
    return f"{zoom:.6f}-{ramp}" if outward else f"1+{ramp}"


def _video_filter(beat: Beat, *, width: int, height: int, fps: int,
                  motion: str, zoom: float) -> tuple[list[str], str]:
    """(input args for the image, video filter chain)."""
    if motion != MOTION_KEN_BURNS:
        # A still needs looping to hold the screen; there is no filter making
        # frames here, so the input has to supply them.
        return (
            ["-loop", "1", "-framerate", str(fps), "-t", f"{beat.duration:.3f}",
             "-i", str(beat.image)],
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p[v]",
        )

    frames = max(1, round(beat.duration * fps))
    sample = config.VIDEO_KEN_BURNS_SUPERSAMPLE
    sw, sh = width * sample, height * sample
    # Alternating direction, chosen by scene number rather than at random so a
    # re-run of one scene matches the cut it is replacing.
    outward = beat.n % 2 == 0
    z = _zoom_expression(frames, zoom, outward=outward)

    # Deliberately NOT looped: zoompan expands one input frame into `d` output
    # frames, so a looped input would multiply out to frames * duration * fps.
    return (
        ["-i", str(beat.image)],
        f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},"
        f"zoompan=z='{z}':d={frames}:x='{_ZOOM_X}':y='{_ZOOM_Y}'"
        f":s={width}x{height}:fps={fps},"
        f"setsar=1,format=yuv420p[v]",
    )


def _audio_filter(beat: Beat, lead_in: float) -> tuple[list[str], str]:
    """(input args for the sound, audio filter chain).

    Every segment carries an audio stream even when its scene is silent: the
    join is a stream copy, and a segment missing a stream breaks the copy.
    """
    rate = config.VIDEO_AUDIO_RATE
    layout = "stereo" if config.VIDEO_AUDIO_CHANNELS == 2 else "mono"
    if beat.audio is None:
        return (
            ["-f", "lavfi", "-t", f"{beat.duration:.3f}",
             "-i", f"anullsrc=r={rate}:cl={layout}"],
            f"[1:a]aformat=sample_rates={rate}:channel_layouts={layout}[a]",
        )

    delay_ms = int(round(max(0.0, lead_in) * 1000))
    return (
        ["-i", str(beat.audio)],
        # adelay puts the silence in front, apad supplies the tail, atrim makes
        # the stream exactly as long as the picture so the join stays aligned.
        f"[1:a]aformat=sample_rates={rate}:channel_layouts={layout},"
        f"adelay={delay_ms}:all=1,apad,atrim=0:{beat.duration:.3f},"
        f"asetpts=N/SR/TB[a]",
    )


def encode_segment(beat: Beat, dest: Path, *, width: int, height: int,
                   fps: int = config.VIDEO_FPS, motion: str = MOTION_KEN_BURNS,
                   zoom: float = config.VIDEO_KEN_BURNS_ZOOM,
                   lead_in: float = config.VIDEO_LEAD_IN_SECONDS) -> Encoded:
    """One scene: its picture, its voice, its exact slice of the timeline."""
    v_in, v_chain = _video_filter(beat, width=width, height=height, fps=fps,
                                  motion=motion, zoom=zoom)
    a_in, a_chain = _audio_filter(beat, lead_in)

    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        *v_in, *a_in,
        "-filter_complex", f"{v_chain};{a_chain}",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{beat.duration:.3f}",
        "-c:v", "libx264", "-preset", config.VIDEO_PRESET,
        "-crf", str(config.VIDEO_CRF), "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", config.VIDEO_AUDIO_BITRATE,
        "-ar", str(config.VIDEO_AUDIO_RATE), "-ac", str(config.VIDEO_AUDIO_CHANNELS),
        "-movflags", "+faststart",
        str(dest),
    ], timeout=config.FFMPEG_SEGMENT_TIMEOUT)
    return Encoded(path=dest, seconds=beat.duration)


def join(segments: list[Path], dest: Path, *, workdir: Path) -> Path:
    """Concatenate finished segments without re-encoding them.

    Every segment was written with identical codec settings by `encode_segment`,
    which is what makes a stream copy legal here -- and what makes the join take
    a second instead of another full encode.
    """
    if not segments:
        raise FfmpegError("nothing to join: no scene segments were produced")

    workdir.mkdir(parents=True, exist_ok=True)
    listing = workdir / "segments.txt"
    # Relative names with cwd=workdir: concat quotes on ' and a Windows absolute
    # path also carries a drive colon, and fighting both escapes is not worth it.
    listing.write_text(
        "".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")

    _run(["-f", "concat", "-safe", "0", "-i", listing.name,
          "-c", "copy", "-movflags", "+faststart", str(dest)],
         timeout=config.FFMPEG_JOIN_TIMEOUT, cwd=workdir)
    return dest


def add_subtitles(source: Path, subtitles: Path, dest: Path, *, mode: str,
                  workdir: Path, fps: int = config.VIDEO_FPS) -> Path:
    """Attach captions as a track, or burn them into the picture."""
    if mode == SUBTITLES_SOFT:
        # mov_text is the subtitle codec MP4 actually supports; a copy of the
        # picture and sound means this costs no quality and almost no time.
        _run(["-i", str(source), "-i", str(subtitles),
              "-c", "copy", "-c:s", "mov_text", str(dest)],
             timeout=config.FFMPEG_JOIN_TIMEOUT)
        return dest

    if mode == SUBTITLES_BURN:
        # Relative paths with cwd again: the subtitles filter parses its own
        # argument, so "C:\..." arrives as a filter option separator plus a path.
        _run(["-i", source.name, "-vf", f"subtitles={subtitles.name}",
              "-c:v", "libx264", "-preset", config.VIDEO_PRESET,
              "-crf", str(config.VIDEO_CRF), "-pix_fmt", "yuv420p", "-r", str(fps),
              "-c:a", "copy", "-movflags", "+faststart", dest.name],
             timeout=config.FFMPEG_JOIN_TIMEOUT, cwd=workdir)
        return dest

    raise FfmpegError(f"unknown subtitle mode {mode!r}")
