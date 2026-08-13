"""Paths, defaults and credential lookup.

Keys are read from disk/env on demand and are never logged, echoed to the
browser, or written into a project file (PRD NFR-4 / §11).
"""

from __future__ import annotations

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
APP_DIR = PKG_DIR.parent                     # holds run.py, engines.json, projects/
PROJECTS_DIR = APP_DIR / "projects"
ENGINES_FILE = APP_DIR / "engines.json"
STATIC_DIR = PKG_DIR / "static"
I18N_DIR = APP_DIR / "i18n"                   # cached UI translations, one file per language

RENDERFUL_API_BASE = "https://api.renderful.ai/api/v1"
DEFAULT_ENGINE = "seedream-5.0-pro"

# Claude does the segmentation, prompt compilation and narration script.
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

# Last resort when the model above keeps answering 529 "overloaded". Opus is the
# first tier to saturate; Sonnet handles these schema-constrained calls well and
# is far less likely to be turned away. Set to "" to fail instead of falling back.
FALLBACK_CLAUDE_MODEL = "claude-sonnet-5"

MAX_STORY_CHARS = 5000                        # FR-101
DEFAULT_SCENE_COUNT = 12
MAX_SCENE_COUNT = 40
WORKERS = 3                                   # concurrent generations

# Pre-flight duration estimates only. A live measurement replaces these the
# moment narration audio exists: 13 words measured 4.0s against the 5.2s these
# predict, so they run roughly 30% long.
NARRATION_WPM = 150
NARRATION_CPM = 350                           # for unspaced scripts (zh/ja)

# --------------------------------------------------------------------------- #
# Narration audio (TTS)
# --------------------------------------------------------------------------- #

DEFAULT_VOICE = "eleven_flash_v2_5"
DEFAULT_SECONDS_PER_SCENE = 8
# ElevenLabs caps a single request at 40k characters; a narration line is two
# orders of magnitude below that, so hitting it means something upstream is wrong.
MAX_TTS_CHARS = 40000
# TTS completed in ~5s live. The 3600s image ceiling exists because a paid render
# once finished late; nothing about a speech job justifies that wait.
AUDIO_POLL_TIMEOUT = 300
AUDIO_POLL_SECONDS = 2


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #

# Subtitle conventions, not invented numbers. 42 characters per line and two
# lines is the widely used broadcast/streaming ceiling (Netflix 42, BBC ~37-40);
# 5/6 of a second is the standard minimum a cue may hold the screen, and seven
# seconds the maximum before a reader has finished and is waiting.
CAPTION_MAX_CHARS_PER_LINE = 42
CAPTION_MAX_LINES = 2
CAPTION_MIN_SECONDS = 5 / 6
CAPTION_MAX_SECONDS = 7.0
# Consecutive cues that share a frame boundary look like one flickering cue.
CAPTION_GAP_SECONDS = 0.08

# --------------------------------------------------------------------------- #
# Video assembly (local ffmpeg)
# --------------------------------------------------------------------------- #

# Overridable because Windows installs land ffmpeg anywhere; PATH is only the
# common case, never an assumption.
FFMPEG_BINARY = os.environ.get("SHOULICO_FFMPEG") or "ffmpeg"
# A long story is a long encode, and a still-image encode is not fast. This is a
# ceiling on one scene, not on the whole assembly.
FFMPEG_SEGMENT_TIMEOUT = 900
FFMPEG_JOIN_TIMEOUT = 1800

DEFAULT_VIDEO_PROFILE = "ffmpeg"
DEFAULT_VIDEO_ASPECT = "16:9"
# "source" follows whatever aspect ratio the images were actually rendered at,
# so a 9:16 story does not get letterboxed into a 16:9 frame by default.
VIDEO_ASPECT_SOURCE = "source"
VIDEO_CANVASES = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}

# A cut landing on the last syllable sounds clipped, and a line starting on the
# first frame of a new picture reads as a mistake. These are the breathing room.
VIDEO_LEAD_IN_SECONDS = 0.35
VIDEO_TAIL_SECONDS = 0.65
# A scene has to hold long enough to be seen even when its line is two words.
VIDEO_MIN_SCENE_SECONDS = 1.5

# Ken Burns. zoompan reads its x/y in whole input pixels, so a still at output
# size visibly steps; sampling from a larger intermediate is what makes the move
# smooth. Two is the point where it stops being visible and before memory hurts.
VIDEO_KEN_BURNS_SUPERSAMPLE = 2
VIDEO_KEN_BURNS_ZOOM = 1.12

VIDEO_FPS = 30
VIDEO_CRF = 20
VIDEO_PRESET = "medium"
VIDEO_AUDIO_BITRATE = "192k"
VIDEO_AUDIO_RATE = 48000
VIDEO_AUDIO_CHANNELS = 2


def _from_file(*candidates: Path) -> str | None:
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text.splitlines()[0].strip()
        except OSError:
            continue
    return None


def renderful_key() -> str | None:
    """env RENDERFUL_API_KEY -> api_key.txt beside the app -> api_key.txt one level up."""
    env = os.environ.get("RENDERFUL_API_KEY")
    if env and env.strip():
        return env.strip()
    return _from_file(APP_DIR / "api_key.txt", APP_DIR.parent / "api_key.txt")


def anthropic_key() -> str | None:
    """env ANTHROPIC_API_KEY -> anthropic_key.txt beside the app.

    Returning None is fine: the Anthropic SDK also resolves an `ant auth login`
    profile on its own, so we only pass a key when we actually found one.
    """
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env and env.strip():
        return env.strip()
    return _from_file(APP_DIR / "anthropic_key.txt", APP_DIR.parent / "anthropic_key.txt")


def key_status() -> dict:
    """Presence + a shape sanity check. The keys themselves never leave the server.

    A key that is present but malformed is worse than a missing one: it shows a
    green light and then fails with a 401 at the moment you try to use it.
    """
    anthropic = anthropic_key()
    warning = None
    if anthropic and not anthropic.startswith("sk-ant-"):
        warning = ("The Anthropic key does not start with 'sk-ant-', so it is probably "
                   "not an API key. Get one from https://console.anthropic.com/settings/keys")
    return {
        "renderful": bool(renderful_key()),
        "anthropic": bool(anthropic),
        "anthropic_warning": warning,
    }
