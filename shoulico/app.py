"""Local HTTP API + the wizard UI.

Single user, single process, bound to localhost. The only endpoint that spends
money is POST /render, and it refuses to run without an explicit confirmation
flag (FR-303, FR-806, NFR-3).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import (captions, compiler, config, engines, i18n,
               narration as narration_mod, orchestrator, store, timeline, video)

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
    clip_params: dict | None = None
    scene_count: int | None = None
    prompt_language: str | None = None
    narration: dict | None = None
    scenes: list[ScenePatch] | None = None


class SegmentRequest(BaseModel):
    scene_count: int | None = None
    style_hint: str | None = None
    prompt_language: str | None = None


class PlanRequest(BaseModel):
    scenes: list[int] | None = None
    force: bool = False


class RenderRequest(PlanRequest):
    confirm: bool = False


class NarrationRequest(BaseModel):
    voice: str | None = None
    seconds_per_scene: int | None = None


class VoiceRequest(BaseModel):
    """Which TTS model speaks the script, and how."""

    voice_engine: str | None = None
    voice_params: dict | None = None


class SpeakRequest(PlanRequest):
    confirm: bool = False


class VideoRequest(BaseModel):
    """How the finished cut is assembled. Local work, so nothing here spends."""

    profile: str | None = None
    params: dict | None = None


class ExportRequest(BaseModel):
    flatten: bool = True


class UiStringsRequest(BaseModel):
    """The page's own English strings, sent up to be localised."""
    code: str
    name: str = ""
    native_name: str = ""
    strings: dict[str, str]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load(pid: str) -> dict:
    try:
        return store.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, f"No project {pid!r}") from None


def _claude_error(e: compiler.ClaudeError) -> HTTPException:
    """A Claude failure the user can act on.

    Capacity problems are a 503 with Retry-After -- the request is fine, the
    moment isn't -- and everything else is a 502 from an upstream we depend on.
    The machine facts ride along in headers so the page can tuck them behind a
    details toggle instead of putting a request id in the sentence.
    """
    status = 503 if e.kind in ("overloaded", "rate_limit") else 502
    headers = {}
    if e.request_id:
        headers["X-Claude-Request-Id"] = e.request_id
    if e.status:
        headers["X-Claude-Status"] = str(e.status)
    if status == 503:
        headers["Retry-After"] = str(int(e.retry_after or 30))
    return HTTPException(status, str(e), headers=headers or None)


def _prompt_language(value: str | None) -> str:
    """'story' (the language the author wrote in) or 'en'. Anything else is a 400."""
    choice = (value or "story").strip().lower()
    if choice not in compiler.PROMPT_LANGUAGE_CHOICES:
        raise HTTPException(
            400, f"prompt_language must be one of "
                 f"{', '.join(compiler.PROMPT_LANGUAGE_CHOICES)}, not {value!r}"
        )
    return choice


def _decorate(project: dict) -> dict:
    """Add derived, non-persisted fields for the UI."""
    dialect = engines.engine(project["engine"]).get("dialect", {})
    scenes = []
    for scene in sorted(project.get("scenes", []), key=lambda s: s["n"]):
        prompt = compiler.compile_prompt(scene.get("body", ""),
                                         project.get("style_profile", ""), dialect)
        text = scene.get("narration") or ""
        count, unit, seconds = narration_mod.measure(text)
        scenes.append({
            **scene,
            "compiled_prompt": prompt,
            "prompt_chars": len(prompt),
            "dirty": bool(scene.get("asset")) and scene.get("asset_prompt") != prompt,
            "narration_words": count,
            "narration_unit": unit,
            "narration_seconds": seconds,
        })
    out = dict(project)
    out["scenes"] = scenes
    # Show the browser the values that would actually be sent. A project saved
    # under an older voice name still holds it, and a control whose stored value
    # matches no option silently displays the first one instead -- so the page
    # would name a different voice than the run would use. Resolving is enough;
    # the next save persists it. Anything validate rejects is left alone so the
    # settings endpoint can report it rather than this one swallowing it.
    narration = project.get("narration") or {}
    if narration.get("voice_params"):
        try:
            out["narration"] = {
                **narration,
                "voice_params": engines.validate(
                    narration.get("voice_engine") or engines.default_voice_key(),
                    narration["voice_params"], engines.SECTION_VOICES),
            }
        except engines.ParamError:
            pass
    out["job"] = orchestrator.status(project["id"])
    out["audio_job"] = orchestrator.status(project["id"], orchestrator.KIND_AUDIO)
    out["video_job"] = orchestrator.status(project["id"], orchestrator.KIND_VIDEO)
    # Runtime of the finished voice-over: measured where audio exists, estimated
    # where it does not, so the number is honest about which it is.
    out["audio_seconds_total"] = round(
        sum(float(s.get("audio_seconds") or 0.0) for s in project.get("scenes", [])), 2)
    out["audio_lines_done"] = sum(1 for s in project.get("scenes", []) if s.get("audio"))
    out["keys"] = config.key_status()
    out["narration_full"] = narration_mod.full_script(project.get("scenes", []))
    out.setdefault("language", {"code": "", "name": "", "native_name": ""})
    out.setdefault("prompt_language", "story")
    out.setdefault("claude_model", "")
    out.setdefault("claude_fell_back", False)
    # Present for every project, empty for an image-only engine, so the page can
    # ask "does this engine do video" without knowing the engine list.
    out.setdefault("clip_params", engines.clip_defaults(project["engine"]))
    out["price_per_clip"] = engines.price_per_clip(project["engine"],
                                                   out.get("clip_params"))
    try:
        out["price_per_image"] = engines.price_per_image(project["engine"],
                                                         project.get("params"))
    except engines.ParamError:
        out["price_per_image"] = 0.0
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
# Interface language (follows the story)
# --------------------------------------------------------------------------- #

@app.post("/api/ui/strings")
def api_ui_strings(body: UiStringsRequest) -> dict:
    """Localise the page's own strings into `code`.

    The page owns the English; this only translates and caches. A language costs
    one Claude call the first time it is asked for, and nothing after that.
    """
    try:
        return i18n.strings_for(
            body.code, body.strings,
            name=body.name, native_name=body.native_name,
            api_key=config.anthropic_key(),
        )
    except i18n.TooMuchText as e:
        raise HTTPException(400, str(e)) from None
    except compiler.ClaudeError as e:
        raise _claude_error(e) from None
    except ImportError:
        raise HTTPException(
            500, "The anthropic package is not installed. Run: pip install -r requirements.txt"
        ) from None
    except Exception as e:  # noqa: BLE001 - the page falls back to English on any failure
        raise HTTPException(502, f"Claude could not translate the interface: {e}") from None


@app.get("/api/ui/languages")
def api_ui_languages() -> dict:
    """Languages this machine has already paid to translate."""
    have = []
    if config.I18N_DIR.is_dir():
        have = sorted(p.stem for p in config.I18N_DIR.glob("*.json"))
    return {"cached": have, "dir": str(config.I18N_DIR)}


@app.delete("/api/ui/strings/{code}")
def api_ui_strings_forget(code: str) -> dict:
    return {"forgotten": i18n.forget(code)}


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
        if body.prompt_language is not None:
            project["prompt_language"] = _prompt_language(body.prompt_language)
        if body.narration is not None:
            project["narration"] = {**project.get("narration", {}), **body.narration}
        if body.engine is not None and body.engine != project["engine"]:
            project["engine"] = body.engine
            project["params"] = engines.defaults_for(body.engine)
            # The video sibling belongs to the engine, so switching engine
            # replaces its settings rather than carrying Veo's frame over to Sora.
            project["clip_params"] = engines.clip_defaults(body.engine)
        if body.params is not None:
            merged = {**project.get("params", {}), **body.params}
            project["params"] = engines.validate(project["engine"], merged)
        if body.clip_params is not None:
            merged = {**project.get("clip_params", {}), **body.clip_params}
            project["clip_params"] = engines.validate_clip(project["engine"], merged)
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
    prompt_lang = _prompt_language(
        body.prompt_language if body.prompt_language is not None
        else project.get("prompt_language")
    )
    eng = engines.engine(project["engine"])

    try:
        result = compiler.segment(
            project.get("story", ""), count,
            style_hint=hint,
            api_key=config.anthropic_key(),
            engine_name=eng.get("name", project["engine"]),
            dialect_notes=eng.get("dialect", {}).get("notes", []),
            prompt_language=prompt_lang,
        )
    except compiler.ClaudeError as e:
        raise _claude_error(e) from None
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
        proj["language"] = result["language"]
        proj["prompt_language"] = prompt_lang
        # Which model actually wrote these scenes. It is the requested one unless
        # that was overloaded and the fallback answered instead.
        proj["claude_model"] = result.get("model") or config.DEFAULT_CLAUDE_MODEL
        proj["claude_fell_back"] = bool(result.get("fell_back"))

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
            language=project.get("language"),
            api_key=config.anthropic_key(),
        )
    except compiler.ClaudeError as e:
        raise _claude_error(e) from None
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
# Step 3c: narration audio (TTS)
#
# Speaking the script spends money, so it follows the render contract exactly:
# preview the cost first, and POST .../speak without confirm is a 400.
# --------------------------------------------------------------------------- #

@app.get("/api/voices")
def api_voices() -> dict:
    return engines.public_voices()


@app.post("/api/projects/{pid}/narration/voice")
def api_voice_settings(pid: str, body: VoiceRequest) -> dict:
    project = _load(pid)
    settings = {**project.get("narration", {})}
    voice_key = body.voice_engine or settings.get("voice_engine") \
        or engines.default_voice_key()
    try:
        params = engines.validate(
            voice_key,
            body.voice_params if body.voice_params is not None
            else settings.get("voice_params"),
            engines.SECTION_VOICES,
        )
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None

    def apply(proj):
        narration = proj.setdefault("narration", {})
        narration["voice_engine"] = voice_key
        narration["voice_params"] = params
    return _decorate(store.mutate(pid, apply))


@app.post("/api/projects/{pid}/narration/plan-audio")
def api_plan_audio(pid: str, body: PlanRequest) -> dict:
    project = _load(pid)
    try:
        return orchestrator.plan_audio(project, body.scenes, body.force)
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/projects/{pid}/narration/speak")
def api_speak(pid: str, body: SpeakRequest) -> dict:
    _load(pid)
    if not body.confirm:
        raise HTTPException(
            400, "Synthesising narration spends money and needs an explicit confirmation.")
    try:
        return orchestrator.start_audio(pid, body.scenes, body.force)
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/projects/{pid}/narration/cancel-audio")
def api_cancel_audio(pid: str) -> dict:
    _load(pid)
    return {"cancelling": orchestrator.cancel(pid, orchestrator.KIND_AUDIO)}


@app.get("/api/projects/{pid}/audio/{name}")
def api_audio(pid: str, name: str):
    _load(pid)
    safe = Path(name).name
    path = store.audio_dir(pid) / safe
    if not path.is_file():
        raise HTTPException(404, "No such audio")
    return FileResponse(path)


# --------------------------------------------------------------------------- #
# Step 5: video assembly
# --------------------------------------------------------------------------- #

@app.get("/api/video-profiles")
def api_video_profiles() -> dict:
    return engines.public_video()


@app.post("/api/projects/{pid}/video/settings")
def api_video_settings(pid: str, body: VideoRequest) -> dict:
    project = _load(pid)
    stored = {**(project.get("video") or {})}
    profile = body.profile or stored.get("profile") or engines.default_video_key()
    try:
        params = engines.validate(
            profile,
            body.params if body.params is not None else stored.get("params"),
            engines.SECTION_VIDEO,
        )
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None

    def apply(proj):
        proj["video"] = {**(proj.get("video") or {}), "profile": profile,
                         "params": params}
    return _decorate(store.mutate(pid, apply))


@app.post("/api/projects/{pid}/video/plan")
def api_plan_video(pid: str) -> dict:
    try:
        return orchestrator.plan_video(_load(pid))
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/projects/{pid}/video/assemble")
def api_assemble(pid: str) -> dict:
    """No confirmation gate: assembly runs locally and spends nothing."""
    _load(pid)
    try:
        return orchestrator.start_video(pid)
    except engines.ParamError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        # A missing ffmpeg is a precondition the user can fix, not a server fault.
        code = 409 if video.available() else 424
        raise HTTPException(code, str(e)) from None


@app.post("/api/projects/{pid}/video/cancel")
def api_cancel_video(pid: str) -> dict:
    _load(pid)
    return {"cancelling": orchestrator.cancel(pid, orchestrator.KIND_VIDEO)}


@app.get("/api/projects/{pid}/video/{name}")
def api_video_file(pid: str, name: str):
    _load(pid)
    safe = Path(name).name
    path = store.video_dir(pid) / safe
    if not path.is_file():
        raise HTTPException(404, "No such video")
    return FileResponse(path)


@app.get("/api/projects/{pid}/captions.{fmt}")
def api_captions(pid: str, fmt: str):
    """The captions on their own, without waiting for an export or an encode."""
    if fmt not in ("srt", "vtt"):
        raise HTTPException(404, "Captions come as srt or vtt")
    project = _load(pid)
    beats, _ = timeline.build(project, store.video_settings(project))
    cues = captions.build(beats)
    if not cues:
        raise HTTPException(404, "No narration to caption yet")
    body = captions.to_srt(cues) if fmt == "srt" else captions.to_vtt(cues)
    return PlainTextResponse(body, media_type=(
        "application/x-subrip" if fmt == "srt" else "text/vtt"))


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
