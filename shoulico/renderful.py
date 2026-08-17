"""Renderful client.

Lifted, behaviour-for-behaviour, from the working generate_boston.py: the
retry/poll/abort semantics in here were paid for with real renders.

  * POLL_TIMEOUT is 3600s per image -- 600 once stranded a paid render that
    finished later.
  * 401/402/403/429 and "limit reached" are fatal for the whole run (the account
    is out of credit or throttled); the caller sets a stop event.
  * Other 4xx (content rejection 451, malformed request) fail that one prompt
    immediately -- retrying them burns time and never succeeds.
  * The delivered bytes are saved as delivered. Renderful returns JPEG whatever
    output_format you ask for, and re-encoding to PNG cannot undo compression
    that already happened -- it just costs ~6x the bytes (FR-905).
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import security
from .config import RENDERFUL_API_BASE

POLL_SECONDS = 5
POLL_TIMEOUT = 3600
HTTP_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2               # multiplied by the attempt number

# Renderful routes on `type`; everything else in the payload is model-specific.
GEN_TYPE_IMAGE = "text-to-image"
GEN_TYPE_AUDIO = "text-to-audio"
# Same picture, plus reference images. A separate model id from the text-to-image
# one -- `type` is one-to-one on this API, so seedream-5.0-pro and
# seedream-5.0-pro-i2i are two entries, not one model in two modes.
GEN_TYPE_IMAGE_REF = "image-to-image"

# The schema (GET /api/v1/openapi.json?model=seedream-5.0-pro-i2i, read
# 2026-08-15) types references as URIs, not base64: `image_url` is a string and
# `images` an array of them. Nothing has to be uploaded, because every image
# Renderful generates is already served from its own CDN and the manifest keeps
# that URL -- so the reference for scene 7 is the URL the anchor came back on.
REFERENCE_FIELD = "images"

# Image defaults, applied only when the engine schema left one out.
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "2K"
DEFAULT_IMAGE_FORMAT = "png"
IMAGE_OUTPUTS_PER_REQUEST = 1

# Registry keys that drive the UI or model routing and must never be sent as
# generation parameters.
NON_API_PARAMS = frozenset({"model_id"})


class FatalAPIError(RuntimeError):
    """Out of credit / rate limited / bad key -- retrying will not help."""


def _request(url: str, key: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data else "GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_call(url: str, key: str, payload: dict | None = None) -> dict:
    last: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            return _request(url, key, payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
                msg = parsed.get("message") or parsed.get("error") or body
            except Exception:
                msg = body
            if e.code in (401, 402, 403, 429) or "limit reached" in str(msg).lower():
                raise FatalAPIError(f"HTTP {e.code}: {msg}") from None
            if 400 <= e.code < 500 and e.code != 408:
                raise RuntimeError(f"HTTP {e.code}: {msg}") from None
            last = RuntimeError(f"HTTP {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        if attempt < HTTP_RETRIES:
            time.sleep(2 * attempt)
    raise RuntimeError(f"request to {url} failed after {HTTP_RETRIES} attempts: {last}")


def submit(prompt: str, key: str, model: str, params: dict, *,
           gen_type: str = GEN_TYPE_IMAGE, references: list[str] | None = None) -> dict:
    """Params must already have passed engines.validate().

    Image payloads keep the exact shape the Boston set was rendered with. Other
    generation types send whatever their own schema declared and nothing else --
    an image's aspect ratio means nothing to a speech model, and Renderful bills
    a request it later rejects.
    """
    payload = {"type": gen_type, "model": model, "prompt": prompt}

    if gen_type in (GEN_TYPE_IMAGE, GEN_TYPE_IMAGE_REF):
        payload.update({
            "aspect_ratio": params.get("aspect_ratio", DEFAULT_ASPECT_RATIO),
            "resolution": params.get("resolution", DEFAULT_RESOLUTION),
            "num_outputs": IMAGE_OUTPUTS_PER_REQUEST,
            "output_format": params.get("output_format", DEFAULT_IMAGE_FORMAT),
        })
        if params.get("seed") is not None:
            payload["seed"] = params["seed"]
        # An image-to-image request with no reference is a text-to-image request
        # wearing the wrong model id, and the model id is the half that bills.
        if gen_type == GEN_TYPE_IMAGE_REF:
            if not references:
                raise ValueError("image-to-image needs at least one reference image")
            payload[REFERENCE_FIELD] = list(references)
    else:
        payload.update({
            k: v for k, v in params.items()
            if k not in NON_API_PARAMS and v is not None and v != ""
        })

    return api_call(f"{RENDERFUL_API_BASE}/generations", key, payload)


def wait_for(gen_id: str, key: str, should_stop=None, on_state=None, *,
             timeout: int = POLL_TIMEOUT, interval: int = POLL_SECONDS) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_stop is not None and should_stop():
            raise RuntimeError("aborted")
        time.sleep(interval)
        status = api_call(f"{RENDERFUL_API_BASE}/generations/{gen_id}", key)
        state = status.get("status")
        if state == "completed":
            return status
        if state == "failed":
            raise RuntimeError(f"generation failed: {status.get('error') or status}")
        if on_state:
            on_state(state)
    raise RuntimeError(f"timed out after {timeout}s waiting for {gen_id}")


def download(url: str, timeout: int = 120) -> bytes:
    """Fetch one delivered asset.

    The URL is whatever the API put in the response, and it is handed straight
    to urlopen -- which speaks file:// as fluently as https://. A response
    pointing at `file:///C:/Users/you/anthropic_key.txt` would have been read and
    saved into the project as that scene's picture. The scheme is checked before
    the first attempt and the read is bounded, so neither a redirect nor an
    endless stream turns a render into something else.
    """
    security.check_download_url(url)
    last: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                # One byte past the ceiling is all it takes to know it was
                # crossed, and it costs nothing to trust that rather than a
                # Content-Length the sender chose for us.
                data = resp.read(security.MAX_DOWNLOAD_BYTES + 1)
        except Exception as e:  # noqa: BLE001 - retry anything transient
            last = e
            if attempt < HTTP_RETRIES:
                time.sleep(2 * attempt)
            continue
        # Outside the retry: an asset that is too big now will be too big again.
        if len(data) > security.MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"refusing an asset larger than the "
                f"{security.MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit")
        return data
    raise RuntimeError(f"download failed: {last}")


def sniff(data: bytes) -> str:
    """The format the bytes actually are, whatever was requested (FR-905)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    # ElevenLabs delivers MP3 with an ID3v2 tag, but a stripped file starts
    # straight on the 11-bit frame sync, so both have to count.
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return "bin"


def save_delivered(data: bytes, dest_no_ext: Path, convert_png: bool = False) -> Path:
    """Write bytes using the extension the payload actually is (FR-905).

    convert_png re-encodes to PNG. Opt-in: it costs ~6x the bytes for identical
    pixels and needs Pillow.
    """
    kind = sniff(data)
    if convert_png and kind != "png":
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            dest = dest_no_ext.with_suffix(".png")
            with Image.open(io.BytesIO(data)) as im:
                im.convert("RGB").save(dest, format="PNG", optimize=True)
            return dest
    dest = dest_no_ext.with_suffix("." + (kind if kind != "bin" else "img"))
    dest.write_bytes(data)
    return dest


def outputs_of(status: dict) -> list[str]:
    outs = status.get("outputs")
    if outs:
        return list(outs)
    single = status.get("output")
    return [single] if single else []
