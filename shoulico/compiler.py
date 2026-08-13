"""Story -> scenes -> engine-targeted prompts (FR-102, FR-104, FR-201..203).

Claude does the segmentation and writes the per-scene prompt bodies plus one
shared style block. The deterministic guardrail pass afterwards is what keeps
the engine honest -- it runs on every compile, including prompts the user has
hand-edited, so an edit can't reintroduce text the engine letters onto the image.
"""

from __future__ import annotations

import json
import logging
import re
import time

from . import config
from .naming import slugify

# Dialogue quoting is language-specific: French uses guillemets, German low/high
# quotes, Japanese corner brackets. The engine letters any of them onto the image,
# so all of them have to go. Single quotes are left alone -- across most languages
# they are apostrophes far more often than they are dialogue.
_QUOTED_DIALOGUE = re.compile(
    r"[\"“”][^\"“”]{1,200}[\"“”]"
    r"|«[^«»]{1,200}»"
    r"|[„‟][^“”„‟]{1,200}[“”]"
    r"|「[^「」]{1,200}」"
    r"|『[^『』]{1,200}』"
)


def strip_quoted_dialogue(text: str) -> str:
    """Quoted dialogue gets rendered as on-image lettering. Drop the quotes."""
    text = _QUOTED_DIALOGUE.sub(" ", text)
    # A clause lifted out mid-sentence leaves its commas behind on both sides.
    text = re.sub(r"\s*([,、，])(\s*[,、，])+", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,、，")


def compile_prompt(body: str, style_block: str, dialect: dict | None = None) -> str:
    """Scene body first, shared style block last -- the order the engine reads best."""
    dialect = dialect or {}
    text = body or ""
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
        "language": {
            "type": "object",
            "description": "The language the story itself is written in.",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "BCP-47 tag, e.g. 'en', 'fr', 'pt-BR', 'ja'. "
                                   "Include the region only when it changes the wording.",
                },
                "name": {"type": "string", "description": "English name, e.g. 'French'."},
                "native_name": {
                    "type": "string",
                    "description": "What that language calls itself, e.g. 'Français'.",
                },
            },
            "required": ["code", "name", "native_name"],
            "additionalProperties": False,
        },
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
    "required": ["language", "style_profile", "scenes"],
    "additionalProperties": False,
}

SEGMENT_SYSTEM = """You are the scene segmenter and prompt compiler for a local \
story-to-image tool. You turn a short story into an ordered set of visual beats and \
one engine-targeted image prompt per beat.

Rules for the prompts you write:
- Write for a diffusion image model, not for a person. Describe what is visible: \
subject, action, staging, camera framing, lighting, mood.
- Never use command-line-style flags (--ar, --v, --stylize and the like). The engine \
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
prose the engine can read, not as a bullet list.

Language:
- Work out which language the story is written in and report it in `language`.
- Unless you are told otherwise, write everything you produce -- title, beat, prompt and \
style_profile -- in that same language, in its own idiom and punctuation. The author \
reads and edits every one of these fields, so they have to come back in the language the \
author wrote in. Never translate the story into English on the way past.
- A term the image engine only understands in English (a camera or lens name, a named \
film stock, an art movement) stays in English inside the otherwise native-language text."""


log = logging.getLogger("shoulico.claude")

# 529 "overloaded" is Anthropic-side capacity, not our request. The SDK's own
# retries are deliberately impatient (sub-second, twice), which is right for a
# blip and useless for a busy few minutes -- so we ladder on top of them, the
# way renderful.api_call does for 5xx.
CLAUDE_ATTEMPTS = 4                    # tries on the requested model
CLAUDE_BACKOFF = (3, 8, 20)            # seconds before attempts 2, 3, 4
SDK_RETRIES = 2                        # the SDK's fast retries inside each attempt


class ClaudeError(RuntimeError):
    """A Claude failure that can be explained in a sentence.

    `kind` is what the caller maps to an HTTP status; `status`, `request_id` and
    `retry_after` are the machine facts, which the UI keeps behind a details
    toggle so the sentence itself stays readable.
    """

    def __init__(self, message: str, *, kind: str = "transient",
                 status: int | None = None, request_id: str | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.request_id = request_id
        self.retry_after = retry_after


def _client(api_key: str | None):
    import anthropic

    kwargs = {"max_retries": SDK_RETRIES}
    if api_key:
        kwargs["api_key"] = api_key
    return anthropic.Anthropic(**kwargs)


# --------------------------------------------------------------------------- #
# Classifying what came back
#
# Duck-typed rather than isinstance-based on the SDK's exception classes: an
# overload can surface either as an HTTP status on the initial request or as an
# error event mid-stream, and those have not always been the same class. Reading
# the status and the error type off whatever we caught survives both.
# --------------------------------------------------------------------------- #

def _status_of(exc) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _error_type(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type"):
            return str(err["type"])
    text = str(exc)
    for marker in ("overloaded_error", "rate_limit_error",
                   "authentication_error", "permission_error"):
        if marker in text:
            return marker
    return ""


def _request_id(exc) -> str | None:
    rid = getattr(exc, "request_id", None)
    if rid:
        return str(rid)
    match = re.search(r"req_[A-Za-z0-9]+", str(exc))
    return match.group(0) if match else None


def _retry_after(exc) -> float | None:
    """The server's own answer to "how long?", when it sends one."""
    try:
        raw = exc.response.headers.get("retry-after")
    except Exception:  # noqa: BLE001 - no response, no header, no problem
        return None
    try:
        return max(0.0, min(float(raw), 120.0))
    except (TypeError, ValueError):
        return None


def classify(exc) -> str:
    """'overloaded' | 'rate_limit' | 'auth' | 'transient' | 'fatal'."""
    status, kind = _status_of(exc), _error_type(exc)
    if status == 529 or kind == "overloaded_error":
        return "overloaded"
    if status == 429 or kind == "rate_limit_error":
        return "rate_limit"
    if status in (401, 403) or kind in ("authentication_error", "permission_error"):
        return "auth"
    if status is not None:
        return "transient" if status >= 500 or status == 408 else "fatal"
    name = type(exc).__name__
    return "transient" if ("Connection" in name or "Timeout" in name) else "fatal"


def _explain(exc, kind: str, *, attempts: int, waited: float) -> ClaudeError:
    """The sentence the user reads instead of a raw SDK exception."""
    over = f"after {attempts} tries over {round(waited)} seconds"
    if kind == "overloaded":
        message = (
            f"Claude is overloaded right now and turned the request away {over}. "
            f"Anthropic refused it before the model ran, so nothing was charged. "
            f"Wait a minute and press the button again."
        )
    elif kind == "rate_limit":
        after = _retry_after(exc)
        wait = f"{int(after)} seconds" if after else "a minute"
        message = (
            f"Your Anthropic account is being rate limited {over}. Nothing was "
            f"charged for a rejected request. Wait {wait} and try again, or check "
            f"the limits on your account at console.anthropic.com."
        )
    elif kind == "auth":
        message = (
            "Anthropic rejected the API key, so nothing was charged. Check "
            "ANTHROPIC_API_KEY or anthropic_key.txt -- a working key starts with "
            "'sk-ant-' and has to be active on a workspace with credit."
        )
    else:
        message = f"Claude could not be reached {over}: {exc}"
    return ClaudeError(message, kind=kind, status=_status_of(exc),
                       request_id=_request_id(exc), retry_after=_retry_after(exc))


def _stream_once(client, system: str, user: str, schema: dict, model: str, max_tokens: int):
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
        return stream.get_final_message()


def _call(system: str, user: str, schema: dict, api_key: str | None,
          model: str, max_tokens: int, meta: dict | None):
    """Ride out a capacity blip on the requested model, then try the fallback.

    Only capacity and connection failures are retried. A bad request or an
    unknown error is re-raised untouched -- retrying those burns time and never
    succeeds, and the caller already reports them verbatim.
    """
    client = _client(api_key)
    plan = [model] * CLAUDE_ATTEMPTS
    fallback = config.FALLBACK_CLAUDE_MODEL
    if fallback and fallback != model:
        plan.append(fallback)

    waited, last, kind = 0.0, None, "transient"
    for i, attempt_model in enumerate(plan):
        if i:
            delay = _retry_after(last) or CLAUDE_BACKOFF[min(i - 1, len(CLAUDE_BACKOFF) - 1)]
            waited += delay
            time.sleep(delay)
        try:
            message = _stream_once(client, system, user, schema, attempt_model, max_tokens)
        except Exception as exc:  # noqa: BLE001 - classified on the next line
            kind, last = classify(exc), exc
            if kind == "auth":
                raise _explain(exc, kind, attempts=i + 1, waited=waited) from None
            if kind == "fatal":
                raise
            log.warning("Claude %s from %s (attempt %d of %d): %s",
                        kind, attempt_model, i + 1, len(plan), exc)
            continue
        if meta is not None:
            meta.update(model=attempt_model, attempts=i + 1,
                        fell_back=attempt_model != model)
        if attempt_model != model:
            log.warning("Claude: %s stayed unavailable, so %s answered instead.",
                        model, attempt_model)
        return message

    raise _explain(last, kind, attempts=len(plan), waited=waited) from None


def _structured_call(system: str, user: str, schema: dict, api_key: str | None,
                     model: str, max_tokens: int = 32000,
                     meta: dict | None = None) -> dict:
    """One streamed, schema-constrained Claude call, ridden through overloads."""
    message = _call(system, user, schema, api_key, model, max_tokens, meta)

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


DEFAULT_LANGUAGE = {"code": "en", "name": "English", "native_name": "English"}

# Prompts follow the story's language by default (the author has to be able to read
# and edit them). "en" is the escape hatch for an engine that only behaves in English.
PROMPT_LANGUAGE_CHOICES = ("story", "en")

_PROMPTS_IN_ENGLISH = (
    "Exception to the language rule: write `prompt` and `style_profile` in English, "
    "whatever language the story is in. `title` and `beat` still go in the story's "
    "language -- those are for the author, not the engine.\n"
)


def normalize_language(raw) -> dict:
    """Whatever came back from the model, reduced to a usable {code,name,native_name}."""
    raw = raw if isinstance(raw, dict) else {}
    code = str(raw.get("code") or "").strip()[:16]
    name = str(raw.get("name") or "").strip()[:60]
    if not code and not name:
        return dict(DEFAULT_LANGUAGE)
    return {
        "code": code or "en",
        "name": name or code,
        "native_name": str(raw.get("native_name") or "").strip()[:60] or name or code,
    }


def segment(story: str, scene_count: int, *, style_hint: str = "",
            api_key: str | None = None,
            model: str = config.DEFAULT_CLAUDE_MODEL,
            engine_name: str = "Seedream 5.0 Pro",
            dialect_notes: list[str] | None = None,
            prompt_language: str = "story") -> dict:
    """Return {'language','model','fell_back','style_profile','scenes'}."""
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
        + (f"\n{_PROMPTS_IN_ENGLISH}" if prompt_language == "en" else "")
        + f"\nSegment this story into exactly {scene_count} visual beats, in story order, "
          f"and write one image prompt for each.\n"
        + (f"\nStyle direction from the author (honour it in style_profile):\n{style_hint.strip()}\n"
           if style_hint.strip() else "")
        + f"\n<story>\n{story}\n</story>"
    )

    meta: dict = {}
    data = _structured_call(SEGMENT_SYSTEM, user, SEGMENT_SCHEMA, api_key, model, meta=meta)

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

    return {
        "language": normalize_language(data.get("language")),
        # Which model actually wrote these scenes -- not always the one we asked
        # for, if the first choice was saturated.
        "model": meta.get("model") or model,
        "fell_back": bool(meta.get("fell_back")),
        "style_profile": (data.get("style_profile") or "").strip(),
        "scenes": scenes,
    }
