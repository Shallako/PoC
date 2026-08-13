"""Local HTTP API + the wizard UI.

Single user, single process, bound to localhost. The only endpoint that spends
money is POST /render, and it refuses to run without an explicit confirmation
flag (FR-303, FR-806, NFR-3).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import compiler, config, engines, narration as narration_mod, orchestrator, store

app = FastAPI(title="Shoulico (local MVP)", docs_url="/api/docs", redoc_url=None)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class NewProject(BaseModel):
    name: str
    story: str = ""
    scene_count: int = config.DEFAULT_SCENE_COUNT
    engine: str | None = None


class ScenePatch(BaseModel):
    n: int
    title: str | None = None
    beat: str | None = None
    body: str | None = None
    narration: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    story: str | None = None
    style_hint: str | None = None
    style_profile: str | None = None
    engine: str | None = None
    params: dict | None = None
    scene_count: int | None = None
    narration: dict | None = None
    scenes: list[ScenePatch] | None = None


class SegmentRequest(BaseModel):
    scene_count: int | None = None
    style_hint: str | None = None


class PlanRequest(BaseModel):
    scenes: list[int] | None = None
    force: bool = False


class RenderRequest(PlanRequest):
    confirm: bool = False


class NarrationRequest(BaseModel):
    voice: str | None = None
    seconds_per_scene: int | None = None


class ExportRequest(BaseModel):
    flatten: bool = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load(pid: str) -> dict:
    try:
        return store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, f"No project {pid!r}") from None


def _decorate(project: dict) -> dict:
    """Add derived, non-persisted fields for the UI."""
    dialect = engines.engine(project["engine"]).get("dialect", {})
    scenes = []
    for scene in sorted(project.get("scenes", []), key=lambda s: s["n"]):
        prompt = compiler.compile_prompt(scene.get("body", ""),
                                         project.get("style_profile", ""), dialect)
        text = scene.get("narration") or ""
        scenes.append({
            **scene,
            "compiled_prompt": prompt,
            "prompt_chars": len(prompt),
            "dirty": bool(scene.get("asset")) and scene.get("asset_prompt") != prompt,
            "narration_words": narration_mod.word_count(text),
            "narration_seconds": narration_mod.estimate_seconds(text),
        })
    out = dict(project)
    out["scenes"] = scenes
    out["job"] = orchestrator.status(project["id"])
    out["keys"] = config.key_status()
    out["narration_full"] = narration_mod.full_script(project.get("scenes", []))
    return out


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #

@app.get("/api/status")
def api_status() -> dict:
    return {
        "keys": config.key_status(),
        "claude_model": config.DEFAULT_CLAUDE_MODEL,
        "max_story_chars": config.MAX_STORY_CHARS,
        "workers": config.WORKERS,
        "projects_dir": str(config.PROJECTS_DIR),
    }


@app.get("/api/engines")
def api_engines() -> dict:
    return engines.public_registry()


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

@app.get("/api/projects")
def api_projects() -> list[dict]:
    return store.list_projects()


@app.post("/api/projects")
def api_create(body: NewProject) -> dict:
    if not body.name.strip():
        raise HTTPException(400, "A project name is required.")
    if len(body.story) > config.MAX_STORY_CHARS:
        raise HTTPException(400, f"The story exceeds {config.MAX_STORY_CHARS} characters.")
    project = store.create(body.name, body.story, engine=body.engine,
                           scene_count=body.scene_count)
    return _decorate(project)


@app.get("/api/projects/{pid}")
def api_project(pid: str) -> dict:
    return _decorate(_load(pid))


@app.delete("/api/projects/{pid}")
def api_delete(pid: str) -> dict:
    _load(pid)
    job = orchestrator.job_for(pid)
    if job and job.running:
        raise HTTPException(409, "A render is running for this project. Cancel it first.")
    store.delete(pid)
    return {"deleted": pid}


@app.patch("/api/projects/{pid}")
def api_patch(pid: str, body: ProjectPatch) -> dict:
    _load(pid)
    if body.story is not None and len(body.story) > config.MAX_STORY_CHARS:
        raise HTTPException(400, f"The story exceeds {config.MAX_STORY_CHARS} characters.")

    if body.engine is not None and body.engine not in engines.registry()["engines"]:
        raise HTTPException(400, f"Unknown engine {body.engine!r}")

    def apply(project):
        if body.name is not None:
            project["name"] = body.name.strip() or project["name"]
        if body.story is not None:
            project["story"] = body.story
        if body.style_hint is not None:
            project["style_hint"] = body.style_hint
        if body.style_profile is not None:
            project["style_profile"] = body.style_profile
        if body.scene_count is not None:
            project["scene_count"] = max(1, min(int(body.scene_count), config.MAX_SCENE_COUNT))
        if body.narration is not None:
            project["narration"] = {**project.get("narration", {}), **body.narration}
        if body.engine is not None and body.engine != project["engine"]:
            project["engine"] = body.engine
            project["params"] = engines.defaults_for(body.engine)
        if body.params is not None:
            merged = {**project.get("params", {}), **body.params}
            project["params"] = engines.validate(project["engine"], merged)
        if body.scenes:
            by_n = {s["n"]: s for s in project["scenes"]}
            for patch in body.scenes:
                scene = by_n.get(patch.n)
                if scene is None:
                    continue
                if patch.title is not None:
                    scene["title"] = patch.title.strip()
                    from .naming import slugify
                    scene["slug"] = slugify(scene["title"])
                if patch.beat is not None:
                    scene["beat"] = patch.beat
                if patch.body is not None:
                    scene["body"] = patch.body
                if patch.narration is not None:
                    scene["narration"] = patch.narration

    try:
        project = store.mutate(pid, apply)
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None
    return _decorate(project)


# --------------------------------------------------------------------------- #
# Step 1 -> 2: segmentation + prompt compilation
# --------------------------------------------------------------------------- #

@app.post("/api/projects/{pid}/segment")
def api_segment(pid: str, body: SegmentRequest) -> dict:
    project = _load(pid)
    job = orchestrator.job_for(pid)
    if job and job.running:
        raise HTTPException(409, "A render is running; cancel it before re-segmenting.")

    count = body.scene_count or project.get("scene_count") or config.DEFAULT_SCENE_COUNT
    hint = body.style_hint if body.style_hint is not None else project.get("style_hint", "")
    eng = engines.engine(project["engine"])

    try:
        result = compiler.segment(
            project.get("story", ""), count,
            style_hint=hint,
            api_key=config.anthropic_key(),
            engine_name=eng.get("name", project["engine"]),
            dialect_notes=eng.get("dialect", {}).get("notes", []),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except ImportError:
        raise HTTPException(
            500, "The anthropic package is not installed. Run: pip install -r requirements.txt"
        ) from None
    except Exception as e:  # noqa: BLE001 - surface the model/network error verbatim
        raise HTTPException(502, f"Claude segmentation failed: {e}") from None

    def apply(proj):
        # Keep anything already rendered for a scene whose prompt body is unchanged.
        previous = {s["n"]: s for s in proj.get("scenes", [])}
        scenes = []
        for s in result["scenes"]:
            old = previous.get(s["n"], {})
            scenes.append({
                "n": s["n"],
                "title": s["title"],
                "slug": s["slug"],
                "beat": s["beat"],
                "body": s["body"],
                "narration": old.get("narration", ""),
                "version": int(old.get("version") or 1),
                "status": old.get("status", "pending") if old.get("asset") else "pending",
                "detail": "",
                "asset": old.get("asset"),
                "asset_prompt": old.get("asset_prompt"),
                "seed": old.get("seed"),
                "cost": old.get("cost"),
                "generation_id": old.get("generation_id"),
            })
        proj["scenes"] = scenes
        proj["style_profile"] = result["style_profile"]
        proj["style_hint"] = hint
        proj["scene_count"] = len(scenes)

    return _decorate(store.mutate(pid, apply))


# --------------------------------------------------------------------------- #
# Step 3: preview cost, then spend
# --------------------------------------------------------------------------- #

@app.post("/api/projects/{pid}/plan")
def api_plan(pid: str, body: PlanRequest) -> dict:
    project = _load(pid)
    try:
        return orchestrator.plan(project, body.scenes, body.force)
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/projects/{pid}/render")
def api_render(pid: str, body: RenderRequest) -> dict:
    _load(pid)
    if not body.confirm:
        raise HTTPException(400, "Rendering spends money and needs an explicit confirmation.")
    try:
        return orchestrator.start(pid, body.scenes, body.force)
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/projects/{pid}/cancel")
def api_cancel(pid: str) -> dict:
    _load(pid)
    return {"cancelling": orchestrator.cancel(pid)}


@app.get("/api/projects/{pid}/image/{name}")
def api_image(pid: str, name: str):
    _load(pid)
    safe = Path(name).name
    path = store.images_dir(pid) / safe
    if not path.is_file():
        raise HTTPException(404, "No such image")
    return FileResponse(path)


# --------------------------------------------------------------------------- #
# Step 3b: narration script (no TTS)
# --------------------------------------------------------------------------- #

@app.post("/api/projects/{pid}/narration")
def api_narration(pid: str, body: NarrationRequest) -> dict:
    project = _load(pid)
    settings = {**project.get("narration", {})}
    if body.voice is not None:
        settings["voice"] = body.voice
    if body.seconds_per_scene is not None:
        settings["seconds_per_scene"] = max(2, min(int(body.seconds_per_scene), 60))

    try:
        lines = narration_mod.generate(
            project.get("story", ""), project.get("scenes", []),
            voice=settings.get("voice", ""),
            seconds_per_scene=int(settings.get("seconds_per_scene", 8)),
            api_key=config.anthropic_key(),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except ImportError:
        raise HTTPException(
            500, "The anthropic package is not installed. Run: pip install -r requirements.txt"
        ) from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Claude narration failed: {e}") from None

    def apply(proj):
        proj["narration"] = settings
        for scene in proj["scenes"]:
            if scene["n"] in lines:
                scene["narration"] = lines[scene["n"]]

    return _decorate(store.mutate(pid, apply))


@app.post("/api/projects/{pid}/narration/save")
def api_narration_save(pid: str) -> dict:
    project = _load(pid)
    written = store.write_narration_files(pid, project)
    return {"written": written, "dir": str(store.narration_dir(pid))}


# --------------------------------------------------------------------------- #
# Step 4: export
# --------------------------------------------------------------------------- #

@app.post("/api/projects/{pid}/export")
def api_export(pid: str, body: ExportRequest) -> dict:
    project = _load(pid)
    store.write_narration_files(pid, project)
    return store.export(pid, project, flatten=body.flatten)


@app.get("/api/projects/{pid}/manifest")
def api_manifest(pid: str) -> dict:
    _load(pid)
    return store.read_manifest(pid)
