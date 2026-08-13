"""Data-driven engine registry + parameter validation.

The registry drives both the UI controls and the prompt compiler (FR-403,
FR-501, FR-1101). It lives in engines.json next to run.py so you can add an
engine without touching code.

Validation is deliberately strict and runs *before* anything is submitted:
Renderful bills a request that it later rejects for a bad parameter, so an
unknown key or an out-of-schema value has to fail locally, for free.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import config

_lock = threading.Lock()
_cache: dict | None = None

# Only seedream-5.0-pro is confirmed against a live Renderful account (it is the
# model the Boston set was rendered with). Everything else you add here is
# unverified: set "verified": false so the UI can warn before you spend.
DEFAULT_REGISTRY: dict[str, Any] = {
    "default": "seedream-5.0-pro",
    "engines": {
        "seedream-5.0-pro": {
            "name": "Seedream 5.0 Pro",
            "provider": "ByteDance (via Renderful)",
            "strength": "Photorealism / illustration · best cost-to-quality",
            "badges": ["T2I"],
            "verified": True,
            "price_per_image": 0.09,
            "price_table": {"resolution": {"1K": 0.045, "2K": 0.09}},
            "price_note": "Local estimate only, and priced by resolution: a live 1K render "
                          "on 2026-08-12 billed 0.045. The real charge comes back on the "
                          "submit response and is stored in manifest.json.",
            "dialect": {
                "strip_mj_flags": True,
                "supports_negative_prompt": False,
                "strip_quoted_dialogue": True,
                "notes": [
                    "Reads Midjourney flags (--ar/--v/--sref/--sw) as literal text; they are stripped.",
                    "No negative-prompt parameter: exclusions have to be phrased positively, in words.",
                    "Quoted dialogue tends to be rendered as on-image text.",
                    "Cannot count reliably -- state exact counts plainly and repeat them.",
                    "The safety filter reads comparative age words ('the younger cousin', "
                    "'young man', 'boy', 'kid') as references to minors and rejects the whole "
                    "prompt with HTTP 451, even when the character is stated elsewhere as an "
                    "adult. Name an explicit adult age instead ('a man of twenty-six').",
                    "Instructions buried at the end of a long style block are partially ignored, "
                    "so the scene body is sent first and the shared style block after it.",
                ],
            },
            "inputs": [
                {
                    "key": "aspect_ratio", "label": "Aspect ratio", "type": "enum",
                    "options": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                    "default": "16:9", "confirmed": ["16:9"],
                },
                {
                    "key": "resolution", "label": "Resolution", "type": "enum",
                    "options": ["1K", "2K"], "default": "2K", "confirmed": ["1K", "2K"],
                },
                {
                    "key": "seed", "label": "Seed (blank = random)", "type": "seed",
                    "default": None, "min": 0, "max": 2147483647,
                },
                {
                    "key": "output_format", "label": "Requested format", "type": "enum",
                    "options": ["png", "jpg"], "default": "png",
                    "help": "Renderful delivers JPEG regardless; the file is saved as delivered.",
                },
            ],
        },
        "custom": {
            "name": "Custom Renderful model",
            "provider": "Renderful",
            "strength": "Type any model id your account supports",
            "badges": ["T2I"],
            "verified": False,
            "price_per_image": 0.09,
            "price_note": "Unverified. Check the model id and pricing in your Renderful "
                          "dashboard before rendering a batch.",
            "dialect": {
                "strip_mj_flags": True,
                "supports_negative_prompt": False,
                "strip_quoted_dialogue": True,
                "notes": ["Unverified engine: render one scene first, then the batch."],
            },
            "inputs": [
                {"key": "model_id", "label": "Model id", "type": "text", "default": ""},
                {
                    "key": "aspect_ratio", "label": "Aspect ratio", "type": "enum",
                    "options": ["16:9", "9:16", "1:1", "4:3", "3:4"], "default": "16:9",
                },
                {
                    "key": "resolution", "label": "Resolution", "type": "enum",
                    "options": ["1K", "2K"], "default": "2K",
                },
                {"key": "seed", "label": "Seed (blank = random)", "type": "seed",
                 "default": None, "min": 0, "max": 2147483647},
                {"key": "output_format", "label": "Requested format", "type": "enum",
                 "options": ["png", "jpg"], "default": "png"},
            ],
        },
    },
}


SECTION_ENGINES = "engines"
SECTION_VOICES = "voices"

# TTS is billed by the character, not the request: a 70-character line billed
# 0.0035 live, which is exactly $0.05 per 1000 characters. Estimating per line
# would be wrong for both a two-word beat and a long paragraph.
CHARS_PER_PRICE_UNIT = 1000

# ElevenLabs identifies a voice by an opaque voice_id, never by its display name:
# sending "George" earns `A voice with voice_id 'George' was not found`. Renderful
# forwards the `voice` parameter through untouched, so the id is what we store.
#
# Read from https://api.elevenlabs.io/v1/voices (public, unauthenticated) on
# 2026-08-13 -- the premade library every account gets. It is generated data, not
# remembered data, which is the point: the first version of this list was written
# from the model's marketing page and three of its six names had already been
# retired from the library.
#
# The label is what the dropdown shows; the key is what goes on the wire.
VOICE_LIBRARY: dict[str, str] = {
    "pNInz6obpgDQGcFmaJgB": "Adam · american male · dominant, firm",
    "Xb7hH8MSUJpSbSDYk0k2": "Alice · british female · clear, engaging educator",
    "hpp4J3VqNfWAUOO0d1Us": "Bella · american female · professional, bright, warm",
    "pqHfZKP75CvOlQylNhV4": "Bill · american male · wise, mature, balanced",
    "nPczCjzI2devNBz1zQrb": "Brian · american male · deep, resonant and comforting",
    "N2lVS1w4EtoT3dr4eOWO": "Callum · american male · husky trickster",
    "IKne3meq5aSn9XLyUdCD": "Charlie · australian male · deep, confident, energetic",
    "iP95p4xoKVk53GoZ742B": "Chris · american male · charming, down-to-earth",
    "onwK4e9ZLuTAKqWW03F9": "Daniel · british male · steady broadcaster",
    "cjVigY5qzO86Huf0OWal": "Eric · american male · smooth, trustworthy",
    "JBFqnCBsd6RMkjVDRZzb": "George · british male · warm, captivating storyteller",
    "SOYHLrjzK2X1ezoPC6cr": "Harry · american male · fierce warrior",
    "cgSgspJ2msm6clMCkdW9": "Jessica · american female · playful, bright, warm",
    "FGY2WhTYpPnrIDTdsKH5": "Laura · american female · enthusiast, quirky attitude",
    "TX3LPaxmHKxFdv7VOQHJ": "Liam · american male · energetic, social media creator",
    "pFZP5JQG7iQjIQuC4Bku": "Lily · british female · velvety actress",
    "XrExE9yKIg1WjnnlVkGX": "Matilda · american female · knowledgable, professional",
    "SAz9YHcvj6GT2YYXdXww": "River · american · relaxed, neutral, informative",
    "CwhRBWXzGAHq8TQ4Fs17": "Roger · american male · laid-back, casual, resonant",
    "EXAVITQu4vr4xnSDxMaL": "Sarah · american female · mature, reassuring, confident",
    "bIHbv24MWmeRgasZH58o": "Will · american male · relaxed optimist",
}

# A storyteller for a storytelling app.
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"

# The display names this app shipped as voice options before the ids were known.
# Every one of them was rejected by the API, so no project has ever produced audio
# under any of them and no working choice is being overridden here. George, Adam
# and Bella map to themselves; Rachel, Josh and Arnold no longer exist in the
# premade library at all, and fall back to the default rather than leaving a
# stored project permanently unable to validate.
LEGACY_VOICE_NAMES: dict[str, str] = {
    "George": "JBFqnCBsd6RMkjVDRZzb",
    "Adam": "pNInz6obpgDQGcFmaJgB",
    "Bella": "hpp4J3VqNfWAUOO0d1Us",
    "Rachel": DEFAULT_VOICE_ID,
    "Josh": DEFAULT_VOICE_ID,
    "Arnold": DEFAULT_VOICE_ID,
}

# Bump when a shipped voice entry's own data is wrong and the copy already written
# to engines.json has to be repaired. v1 shipped display names as voice options.
VOICE_SCHEMA_VERSION = 2

# Labels read "Name · accent gender · character"; the name alone identifies the
# voice in an error message without printing 21 opaque ids.
LABEL_SEPARATOR = " · "

# Voice registry. Only eleven_flash_v2_5 has been put through a live account
# (generation tro5R9t8qky565RboWR6, 2026-08-13). The individual voice, speed and
# stability values below are declared from the model's published schema and have
# not each been proven -- the same rule as the image engines: prove one line
# before a batch.
DEFAULT_VOICES: dict[str, Any] = {
    "eleven_flash_v2_5": {
        "name": "Eleven Flash v2.5",
        "provider": "ElevenLabs (via Renderful)",
        "strength": "Fastest and cheapest · proven on this account",
        "badges": ["TTS"],
        "verified": True,
        "price_per_1k_chars": 0.05,
        "price_note": "Derived, not guessed: a live 70-character line on 2026-08-13 "
                      "billed 0.0035, i.e. $0.05 per 1000 characters. The real charge "
                      "comes back on the response and is stored in manifest.json.",
        "schema_version": VOICE_SCHEMA_VERSION,
        "notes": [
            "Proven: model id, prompt and voice id. Unproven: speed, stability and "
            "similarity_boost values -- synthesise one line before a batch.",
            "Leave the language code blank to follow the story's own language.",
            "Voices are the premade library every ElevenLabs account gets. To use a "
            "cloned voice, add its id and a label to the voice input in engines.json.",
        ],
        "inputs": [
            {
                "key": "voice", "label": "Voice", "type": "enum",
                "options": list(VOICE_LIBRARY),
                "labels": dict(VOICE_LIBRARY),
                "aliases": dict(LEGACY_VOICE_NAMES),
                "default": DEFAULT_VOICE_ID,
            },
            {
                "key": "language_code", "label": "Language code (blank = follow the story)",
                "type": "text", "default": "", "required": False,
            },
            {
                "key": "speed", "label": "Speed", "type": "range",
                "min": 0.25, "max": 4.0, "default": 1.0,
            },
            {
                "key": "stability", "label": "Stability", "type": "range",
                "min": 0.0, "max": 1.0, "default": 0.5,
            },
            {
                "key": "similarity_boost", "label": "Similarity boost", "type": "range",
                "min": 0.0, "max": 1.0, "default": 0.75,
            },
        ],
    },
    "speech-2.6-turbo": {
        "name": "MiniMax Speech 2.6 Turbo",
        "provider": "MiniMax (via Renderful)",
        "strength": "40 languages · for stories not written in English",
        "badges": ["TTS", "multilingual"],
        "verified": False,
        "price_per_1k_chars": 0.06,
        "price_note": "Unverified. Priced from the listing's floor ($0.06). Synthesise "
                      "one line and read the real charge off the response before a batch.",
        "schema_version": VOICE_SCHEMA_VERSION,
        "notes": [
            "Unverified engine: synthesise one line first, then the batch.",
            "Parameter names are assumed to match the ElevenLabs family and may differ.",
            "No voice picker: MiniMax uses its own voice ids, not ElevenLabs', and "
            "guessing them is the mistake that made every ElevenLabs voice fail. "
            "The account default speaks until one is confirmed live.",
        ],
        "inputs": [
            {
                "key": "language_code", "label": "Language code (blank = follow the story)",
                "type": "text", "default": "", "required": False,
            },
            {
                "key": "speed", "label": "Speed", "type": "range",
                "min": 0.5, "max": 2.0, "default": 1.0,
            },
        ],
    },
}

DEFAULT_REGISTRY["default_voice"] = config.DEFAULT_VOICE
DEFAULT_REGISTRY[SECTION_VOICES] = DEFAULT_VOICES


class ParamError(ValueError):
    """A parameter that the engine schema rejects. Never reaches the API."""


def _option_names(spec: dict) -> list[str]:
    """Enum options as a human would name them, for error messages.

    A voice id carries no meaning on its own, so an error listing 21 of them
    tells the reader nothing. Options with no label print as themselves, which
    is what every image engine wants.
    """
    labels = spec.get("labels") or {}
    return [str(labels.get(o, o)).split(LABEL_SEPARATOR)[0] for o in spec["options"]]


def _migrate(reg: dict) -> tuple[dict, bool]:
    """Add registry sections a hand-written engines.json predates.

    The file is the user's to edit, so missing sections are filled in rather than
    overwritten -- an engines.json written before voices existed keeps every
    image engine exactly as it was.
    """
    changed = False
    if SECTION_VOICES not in reg:
        reg[SECTION_VOICES] = json.loads(json.dumps(DEFAULT_VOICES))
        changed = True
    if "default_voice" not in reg:
        reg["default_voice"] = config.DEFAULT_VOICE
        changed = True

    # Repair a shipped voice whose own data was wrong. Filling in a missing
    # section is not enough here: the broken voice list had already been written
    # to disk, so leaving the file alone would leave it broken forever. Only
    # voices this app ships are touched, and only when the version they were
    # written at is behind the current one -- a voice the user added themselves
    # has no shipped counterpart and is never rewritten.
    for key, shipped in DEFAULT_VOICES.items():
        have = reg[SECTION_VOICES].get(key)
        if have is None or have.get("schema_version", 1) < shipped["schema_version"]:
            reg[SECTION_VOICES][key] = json.loads(json.dumps(shipped))
            changed = True
    return reg, changed


def registry(reload: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is not None and not reload:
            return _cache
        path = config.ENGINES_FILE
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_REGISTRY, indent=2), encoding="utf-8")
            _cache = json.loads(json.dumps(DEFAULT_REGISTRY))
        else:
            loaded, changed = _migrate(json.loads(path.read_text(encoding="utf-8")))
            if changed:
                path.write_text(json.dumps(loaded, indent=2, ensure_ascii=False),
                                encoding="utf-8")
            _cache = loaded
        return _cache


def _entry(section: str, key: str) -> dict:
    found = registry().get(section, {}).get(key)
    if found is None:
        noun = "voice" if section == SECTION_VOICES else "engine"
        raise ParamError(f"unknown {noun} {key!r}")
    return found


def engine(key: str) -> dict:
    return _entry(SECTION_ENGINES, key)


def voice(key: str) -> dict:
    return _entry(SECTION_VOICES, key)


def default_voice_key() -> str:
    return registry().get("default_voice", config.DEFAULT_VOICE)


def default_engine_key() -> str:
    return registry().get("default", config.DEFAULT_ENGINE)


def defaults_for(key: str, section: str = SECTION_ENGINES) -> dict:
    return {i["key"]: i.get("default") for i in _entry(section, key)["inputs"]}


def validate(key: str, params: dict | None, section: str = SECTION_ENGINES) -> dict:
    """Return a normalised copy of `params`, or raise ParamError.

    Runs before every submission -- a rejected request still costs money.
    """
    eng = _entry(section, key)
    schema = {i["key"]: i for i in eng["inputs"]}
    params = dict(params or {})

    unknown = sorted(set(params) - set(schema))
    if unknown:
        raise ParamError(f"{eng['name']}: unknown parameter(s) {', '.join(unknown)}")

    out: dict[str, Any] = {}
    for k, spec in schema.items():
        value = params.get(k, spec.get("default"))
        kind = spec["type"]

        if kind == "enum":
            # A value this app used to ship still sits in saved projects. Resolve
            # it here, at the one place every stored parameter passes through,
            # so an old project loads instead of being stuck: the settings
            # endpoint revalidates what it read back, and would reject its own
            # stored value with no way for the user to correct it.
            value = (spec.get("aliases") or {}).get(value, value)
            if value not in spec["options"]:
                raise ParamError(
                    f"{spec['label']}: {value!r} is not one of "
                    f"{', '.join(_option_names(spec))}"
                )
            out[k] = value

        elif kind == "seed":
            if value in (None, "", "random"):
                out[k] = None
            else:
                try:
                    seed = int(value)
                except (TypeError, ValueError):
                    raise ParamError(f"{spec['label']}: must be a whole number") from None
                if not (spec.get("min", 0) <= seed <= spec.get("max", 2 ** 31 - 1)):
                    raise ParamError(
                        f"{spec['label']}: must be between {spec.get('min', 0)} "
                        f"and {spec.get('max', 2 ** 31 - 1)}"
                    )
                out[k] = seed

        elif kind == "range":
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ParamError(f"{spec['label']}: must be a number") from None
            if not (spec["min"] <= num <= spec["max"]):
                raise ParamError(
                    f"{spec['label']}: must be between {spec['min']} and {spec['max']}"
                )
            out[k] = num

        elif kind == "toggle":
            out[k] = bool(value)

        elif kind == "text":
            text = ("" if value is None else str(value)).strip()
            if spec.get("required", k == "model_id") and not text:
                raise ParamError(f"{spec['label']}: required")
            out[k] = text

        else:
            raise ParamError(f"{spec['label']}: unsupported control type {kind!r}")

    return out


def model_id(key: str, params: dict) -> str:
    """The string Renderful expects in the `model` field."""
    if key == "custom":
        mid = (params.get("model_id") or "").strip()
        if not mid:
            raise ParamError("Model id: required for the custom engine")
        return mid
    return key


def unconfirmed_values(key: str, params: dict,
                       section: str = SECTION_ENGINES) -> list[str]:
    """Params outside the values we have actually seen a live account accept."""
    warnings = []
    for spec in _entry(section, key)["inputs"]:
        confirmed = spec.get("confirmed")
        if not confirmed:
            continue
        value = params.get(spec["key"])
        if value is not None and value not in confirmed:
            warnings.append(
                f"{spec['label']} = {value} has not been confirmed against a live account "
                f"(confirmed: {', '.join(map(str, confirmed))})"
            )
    return warnings


def price_per_image(key: str, params: dict | None = None) -> float:
    """Resolution changes what Renderful charges, so the estimate follows it.

    Anything not in the table falls back to the engine's headline price, which is
    the dearest one -- an estimate that is too low is worse than one too high.
    """
    eng = engine(key)
    base = float(eng.get("price_per_image") or 0.0)
    for param, prices in (eng.get("price_table") or {}).items():
        value = (params or {}).get(param)
        if value in prices:
            return float(prices[value])
    return base


def price_per_1k_chars(voice_key: str) -> float:
    return float(voice(voice_key).get("price_per_1k_chars") or 0.0)


def price_for_text(voice_key: str, text: str) -> float:
    """Estimated charge for synthesising one line.

    Billed by the character, so a two-word beat and a long paragraph cannot cost
    the same. The actual charge still comes back on the response.
    """
    chars = len(text or "")
    return price_per_1k_chars(voice_key) * chars / CHARS_PER_PRICE_UNIT


def public_voices() -> dict:
    """Voice registry as sent to the browser."""
    reg = registry()
    return {
        "default": reg.get("default_voice", config.DEFAULT_VOICE),
        "voices": {
            k: {
                "key": k,
                "name": v.get("name", k),
                "provider": v.get("provider", ""),
                "strength": v.get("strength", ""),
                "badges": v.get("badges", []),
                "verified": bool(v.get("verified")),
                "price_per_1k_chars": v.get("price_per_1k_chars", 0.0),
                "price_note": v.get("price_note", ""),
                "notes": v.get("notes", []),
                "inputs": v.get("inputs", []),
            }
            for k, v in reg.get(SECTION_VOICES, {}).items()
        },
    }


def public_registry() -> dict:
    """Registry as sent to the browser (no secrets involved, but keep it explicit)."""
    reg = registry()
    return {
        "default": reg.get("default", config.DEFAULT_ENGINE),
        "engines": {
            k: {
                "key": k,
                "name": v.get("name", k),
                "provider": v.get("provider", ""),
                "strength": v.get("strength", ""),
                "badges": v.get("badges", []),
                "verified": bool(v.get("verified")),
                "price_per_image": v.get("price_per_image", 0.0),
                "price_note": v.get("price_note", ""),
                "dialect_notes": v.get("dialect", {}).get("notes", []),
                "inputs": v.get("inputs", []),
            }
            for k, v in reg["engines"].items()
        },
    }
