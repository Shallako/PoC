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


class ParamError(ValueError):
    """A parameter that the engine schema rejects. Never reaches the API."""


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
            _cache = json.loads(path.read_text(encoding="utf-8"))
        return _cache


def engine(key: str) -> dict:
    reg = registry()
    eng = reg["engines"].get(key)
    if eng is None:
        raise ParamError(f"unknown engine {key!r}")
    return eng


def default_engine_key() -> str:
    return registry().get("default", config.DEFAULT_ENGINE)


def defaults_for(key: str) -> dict:
    return {i["key"]: i.get("default") for i in engine(key)["inputs"]}


def validate(key: str, params: dict | None) -> dict:
    """Return a normalised copy of `params`, or raise ParamError.

    Runs before every submission -- a rejected request still costs money.
    """
    eng = engine(key)
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
            if value not in spec["options"]:
                raise ParamError(
                    f"{spec['label']}: {value!r} is not one of {', '.join(spec['options'])}"
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


def unconfirmed_values(key: str, params: dict) -> list[str]:
    """Params outside the values we have actually seen a live account accept."""
    warnings = []
    for spec in engine(key)["inputs"]:
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
