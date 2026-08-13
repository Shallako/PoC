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
NARRATION_WPM = 150                           # for duration estimates only
NARRATION_CPM = 350                           # ditto, for unspaced scripts (zh/ja)


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
