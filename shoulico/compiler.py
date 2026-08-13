"""Story -> scenes -> engine-targeted prompts (FR-102, FR-104, FR-201..203).

Claude does the segmentation and writes the per-scene prompt bodies plus one
shared style block. The deterministic guardrail pass afterwards is what keeps
the engine honest -- it runs on every compile, including prompts the user has
hand-edited, so an edit can't reintroduce a flag the engine reads as text.
"""

from __future__ import annotations

import json
import re

from . import config
from .naming import slugify

MJ_FLAGS = re.compile(
    r"\s--(?:sref|cref|srefs|style|stylize|sw|cw|ar|v|niji|q|quality|chaos|c|s|seed|"
    r"stop|tile|weird|iw|repeat|r|no|p|profile|raw|fast|relax|turbo)\b"
    r"(?:\s+(?:https?://\S+|[^\s-][^\s]*))?",
    re.IGNORECASE,
)

_DOUBLE_QUOTED = re.compile(r"[\"“”][^\"“”]{1,200}[\"“”]")


def strip_mj_flags(text: str) -> str:
    text = MJ_FLAGS.sub(" ", " " + text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


def strip_quoted_dialogue(text: str) -> str:
    """Quoted dialogue gets rendered as on-image lettering. Drop the quotes."""
    text = _DOUBLE_QUOTED.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


def compile_prompt(body: str, style_block: str, dialect: dict | None = None) -> str:
    """Scene body first, shared style block last -- the order the engine reads best."""
    dialect = dialect or {}
    text = body or ""
    if dialect.get("strip_mj_flags", True):
        text = strip_mj_flags(text)
    if dialect.get("strip_quoted_dialogue", True):
        text = strip_quoted_dialogue(text)
    text = text.strip()
    style = (style_block or "").strip()
    return f"{text} {style}".strip() if style else text


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "style_profile": {
            "type": "string",
            "description": "One shared style/consistency block appended to every prompt.",
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ordinal": {"type": "integer"},
                    "title": {"type": "string"},
                    "beat": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["ordinal", "title", "beat", "prompt"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["style_profile", "scenes"],
    "additionalProperties": False,
}

SEGMENT_SYSTEM = """You are the scene segmenter and prompt compiler for a local \
story-to-image tool. You turn a short story into an ordered set of visual beats and \
one engine-targeted image prompt per beat.

Rules for the prompts you write:
- Write for a diffusion image model, not for a person. Describe what is visible: \
subject, action, staging, camera framing, lighting, mood.
- Never use Midjourney-style flags (--ar, --v, --sref, --sw, --stylize). The engine \
reads them as literal words. Aspect ratio is a separate API parameter.
- Never put quoted dialogue or any words meant to appear in the picture into a prompt: \
the engine renders quoted text as lettering on the image. Convey speech through facial \
expression and gesture instead.
- The engine cannot count reliably. When a count matters, state it plainly and simply \
("exactly four players, one to a side").
- Phrase exclusions positively where you can. There is no negative-prompt parameter; \
anything you want excluded has to be said in words inside the prompt.
- Every person you describe must read unambiguously as an adult. Give an explicit adult \
age the first time a character appears in a prompt ("a man of twenty-six") and never use \
comparative or diminutive age words for them ("the younger cousin", "young man", "boy", \
"girl", "kid"). Image-engine safety filters read those words as references to minors and \
reject the entire prompt.
- Keep each scene prompt self-contained: a reader who has not seen the other scenes \
should still be able to picture it.
- Do not repeat the shared style block inside individual scene prompts. It is appended \
automatically to every one of them.

The style_profile is that shared block. It carries everything that must stay identical \
across the whole set: art style and rendering, period and setting, recurring character \
descriptions (build, age, hair, clothing, distinguishing features), casting, wardrobe \
and prop rules, and any hard "no text in the image" instruction. Write it as flowing \
prose the engine can read, not as a bullet list."""


def _client(api_key: str | None):
    import anthropic

    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def _structured_call(system: str, user: str, schema: dict, api_key: str | None,
                     model: str, max_tokens: int = 32000) -> dict:
    """One streamed, schema-constrained Claude call."""
    client = _client(api_key)
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RuntimeError(
            "Claude declined this request"
            + (f" (category: {category})" if category else "")
            + ". Edit the story text and try again."
        )
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude hit the output limit before finishing. Try fewer scenes, "
            "or a shorter story."
        )

    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text.strip():
        raise RuntimeError("Claude returned an empty response.")
    return json.loads(text)


def segment(story: str, scene_count: int, *, style_hint: str = "",
            api_key: str | None = None,
            model: str = config.DEFAULT_CLAUDE_MODEL,
            engine_name: str = "Seedream 5.0 Pro",
            dialect_notes: list[str] | None = None) -> dict:
    """Return {'style_profile': str, 'scenes': [{n, title, slug, beat, body}]}."""
    story = (story or "").strip()
    if not story:
        raise ValueError("The story is empty.")
    if len(story) > config.MAX_STORY_CHARS:
        raise ValueError(
            f"The story is {len(story)} characters; the limit is {config.MAX_STORY_CHARS}."
        )
    scene_count = max(1, min(int(scene_count), config.MAX_SCENE_COUNT))

    notes = "\n".join(f"- {n}" for n in (dialect_notes or []))
    user = (
        f"Target image engine: {engine_name}.\n"
        + (f"Engine quirks to write around:\n{notes}\n" if notes else "")
        + f"\nSegment this story into exactly {scene_count} visual beats, in story order, "
          f"and write one image prompt for each.\n"
        + (f"\nStyle direction from the author (honour it in style_profile):\n{style_hint.strip()}\n"
           if style_hint.strip() else "")
        + f"\n<story>\n{story}\n</story>"
    )

    data = _structured_call(SEGMENT_SYSTEM, user, SEGMENT_SCHEMA, api_key, model)

    scenes = []
    for i, raw in enumerate(data.get("scenes", []), start=1):
        title = (raw.get("title") or f"scene {i}").strip()
        scenes.append({
            "n": int(raw.get("ordinal") or i),
            "title": title,
            "slug": slugify(title),
            "beat": (raw.get("beat") or "").strip(),
            "body": (raw.get("prompt") or "").strip(),
        })
    scenes.sort(key=lambda s: s["n"])
    # Renumber so the ordinals are always dense and 1-based, whatever came back.
    for i, s in enumerate(scenes, start=1):
        s["n"] = i
    if not scenes:
        raise RuntimeError("Claude returned no scenes.")

    return {"style_profile": (data.get("style_profile") or "").strip(), "scenes": scenes}
