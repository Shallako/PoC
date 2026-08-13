"""Sort-safe naming (PRD §10).

Versioned asset stem : {project}_{NNN}_{slug}_v{VV}[_seed{SEED}]
Flattened export stem: {project}_{NNN}_{slug}
Narration shares the flattened stem so a NLE lines the pair up automatically.
"""

from __future__ import annotations

import re
import unicodedata

_SMART_QUOTES = re.compile(r"[‘’“”'\"]")

# Letters NFKD leaves alone, so an accent-folded slug does not silently lose them.
_FOLD = str.maketrans({"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d",
                       "ð": "d", "þ": "th", "ł": "l", "ħ": "h", "ı": "i"})


def slugify(title: str, limit: int = 60) -> str:
    """ASCII, lower case, hyphenated. Filenames stay portable whatever the story's
    language is: accents fold to their base letter, and a title in a script with no
    Latin form at all (Japanese, Arabic) falls back to `scene` -- the ordinal in the
    stem is what keeps those unique and in order."""
    s = (title or "").lower()
    s = s.replace("&", " and ")
    s = _SMART_QUOTES.sub("", s)
    s = unicodedata.normalize("NFKD", s.translate(_FOLD))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > limit:
        s = s[:limit].rstrip("-")
    return s or "scene"


def project_slug(name: str) -> str:
    return slugify(name, limit=40)


def asset_stem(project: str, n: int, slug: str, version: int = 1, seed: int | None = None) -> str:
    stem = f"{project}_{n:03d}_{slug}_v{version:02d}"
    if seed is not None:
        stem += f"_seed{seed}"
    return stem


def flat_stem(project: str, n: int, slug: str) -> str:
    return f"{project}_{n:03d}_{slug}"


def full_voiceover_stem(project: str) -> str:
    return f"{project}_full-voiceover"


def video_stem(project: str) -> str:
    return f"{project}_video"


def captions_stem(project: str) -> str:
    return f"{project}_captions"


def timing_stem(project: str) -> str:
    return f"{project}_timing"


# Narration audio shares the flattened image stem, so dropping export/ into an
# editor pairs picture and voice on the same row without renaming anything.
audio_stem = flat_stem

# The manifest is keyed by asset, and one scene now has two: an image keyed by
# its versioned stem, and a narration line keyed by this.
_NARRATION_KEY_SUFFIX = "_narration"


def narration_key(project: str, n: int, slug: str) -> str:
    return flat_stem(project, n, slug) + _NARRATION_KEY_SUFFIX
