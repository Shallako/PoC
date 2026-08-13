"""Project persistence + the provenance manifest (PRD §10, FR-901/902).

Everything is plain files on disk so the project stays inspectable and
diffable. Writes go through a temp file + replace, under a per-project lock,
because render workers write concurrently.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from . import config, engines
from .naming import (asset_stem, audio_stem, captions_stem, flat_stem,
                     full_voiceover_stem, narration_key, project_slug,
                     timing_stem, video_stem)

# MP4 because it is the container CapCut, Premiere, Resolve and every phone
# already open without being told anything.
VIDEO_SUFFIX = ".mp4"
CAPTIONS_SUFFIX = ".srt"

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock_for(pid: str) -> threading.Lock:
    with _locks_guard:
        return _locks[pid]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def project_dir(pid: str) -> Path:
    return config.PROJECTS_DIR / pid


def images_dir(pid: str) -> Path:
    return project_dir(pid) / "images"


def narration_dir(pid: str) -> Path:
    return project_dir(pid) / "narration"


def audio_dir(pid: str) -> Path:
    return project_dir(pid) / "audio"


def video_dir(pid: str) -> Path:
    return project_dir(pid) / "video"


def export_dir(pid: str) -> Path:
    return project_dir(pid) / "export"


def project_file(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def manifest_file(pid: str) -> Path:
    return project_dir(pid) / "manifest.json"


# Windows refuses to replace a file another handle has open, and the UI polls
# project.json while workers write it. The temp name is per-write so two writers
# can never share one, and the swap is retried briefly rather than losing the
# render state a paid job just produced.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.05


def _read_json(path: Path):
    """Read JSON a worker thread may be replacing underneath us.

    The swap below is as close to atomic as Windows offers, but a reader that
    opens the file inside that window still gets PermissionError. The UI polls
    the project throughout a render, so this is a normal event, not an error.
    """
    last: PermissionError | None = None
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as e:
            last = e
            time.sleep(_REPLACE_BACKOFF_SECONDS * attempt)
    raise last


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        for attempt in range(1, _REPLACE_ATTEMPTS + 1):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS:
                    raise
                time.sleep(_REPLACE_BACKOFF_SECONDS * attempt)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

def list_projects() -> list[dict]:
    out = []
    if not config.PROJECTS_DIR.is_dir():
        return out
    for d in sorted(config.PROJECTS_DIR.iterdir()):
        pf = d / "project.json"
        if not pf.is_file():
            continue
        try:
            p = _read_json(pf)
        except Exception:
            continue
        scenes = p.get("scenes", [])
        out.append({
            "id": p.get("id", d.name),
            "name": p.get("name", d.name),
            "updated_at": p.get("updated_at", ""),
            "scenes": len(scenes),
            "rendered": sum(1 for s in scenes if s.get("status") == "done"),
        })
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return out


def exists(pid: str) -> bool:
    return project_file(pid).is_file()


def create(name: str, story: str = "", *, engine: str | None = None,
           scene_count: int = config.DEFAULT_SCENE_COUNT) -> dict:
    base = project_slug(name) or "project"
    pid, i = base, 2
    while exists(pid):
        pid, i = f"{base}-{i}", i + 1

    engine_key = engine or engines.default_engine_key()
    voice_key = engines.default_voice_key()
    video_key = engines.default_video_key()
    project = {
        "id": pid,
        "name": name.strip() or pid,
        "created_at": now(),
        "updated_at": now(),
        "story": story or "",
        "style_hint": "",
        "style_profile": "",
        # Filled in by segmentation, from the story itself.
        "language": {"code": "", "name": "", "native_name": ""},
        "prompt_language": "story",

        "engine": engine_key,
        "params": engines.defaults_for(engine_key),
        "scene_count": scene_count,
        "narration": {
            "voice": "",                     # narrator tone, fed to Claude
            "seconds_per_scene": config.DEFAULT_SECONDS_PER_SCENE,
            "mode": "per-scene",
            # Which TTS model speaks the script, and how.
            "voice_engine": voice_key,
            "voice_params": engines.defaults_for(voice_key, engines.SECTION_VOICES),
        },
        # How the finished cut is assembled. Local and free, so no spend key.
        "video": {
            "profile": video_key,
            "params": engines.defaults_for(video_key, engines.SECTION_VIDEO),
        },
        "scenes": [],
        "spend": {"images": 0, "lines": 0, "actual": 0.0},
    }
    for d in (images_dir(pid), narration_dir(pid), audio_dir(pid),
              video_dir(pid), export_dir(pid)):
        d.mkdir(parents=True, exist_ok=True)
    _write_json(project_file(pid), project)
    _write_json(manifest_file(pid), {})
    return project


def load(pid: str) -> dict:
    pf = project_file(pid)
    if not pf.is_file():
        raise FileNotFoundError(f"no project {pid!r}")
    return _read_json(pf)


def save(project: dict) -> dict:
    project["updated_at"] = now()
    _write_json(project_file(project["id"]), project)
    return project


def mutate(pid: str, fn: Callable[[dict], None]) -> dict:
    """Load -> apply -> save, atomically with respect to other threads."""
    with _lock_for(pid):
        project = load(pid)
        fn(project)
        return save(project)


def delete(pid: str) -> None:
    with _lock_for(pid):
        shutil.rmtree(project_dir(pid), ignore_errors=True)


# --------------------------------------------------------------------------- #
# Manifest (PRD §10)
# --------------------------------------------------------------------------- #

def record_asset(pid: str, entry: dict) -> None:
    """One record per asset, keyed by its versioned stem."""
    with _lock_for(pid + "::manifest"):
        path = manifest_file(pid)
        try:
            data = _read_json(path)
        except Exception:
            data = {}
        data[entry["key"]] = entry
        _write_json(path, data)


def read_manifest(pid: str) -> dict:
    try:
        return json.loads(manifest_file(pid).read_text(encoding="utf-8"))
    except Exception:
        return {}


def manifest_entry(pid: str, scene: dict, project: dict, *, engine_model: str,
                   prompt: str, params: dict, generation_id: str | None,
                   file_name: str, cost, source_url: str | None) -> dict:
    key = asset_stem(pid, scene["n"], scene["slug"], scene.get("version", 1),
                     params.get("seed"))
    return {
        "key": key,
        "project": pid,
        "scene": scene["n"],
        "slug": scene["slug"],
        "title": scene.get("title", ""),
        "version": scene.get("version", 1),
        "engine": project.get("engine"),
        "model": engine_model,
        "seed": params.get("seed"),
        "params": params,
        "prompt": prompt,
        "file": file_name,
        "generation_id": generation_id,
        "source_url": source_url,
        "cost": cost,
        "created_at": now(),
        "selected": True,
    }


def narration_manifest_entry(pid: str, scene: dict, project: dict, *, voice_key: str,
                             model: str, text: str, params: dict,
                             generation_id: str | None, file_name: str, cost,
                             seconds: float | None, measured: bool,
                             source_url: str | None) -> dict:
    """Provenance for one spoken line, alongside the image record for the scene.

    `seconds` is the number every downstream step depends on, so the manifest
    records whether it was measured off the delivered bytes or fell back to the
    word-count estimate.
    """
    return {
        "key": narration_key(pid, scene["n"], scene["slug"]),
        "kind": "narration",
        "project": pid,
        "scene": scene["n"],
        "slug": scene["slug"],
        "title": scene.get("title", ""),
        "voice_engine": voice_key,
        "model": model,
        "params": params,
        "text": text,
        "language": (project.get("language") or {}).get("code", ""),
        "file": file_name,
        "generation_id": generation_id,
        "source_url": source_url,
        "cost": cost,
        "seconds": seconds,
        "seconds_measured": measured,
        "created_at": now(),
        "selected": True,
    }


# --------------------------------------------------------------------------- #
# Export (FR-904, §10)
# --------------------------------------------------------------------------- #

def write_narration_files(pid: str, project: dict) -> list[str]:
    """Per-scene lines share the image stem; the full track gets its own file."""
    ndir = narration_dir(pid)
    ndir.mkdir(parents=True, exist_ok=True)
    written = []
    for scene in project.get("scenes", []):
        text = (scene.get("narration") or "").strip()
        if not text:
            continue
        name = flat_stem(pid, scene["n"], scene["slug"]) + ".txt"
        (ndir / name).write_text(text + "\n", encoding="utf-8")
        written.append(name)

    from .narration import full_script
    full = full_script(project.get("scenes", []))
    if full:
        name = full_voiceover_stem(pid) + ".txt"
        (ndir / name).write_text(full + "\n", encoding="utf-8")
        written.append(name)
    return written


def export(pid: str, project: dict, *, flatten: bool = True) -> dict:
    """Copy selected renders (and narration) into export/ under editor-friendly names."""
    edir = export_dir(pid)
    edir.mkdir(parents=True, exist_ok=True)
    for old in edir.iterdir():
        if old.is_file():
            old.unlink()

    rows = []
    for scene in sorted(project.get("scenes", []), key=lambda s: s["n"]):
        asset = scene.get("asset")
        if not asset:
            continue
        src = images_dir(pid) / asset
        if not src.is_file():
            continue
        stem = (flat_stem(pid, scene["n"], scene["slug"]) if flatten
                else Path(asset).stem)
        dest = edir / (stem + src.suffix)
        shutil.copy2(src, dest)

        row = {"scene": scene["n"], "image": dest.name, "narration": "",
               "audio": "", "seconds": scene.get("audio_seconds")}
        text = (scene.get("narration") or "").strip()
        if text:
            nfile = edir / (stem + ".txt")
            nfile.write_text(text + "\n", encoding="utf-8")
            row["narration"] = nfile.name

        # Voice travels under the image's stem, so an editor pairs them on sight.
        spoken = scene.get("audio")
        if spoken:
            asrc = audio_dir(pid) / spoken
            if asrc.is_file():
                adest = edir / (audio_stem(pid, scene["n"], scene["slug"]) + asrc.suffix)
                shutil.copy2(asrc, adest)
                row["audio"] = adest.name
        rows.append(row)

    from .narration import full_script
    full = full_script(project.get("scenes", []))
    if full:
        (edir / (full_voiceover_stem(pid) + ".txt")).write_text(full + "\n", encoding="utf-8")

    extras = write_editor_files(pid, project, edir)

    if manifest_file(pid).is_file():
        shutil.copy2(manifest_file(pid), edir / "manifest.json")
    return {"dir": str(edir), "files": rows, "full_voiceover": bool(full), **extras}


def write_editor_files(pid: str, project: dict, edir: Path) -> dict:
    """Captions, a timing sheet, and the finished cut if one has been assembled.

    This is the hand-off half of export: CapCut (or Premiere, or Resolve) can
    import an SRT and place every line at the right moment instead of the editor
    dragging clips against a waveform by eye. It costs nothing and needs no
    ffmpeg, so it happens on every export whether a video was assembled or not.
    """
    # Local import: timeline reads this module's directory helpers.
    from . import captions as captions_mod
    from . import timeline as timeline_mod

    settings = video_settings(project)
    beats, _ = timeline_mod.build(project, settings)
    cues = captions_mod.build(beats)

    out: dict = {"captions": "", "captions_vtt": "", "timing": "", "video": "",
                 "runtime_seconds": timeline_mod.total_seconds(beats),
                 "runtime_measured": timeline_mod.all_measured(beats)}
    if cues:
        srt = edir / (captions_stem(pid) + ".srt")
        vtt = edir / (captions_stem(pid) + ".vtt")
        srt.write_text(captions_mod.to_srt(cues), encoding="utf-8")
        vtt.write_text(captions_mod.to_vtt(cues), encoding="utf-8")
        out["captions"], out["captions_vtt"] = srt.name, vtt.name

    if beats:
        sheet = edir / (timing_stem(pid) + ".csv")
        sheet.write_text(timing_csv(beats), encoding="utf-8", newline="")
        out["timing"] = sheet.name

    assembled = video_dir(pid) / (video_stem(pid) + VIDEO_SUFFIX)
    if assembled.is_file():
        dest = edir / assembled.name
        shutil.copy2(assembled, dest)
        out["video"] = dest.name
    return out


def timing_csv(beats: list) -> str:
    """One row per scene: where it starts, how long it holds, what is said.

    Written with the csv module rather than joined by hand because a narration
    line contains commas and quotation marks by nature, and a hand-joined sheet
    would silently corrupt the very column an editor cares about.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["scene", "title", "start_seconds", "duration_seconds",
                     "speech_start_seconds", "speech_seconds", "duration_source",
                     "image", "audio", "narration"])
    for b in beats:
        writer.writerow([
            b.n, b.title, f"{b.start:.3f}", f"{b.duration:.3f}",
            f"{b.speech_start:.3f}", f"{b.speech_seconds:.3f}",
            "measured" if b.measured else "estimated",
            b.image.name, b.audio.name if b.audio else "", b.text,
        ])
    return buf.getvalue()


def video_settings(project: dict) -> dict:
    """Validated assembly settings, falling back to the shipped defaults.

    Export must never fail because a stored setting went stale, so anything the
    schema rejects is replaced rather than raised -- the settings endpoint is
    where a bad value gets reported.
    """
    stored = project.get("video") or {}
    key = stored.get("profile") or engines.default_video_key()
    try:
        return engines.validate(key, stored.get("params"), engines.SECTION_VIDEO)
    except engines.ParamError:
        return engines.defaults_for(engines.default_video_key(), engines.SECTION_VIDEO)
