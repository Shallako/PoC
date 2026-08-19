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

from . import config, security

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
                "supports_negative_prompt": False,
                "strip_quoted_dialogue": True,
                "notes": [
                    "Reads command-line-style flags (--ar/--v/--stylize) as literal text; "
                    "keep them out of the prompt.",
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
            # Reference-conditioned sibling, for character consistency. Same
            # price band as the text-to-image entry above, pair for pair
            # (GET /api/v1/models, 2026-08-15) -- references are the same model
            # taking one more input, not a premium tier.
            "ref": {"model": "seedream-5.0-pro-i2i", "max_refs": 10,
                    "verified": False},
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
        # --------------------------------------------------------------- #
        # Engines that also generate video.
        #
        # Renderful has no dual-mode model: `type` is one-to-one, so
        # nano-banana-pro is text-to-image and google-veo-3.1 is text-to-video,
        # and an engine that does both is a pair of ids from the same house.
        # The sibling lives under "clip" on the image entry, with its own
        # schema, so choosing the engine chooses both halves.
        #
        # Every id, aspect ratio, resolution, duration and price band below was
        # read from GET /api/v1/models on this account on 2026-08-13. None of it
        # is remembered, because the last time model ids were written from
        # memory every one of them was wrong (see VOICE_LIBRARY).
        # --------------------------------------------------------------- #
        "nano-banana-pro": {
            "name": "Nano Banana Pro",
            "provider": "Google (via Renderful)",
            "strength": "Strongest prompt-following · up to 4K · clips via Veo 3.1",
            "badges": ["T2I", "T2V", "4K"],
            "verified": False,
            "price_per_image": 0.135,
            "price_table": {"resolution": {"1k": 0.135, "2k": 0.27, "4k": 0.54}},
            "price_note": "Unverified on this account. The catalog publishes a $0.135-$0.54 "
                          "band; the table reads the floor as the 1k price and doubles per "
                          "step, which is how Seedream's 1K/2K prices behave. Only the floor "
                          "is a published figure -- the two above it are interpolated. The "
                          "real charge comes back on the response and lands in manifest.json.",
            "dialect": {
                "supports_negative_prompt": False,
                "strip_quoted_dialogue": True,
                "notes": [
                    "Unverified engine: render one scene first, then the batch.",
                    "Resolutions are lower-case here (1k/2k/4k), unlike Seedream's 1K/2K. "
                    "The strings are sent as written, so the case matters.",
                    "No negative-prompt parameter: exclusions have to be phrased positively, in words.",
                    "Quoted dialogue tends to be rendered as on-image text.",
                    "Seed and requested format are sent on every request because the payload "
                    "is shared with Seedream; neither is published for this model.",
                ],
            },
            "inputs": [
                {
                    "key": "aspect_ratio", "label": "Aspect ratio", "type": "enum",
                    "options": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
                    "default": "16:9",
                },
                {
                    "key": "resolution", "label": "Resolution", "type": "enum",
                    "options": ["1k", "2k", "4k"], "default": "2k",
                },
                {"key": "seed", "label": "Seed (blank = random)", "type": "seed",
                 "default": None, "min": 0, "max": 2147483647},
                {"key": "output_format", "label": "Requested format", "type": "enum",
                 "options": ["png", "jpg"], "default": "png",
                 "help": "Renderful saves whatever bytes it delivers, whichever you ask for."},
            ],
            # Reference-conditioned sibling (GET /api/v1/models, 2026-08-15).
            "ref": {"model": "nano-banana-2-i2i", "max_refs": 10, "verified": False},
            "clip": {
                "model": "google-veo-3.1",
                "name": "Google Veo 3.1",
                "provider": "Google (via Renderful)",
                "price_per_clip": 5.64,
                "price_table": {"resolution": {"720p": 2.82, "1080p": 5.64}},
                "price_note": "Unverified. The catalog publishes $2.82-$5.64 per clip against "
                              "exactly two resolutions, so the band is read as one price each "
                              "-- less of a guess than the image tables. Anything off the "
                              "table falls back to the ceiling: one clip is 20-40x one image, "
                              "so an estimate that is too low would be a far worse mistake "
                              "than one that is too high.",
                "notes": [
                    "Speaks for itself: Veo 3.1 generates its own dialogue and sound, so a "
                    "clip needs no separate narration audio.",
                    "The audio controls below are declared from the model's behaviour, not "
                    "from a published schema -- the catalog lists only aspect ratio, "
                    "resolution and duration. Generate one clip before a batch.",
                    "Longest clip is 8 seconds. A scene whose narration runs longer than "
                    "its clip has to be split or held on a still.",
                ],
                "inputs": [
                    {
                        "key": "aspect_ratio", "label": "Aspect ratio", "type": "enum",
                        "options": ["16:9", "9:16"], "default": "16:9",
                    },
                    {
                        "key": "resolution", "label": "Resolution", "type": "enum",
                        "options": ["720p", "1080p"], "default": "720p",
                    },
                    {
                        "key": "duration", "label": "Clip length", "type": "enum",
                        "options": ["4", "6", "8"],
                        "labels": {"4": "4 seconds", "6": "6 seconds", "8": "8 seconds"},
                        "default": "8",
                    },
                    {
                        "key": "audio", "label": "Generate speech and sound", "type": "toggle",
                        "default": True,
                        "help": "Unconfirmed parameter name -- prove one clip before a batch.",
                    },
                    {
                        "key": "spoken_language",
                        "label": "Spoken language (blank = follow the story)",
                        "type": "text", "default": "", "required": False,
                    },
                ],
            },
        },
        "gpt-image-2": {
            "name": "GPT Image 2",
            "provider": "OpenAI (via Renderful)",
            "strength": "Cheapest of the three · reliable lettering · clips via Sora 2",
            "badges": ["T2I", "T2V", "4K"],
            "verified": False,
            "price_per_image": 0.03,
            "price_table": {"resolution": {"1K": 0.03, "2K": 0.06, "4K": 0.12}},
            "price_note": "Unverified on this account. The catalog publishes a $0.03-$0.12 "
                          "band, which undercuts Seedream at every resolution. Only the floor "
                          "is published; 2K and 4K are interpolated the same way as Nano "
                          "Banana Pro. The real charge comes back on the response.",
            "dialect": {
                "supports_negative_prompt": False,
                "strip_quoted_dialogue": True,
                "notes": [
                    "Unverified engine: render one scene first, then the batch.",
                    "Resolutions are upper-case here (1K/2K/4K) and 'auto' is a valid aspect "
                    "ratio, which lets the model choose the frame -- fine for one image, bad "
                    "for a set that has to cut together.",
                    "Renders legible text better than the others, which makes stray quoted "
                    "dialogue *more* likely to appear in the picture, not less. The guardrail "
                    "still strips it.",
                    "No negative-prompt parameter: exclusions have to be phrased positively, in words.",
                ],
            },
            "inputs": [
                {
                    "key": "aspect_ratio", "label": "Aspect ratio", "type": "enum",
                    "options": ["auto", "1:1", "9:16", "16:9", "4:3", "3:4"],
                    "default": "16:9",
                    "help": "'auto' lets the model pick per image, so a set can come back mixed.",
                },
                {
                    "key": "resolution", "label": "Resolution", "type": "enum",
                    "options": ["1K", "2K", "4K"], "default": "2K",
                },
                {"key": "seed", "label": "Seed (blank = random)", "type": "seed",
                 "default": None, "min": 0, "max": 2147483647},
                {"key": "output_format", "label": "Requested format", "type": "enum",
                 "options": ["png", "jpg"], "default": "png"},
            ],
            # Reference-conditioned sibling (GET /api/v1/models, 2026-08-15).
            "ref": {"model": "gpt-image-2-i2i", "max_refs": 10, "verified": False},
            "clip": {
                "model": "sora-2",
                "name": "Sora 2",
                "provider": "OpenAI (via Renderful)",
                "price_per_clip": 0.88,
                "price_note": "Unverified. The catalog publishes $0.44-$0.88 per clip -- the "
                              "cheapest video in the catalog by a wide margin, and about a "
                              "sixth of Veo 3.1. The estimate takes the ceiling.",
                "notes": [
                    "Speaks for itself: Sora 2 generates its own dialogue and sound, so a "
                    "clip needs no separate narration audio.",
                    "The audio controls below are declared from the model's behaviour, not "
                    "from a published schema -- the catalog lists only aspect ratio, "
                    "resolution and duration. Generate one clip before a batch.",
                    "Clips run to 20 seconds, long enough for most single narration lines, "
                    "and only 720p is offered.",
                ],
                "inputs": [
                    {
                        "key": "aspect_ratio", "label": "Aspect ratio", "type": "enum",
                        "options": ["16:9", "9:16"], "default": "16:9",
                    },
                    {
                        "key": "resolution", "label": "Resolution", "type": "enum",
                        "options": ["720p"], "default": "720p",
                    },
                    {
                        "key": "duration", "label": "Clip length", "type": "enum",
                        "options": ["4", "8", "12", "16", "20"],
                        "labels": {"4": "4 seconds", "8": "8 seconds", "12": "12 seconds",
                                   "16": "16 seconds", "20": "20 seconds"},
                        "default": "8",
                    },
                    {
                        "key": "audio", "label": "Generate speech and sound", "type": "toggle",
                        "default": True,
                        "help": "Unconfirmed parameter name -- prove one clip before a batch.",
                    },
                    {
                        "key": "spoken_language",
                        "label": "Spoken language (blank = follow the story)",
                        "type": "text", "default": "", "required": False,
                    },
                ],
            },
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

# Blocks that may be added to a shipped engine entry that predates them. Only
# ever filled in when missing -- see _migrate().
ADDITIVE_ENGINE_KEYS = ("ref",)

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

# Bump when a shipped entry's own data is wrong and the copy already written to
# engines.json has to be repaired. Voices v1 shipped display names as options.
VOICE_SCHEMA_VERSION = 2
VIDEO_SCHEMA_VERSION = 1

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

SECTION_VIDEO = "video"

# Assembly happens on this machine with ffmpeg, so nothing here is billed. It
# still lives in the registry: these are typed inputs with defaults, which is
# exactly what the schema machinery already draws controls for and validates,
# and putting them here means the defaults are yours to change in engines.json.
DEFAULT_VIDEO_PROFILES: dict[str, Any] = {
    "ffmpeg": {
        "name": "ffmpeg (this machine)",
        "provider": "local",
        "strength": "No API cost, no upload, no watermark · needs ffmpeg installed",
        "badges": ["local", "free"],
        "verified": True,
        "schema_version": VIDEO_SCHEMA_VERSION,
        "notes": [
            "Assembly is local: nothing is uploaded and nothing is billed.",
            "Scene lengths come from measured narration audio. Speak the script "
            "first and the cut is exact; assemble before that and it follows the "
            "word-count estimate, which runs about 30% long.",
        ],
        "inputs": [
            {
                "key": "aspect", "label": "Frame", "type": "enum",
                "options": [config.VIDEO_ASPECT_SOURCE, *config.VIDEO_CANVASES],
                "labels": {
                    config.VIDEO_ASPECT_SOURCE: "Follow the rendered images",
                    **{k: f"{k} · {w}x{h}" for k, (w, h) in config.VIDEO_CANVASES.items()},
                },
                "default": config.VIDEO_ASPECT_SOURCE,
            },
            {
                "key": "motion", "label": "Motion", "type": "enum",
                "options": ["ken-burns", "none"],
                "labels": {"ken-burns": "Ken Burns · slow zoom, alternating",
                           "none": "Still frames"},
                "default": "ken-burns",
            },
            {
                "key": "subtitles", "label": "Captions in the video", "type": "enum",
                "options": ["soft", "none", "burn"],
                "labels": {"soft": "Track the player can switch off",
                           "none": "None · the .srt is still exported",
                           "burn": "Burned into the picture · re-encodes"},
                "default": "soft",
                "help": "A .srt and .vtt are always written to export/ regardless.",
            },
            {
                "key": "fps", "label": "Frames per second", "type": "integer",
                "min": 12, "max": 60, "default": config.VIDEO_FPS,
            },
            {
                "key": "lead_in", "label": "Silence before each line (s)",
                "type": "range", "min": 0.0, "max": 3.0, "step": 0.05,
                "default": config.VIDEO_LEAD_IN_SECONDS,
            },
            {
                "key": "tail", "label": "Silence after each line (s)",
                "type": "range", "min": 0.0, "max": 5.0, "step": 0.05,
                "default": config.VIDEO_TAIL_SECONDS,
            },
        ],
    },
}

DEFAULT_REGISTRY["default_voice"] = config.DEFAULT_VOICE
DEFAULT_REGISTRY[SECTION_VOICES] = DEFAULT_VOICES
DEFAULT_REGISTRY["default_video"] = config.DEFAULT_VIDEO_PROFILE
DEFAULT_REGISTRY[SECTION_VIDEO] = DEFAULT_VIDEO_PROFILES


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

    # An engines.json written before an engine shipped would never see it: the
    # file exists, so the defaults are not consulted again. Missing entries are
    # added; existing ones are left exactly as the user has them, including
    # seedream's, so this can only ever grow the list.
    for key, shipped in DEFAULT_REGISTRY[SECTION_ENGINES].items():
        if key not in reg.get(SECTION_ENGINES, {}):
            reg.setdefault(SECTION_ENGINES, {})[key] = json.loads(json.dumps(shipped))
            changed = True
            continue
        # A capability added to a shipped engine after the file was written is
        # filled in, but only where the key is absent entirely. Rewriting the
        # entry the way stale voices are rewritten would throw away a re-priced
        # or renamed engine, and the promise made in the README is that editing
        # one is safe. An absent key was never edited, so adding it cannot
        # destroy anything.
        for capability in ADDITIVE_ENGINE_KEYS:
            if capability in shipped and capability not in reg[SECTION_ENGINES][key]:
                reg[SECTION_ENGINES][key][capability] = json.loads(
                    json.dumps(shipped[capability]))
                changed = True

    sections = (
        (SECTION_VOICES, DEFAULT_VOICES, "default_voice", config.DEFAULT_VOICE),
        (SECTION_VIDEO, DEFAULT_VIDEO_PROFILES, "default_video",
         config.DEFAULT_VIDEO_PROFILE),
    )
    for section, shipped_all, default_key, default_value in sections:
        if section not in reg:
            reg[section] = json.loads(json.dumps(shipped_all))
            changed = True
        if default_key not in reg:
            reg[default_key] = default_value
            changed = True

        # Repair a shipped entry whose own data was wrong. Filling in a missing
        # section is not enough: the broken voice list had already been written
        # to disk, so leaving the file alone would have left it broken forever.
        # Only entries this app ships are touched, and only when the version they
        # were written at is behind -- an entry the user added themselves has no
        # shipped counterpart and is never rewritten.
        for key, shipped in shipped_all.items():
            have = reg[section].get(key)
            if have is None or have.get("schema_version", 1) < shipped["schema_version"]:
                reg[section][key] = json.loads(json.dumps(shipped))
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
            # ensure_ascii=False on both writes, or the two disagree: a fresh
            # file gets \u2014 where a migrated one gets the character itself,
            # so the first migration rewrites every line that has punctuation in
            # it and the diff says nothing about what actually changed.
            path.write_text(json.dumps(DEFAULT_REGISTRY, indent=2, ensure_ascii=False),
                            encoding="utf-8")
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
        noun = {SECTION_VOICES: "voice", SECTION_VIDEO: "video profile"}.get(
            section, "engine")
        raise ParamError(f"unknown {noun} {key!r}")
    return found


def engine(key: str) -> dict:
    return _entry(SECTION_ENGINES, key)


def voice(key: str) -> dict:
    return _entry(SECTION_VOICES, key)


def default_voice_key() -> str:
    return registry().get("default_voice", config.DEFAULT_VOICE)


def video_profile(key: str) -> dict:
    return _entry(SECTION_VIDEO, key)


def default_video_key() -> str:
    return registry().get("default_video", config.DEFAULT_VIDEO_PROFILE)


def default_engine_key() -> str:
    return registry().get("default", config.DEFAULT_ENGINE)


def defaults_for(key: str, section: str = SECTION_ENGINES) -> dict:
    return {i["key"]: i.get("default") for i in _entry(section, key)["inputs"]}


def validate(key: str, params: dict | None, section: str = SECTION_ENGINES) -> dict:
    """Return a normalised copy of `params`, or raise ParamError.

    Runs before every submission -- a rejected request still costs money.
    """
    eng = _entry(section, key)
    return _validate_inputs(eng["name"], eng["inputs"], params)


def _validate_inputs(name: str, inputs: list, params: dict | None) -> dict:
    """The schema walk itself, against any declared `inputs` list.

    Split out from validate() so an engine's video sibling is checked by exactly
    the same rules as the engine: one implementation, one set of error messages.
    """
    schema = {i["key"]: i for i in inputs}
    params = dict(params or {})

    unknown = sorted(set(params) - set(schema))
    if unknown:
        raise ParamError(f"{name}: unknown parameter(s) {', '.join(unknown)}")

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

        elif kind == "integer":
            # Distinct from "range" because the consumer is a command line, not
            # arithmetic: a frame rate of 30.0 is not a frame rate ffmpeg takes.
            try:
                num = int(value)
            except (TypeError, ValueError):
                raise ParamError(f"{spec['label']}: must be a whole number") from None
            if not (spec["min"] <= num <= spec["max"]):
                raise ParamError(
                    f"{spec['label']}: must be between {spec['min']} and {spec['max']}"
                )
            out[k] = num

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
            # Bounded and de-controlled: a text parameter is the one control
            # whose value goes into an API payload verbatim, and `model_id` in
            # particular becomes the model this app is billed for.
            limit = int(spec.get("max_chars") or security.LIMIT_PARAM_TEXT)
            text = security.clean(value, limit + 1)
            if spec.get("required", k == "model_id") and not text:
                raise ParamError(f"{spec['label']}: required")
            if len(text) > limit:
                raise ParamError(f"{spec['label']}: at most {limit} characters")
            out[k] = text

        else:
            raise ParamError(f"{spec['label']}: unsupported control type {kind!r}")

    return out


# --------------------------------------------------------------------------- #
# The reference-conditioned sibling of an image engine
#
# Same house, same price band, one extra input. Used to hold a character's face
# steady across a set, which a shared style block cannot do: prose fixes the
# look, only the picture fixes the identity.
# --------------------------------------------------------------------------- #

def ref_spec(key: str) -> dict | None:
    """The paired image-to-image model, or None for an engine without one."""
    return engine(key).get("ref") or None


def supports_references(key: str) -> bool:
    return ref_spec(key) is not None


def ref_model(key: str) -> str:
    """The model id to send when a render carries reference images."""
    spec = ref_spec(key)
    if spec is None:
        raise ParamError(f"{engine(key)['name']} does not accept reference images")
    model = (spec.get("model") or "").strip()
    if not model:
        raise ParamError(f"{engine(key)['name']}: reference model id is missing")
    return model


def max_references(key: str) -> int:
    """How many references this engine takes, capped by the app-wide limit."""
    spec = ref_spec(key) or {}
    declared = int(spec.get("max_refs") or config.MAX_REFERENCES_PER_SCENE)
    return max(1, min(declared, config.MAX_REFERENCES_PER_SCENE))


# --------------------------------------------------------------------------- #
# The video sibling of an image engine
# --------------------------------------------------------------------------- #

def clip_spec(key: str) -> dict | None:
    """The paired text-to-video model, or None for an image-only engine."""
    return engine(key).get("clip") or None


def makes_video(key: str) -> bool:
    return clip_spec(key) is not None


def clip_defaults(key: str) -> dict:
    spec = clip_spec(key)
    return {i["key"]: i.get("default") for i in spec["inputs"]} if spec else {}


def validate_clip(key: str, params: dict | None) -> dict:
    spec = clip_spec(key)
    if spec is None:
        raise ParamError(f"{engine(key)['name']} does not generate video")
    return _validate_inputs(spec["name"], spec["inputs"], params)


def price_per_clip(key: str, params: dict | None = None) -> float:
    """What one generated clip is estimated to cost.

    Unlike price_per_image this takes the published ceiling rather than the
    floor: a clip runs 20-40x an image, so guessing low here would understate a
    batch by more than the whole image budget.
    """
    spec = clip_spec(key)
    if spec is None:
        return 0.0
    base = float(spec.get("price_per_clip") or 0.0)
    for param, prices in (spec.get("price_table") or {}).items():
        value = (params or {}).get(param)
        if value in prices:
            return float(prices[value])
    return base


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


def public_video() -> dict:
    """Video profiles as sent to the browser, plus whether ffmpeg is actually here.

    The UI has to be able to say "install this" instead of offering a button that
    fails, so availability travels with the profiles rather than being guessed.
    """
    from . import video  # local: keeps a subprocess module out of import time

    reg = registry()
    return {
        "default": reg.get("default_video", config.DEFAULT_VIDEO_PROFILE),
        "ffmpeg": {
            "available": video.available(),
            "version": video.version(),
            "hint": video.install_hint(),
        },
        "profiles": {
            k: {
                "key": k,
                "name": v.get("name", k),
                "provider": v.get("provider", ""),
                "strength": v.get("strength", ""),
                "badges": v.get("badges", []),
                "notes": v.get("notes", []),
                "inputs": v.get("inputs", []),
            }
            for k, v in reg.get(SECTION_VIDEO, {}).items()
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
                "clip": _public_clip(v.get("clip")),
            }
            for k, v in reg["engines"].items()
        },
    }


def _public_clip(spec: dict | None) -> dict | None:
    if not spec:
        return None
    return {
        "model": spec.get("model", ""),
        "name": spec.get("name", spec.get("model", "")),
        "provider": spec.get("provider", ""),
        "price_per_clip": spec.get("price_per_clip", 0.0),
        "price_note": spec.get("price_note", ""),
        "notes": spec.get("notes", []),
        "inputs": spec.get("inputs", []),
    }
