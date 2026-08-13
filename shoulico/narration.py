"""Narration *script* generation (FR-1201..FR-1206).

Script only -- no TTS, no audio. The script is reviewable and editable before
anything else happens to it, and per-scene lines are written to files that share
the image stem so an editor lines them up automatically.
"""

from __future__ import annotations

from . import config
from .compiler import _structured_call

NARRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ordinal": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["ordinal", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lines"],
    "additionalProperties": False,
}

NARRATION_SYSTEM = """You write voice-over narration scripts. You are writing words \
that will be read aloud over a sequence of still images, so:

- Write for the ear. Short sentences, plain words, natural spoken rhythm.
- Narrate; do not describe the picture. The viewer can already see the image. Say the \
thing the image cannot say: what it means, what happened before it, what is at stake.
- Keep the narrator's voice consistent across every line.
- No stage directions, no speaker labels, no timecodes, no bracketed notes, no markdown. \
Only the words to be spoken.
- Consecutive lines must read as one continuous piece when played back to back."""


def generate(story: str, scenes: list[dict], *, voice: str = "",
             seconds_per_scene: int = 8,
             api_key: str | None = None,
             model: str = config.DEFAULT_CLAUDE_MODEL) -> dict[int, str]:
    """Return {scene_ordinal: narration text}."""
    story = (story or "").strip()
    if not story:
        raise ValueError("The story is empty.")
    if not scenes:
        raise ValueError("Segment the story into scenes first.")

    words = max(8, int(seconds_per_scene * config.NARRATION_WPM / 60))
    beats = "\n".join(
        f"{s['n']}. {s.get('title', '')} — {s.get('beat') or s.get('body', '')[:200]}"
        for s in scenes
    )
    user = (
        f"Write one narration line for each of the {len(scenes)} beats below, in order.\n"
        f"Aim for about {words} words per line (roughly {seconds_per_scene} seconds "
        f"read aloud). Vary the length where the beat calls for it.\n"
        + (f"\nNarrator voice / tone: {voice.strip()}\n" if voice.strip() else "")
        + f"\n<story>\n{story}\n</story>\n\n<beats>\n{beats}\n</beats>"
    )

    data = _structured_call(NARRATION_SYSTEM, user, NARRATION_SCHEMA, api_key, model,
                            max_tokens=16000)

    by_ordinal: dict[int, str] = {}
    lines = data.get("lines", [])
    for i, raw in enumerate(lines, start=1):
        try:
            n = int(raw.get("ordinal") or i)
        except (TypeError, ValueError):
            n = i
        by_ordinal[n] = (raw.get("text") or "").strip()

    # Fall back to positional matching if the model's ordinals don't line up.
    if sorted(by_ordinal) != sorted(s["n"] for s in scenes) and len(lines) == len(scenes):
        by_ordinal = {
            s["n"]: (lines[i].get("text") or "").strip()
            for i, s in enumerate(scenes)
        }
    return by_ordinal


def word_count(text: str) -> int:
    return len((text or "").split())


def estimate_seconds(text: str) -> float:
    return round(word_count(text) / config.NARRATION_WPM * 60, 1)


def full_script(scenes: list[dict]) -> str:
    """The continuous single-track version (FR-1201 mode a)."""
    parts = []
    for s in sorted(scenes, key=lambda x: x["n"]):
        line = (s.get("narration") or "").strip()
        if line:
            parts.append(line)
    return "\n\n".join(parts)
