"""Project persistence + the provenance manifest (PRD §10, FR-901/902).

Everything is plain files on disk so the project stays inspectable and
diffable. Writes go through a temp file + replace, under a per-project lock,
because render workers write concurrently.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config, engines
from .naming import asset_stem, flat_stem, full_voiceover_stem, project_slug

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


def export_dir(pid: str) -> Path:
    return project_dir(pid) / "export"


def project_file(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def manifest_file(pid: str) -> Path:
    return project_dir(pid) / "manifest.json"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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
            p = json.loads(pf.read_text(encoding="utf-8"))
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
    project = {
        "id": pid,
        "name": name.strip() or pid,
        "created_at": now(),
        "updated_at": now(),
        "story": story or "",
        "style_hint": "",
        "style_profile": "",
        "engine": engine_key,
        "params": engines.defaults_for(engine_key),
        "scene_count": scene_count,
        "narration": {"voice": "", "seconds_per_scene": 8, "mode": "per-scene"},
        "scenes": [],
        "spend": {"images": 0, "actual": 0.0},
    }
    for d in (images_dir(pid), narration_dir(pid), export_dir(pid)):
        d.mkdir(parents=True, exist_ok=True)
    _write_json(project_file(pid), project)
    _write_json(manifest_file(pid), {})
    return project


def load(pid: str) -> dict:
    pf = project_file(pid)
    if not pf.is_file():
        raise FileNotFoundError(f"no project {pid!r}")
    return json.loads(pf.read_text(encoding="utf-8"))


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
            data = json.loads(path.read_text(encoding="utf-8"))
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

        row = {"scene": scene["n"], "image": dest.name, "narration": ""}
        text = (scene.get("narration") or "").strip()
        if text:
            nfile = edir / (stem + ".txt")
            nfile.write_text(text + "\n", encoding="utf-8")
            row["narration"] = nfile.name
        rows.append(row)

    from .narration import full_script
    full = full_script(project.get("scenes", []))
    if full:
        (edir / (full_voiceover_stem(pid) + ".txt")).write_text(full + "\n", encoding="utf-8")

    if manifest_file(pid).is_file():
        shutil.copy2(manifest_file(pid), edir / "manifest.json")
    return {"dir": str(edir), "files": rows, "full_voiceover": bool(full)}
