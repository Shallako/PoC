"""Sort-safe naming (PRD §10).

Versioned asset stem : {project}_{NNN}_{slug}_v{VV}[_seed{SEED}]
Flattened export stem: {project}_{NNN}_{slug}
Narration shares the flattened stem so a NLE lines the pair up automatically.
"""

from __future__ import annotations

import re

_SMART_QUOTES = re.compile(r"[‘’“”'\"]")


def slugify(title: str, limit: int = 60) -> str:
    s = (title or "").lower()
    s = s.replace("&", " and ")
    s = _SMART_QUOTES.sub("", s)
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
