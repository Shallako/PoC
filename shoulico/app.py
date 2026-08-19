"""Local HTTP API + the wizard UI.

Single user, single process, bound to localhost. The only endpoint that spends
money is POST /render, and it refuses to run without an explicit confirmation
flag (FR-303, FR-806, NFR-3).
"""

from __future__ import annotations

import secrets
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import (activity, captions, compiler, config, engines, i18n,
               narration as narration_mod, orchestrator, screening, security,
               store, timeline, video)

# Done at import rather than in run.py: uvicorn --reload runs the app in a child
# process that imports this module directly and never executes run.py, so a
# handler installed there would vanish exactly when it is most wanted.
activity.install_stdout_handler()

app = FastAPI(title="Shoulico (local MVP)", docs_url="/api/docs", redoc_url=None)

# Outermost, so a request from somewhere it should not be is refused before any
# route, body parse or file read happens. See security.py for what it stops and
# why a localhost API with no authentication needs it at all.
app.add_middleware(security.LocalOnly)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class NewProject(BaseModel):
    name: str
    story: str = ""
    scene_count: int = config.DEFAULT_SCENE_COUNT
    engine: str | None = None


class ScreenRequest(BaseModel):
    text: str = ""


class ScenePatch(BaseModel):
    n: int
    title: str | None = None
    beat: str | None = None
    body: str | None = None
    narration: str | None = None


class CastPatch(BaseModel):
    slug: str
    description: str | None = None


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
    consistency: str | None = None
    cast: list[CastPatch] | None = None


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
    except security.BadProjectId:
        # Not a 400: an id this app could never have minted names no project, and
        # saying so in the same words as a missing one tells a prober nothing.
        raise HTTPException(404, "No project by that name.") from None


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


def _cancelled(what: str) -> HTTPException:
    """A phase the user stopped on purpose.

    409 rather than an error status: nothing went wrong, the answer simply is
    not coming. The sentence says what did *not* happen to the project, because
    that is the question someone asks after pressing stop.
    """
    return HTTPException(409, f"{what} was cancelled. Nothing on the project changed.")


def _asset(directory, name: str, missing: str) -> FileResponse:
    """Serve one file from one directory, or 404.

    Every asset route goes through here so that "the name in the URL cannot
    leave this folder" is written once and provable once, rather than being a
    `Path(name).name` that each route has to remember on its own.
    """
    try:
        path = security.safe_child(directory, name)
    except security.BadProjectId:
        raise HTTPException(404, missing) from None
    if not path.is_file():
        raise HTTPException(404, missing)
    return FileResponse(path)


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
    using_cast = orchestrator.consistency_on(project)
    versions = (orchestrator.plan_cast(project)["versions"] if using_cast else {})

    scenes = []
    for scene in sorted(project.get("scenes", []), key=lambda s: s["n"]):
        prompt = compiler.compile_prompt(scene.get("body", ""),
                                         project.get("style_profile", ""), dialect)
        text = scene.get("narration") or ""
        count, unit, seconds = narration_mod.measure(text)
        tokens = (orchestrator.scene_tokens(project, scene, versions)
                  if using_cast else [])
        stale_refs = bool(scene.get("asset")) and list(
            scene.get("asset_refs") or []) != tokens
        # An edited line dates the recording exactly as an edited prompt dates
        # the picture, and the server has always known it -- plan_audio re-speaks
        # a scene whose text moved. Nothing said so anywhere else, so a scene
        # whose narration had been rewritten still read as finished, and the
        # export shipped the voice of a sentence no longer in the project.
        stale_audio = bool(scene.get("audio")) and (
            (scene.get("audio_text") or "") != text.strip())
        scenes.append({
            **scene,
            "compiled_prompt": prompt,
            "prompt_chars": len(prompt),
            # "dirty" is what the gallery badges, and a scene whose anchor moved
            # is every bit as out of date as one whose prompt did -- the picture
            # on screen is not the picture this project would produce now.
            "dirty": bool(scene.get("asset")) and (
                scene.get("asset_prompt") != prompt or stale_refs),
            "stale_references": stale_refs,
            "audio_dirty": stale_audio,
            "narration_words": count,
            "narration_unit": unit,
            "narration_seconds": seconds,
        })
    out = dict(project)
    out["scenes"] = scenes
    # What this project would disown if it were handed over as it stands.
    # Every step that cuts or ships work reads this one summary, so "out of
    # date" means the same thing in the gallery, at the assembly and at the
    # door -- three computations of it would eventually disagree, and the
    # disagreement would show up as a finished video nobody could explain.
    out["stale"] = {
        "images": [s["n"] for s in scenes if s["dirty"]],
        "audio": [s["n"] for s in scenes if s["audio_dirty"]],
        "missing_images": [s["n"] for s in scenes if not s.get("asset")],
        "unspoken": [s["n"] for s in scenes
                     if (s.get("narration") or "").strip() and not s.get("audio")],
    }
    out["consistency_active"] = using_cast
    out["supports_references"] = engines.supports_references(project["engine"])
    out["cast"] = [
        {**member,
         "anchor_prompt": orchestrator.anchor_prompt(project, member),
         "dirty": bool(member.get("asset")) and (
             member.get("asset_prompt") != orchestrator.anchor_prompt(project, member))}
        for member in (project.get("cast") or [])
    ]
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
def index() -> HTMLResponse:
    """The wizard, served under a fresh Content-Security-Policy nonce.

    The page's own script is the only one that gets the nonce, which is what
    lets the policy refuse every *other* inline script -- including the inline
    event handler a scene title or a translated string would have to become in
    order to do any damage.
    """
    nonce = secrets.token_urlsafe(16)
    html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": security.page_csp(nonce),
        "Cache-Control": "no-store",
    })


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #

@app.get("/api/status")
def api_status() -> dict:
    return {
        "keys": config.key_status(),
        # The model the page names, which is the one that segments -- the call
        # whose result the user actually reads and edits.
        "claude_model": config.SEGMENT_MODEL,
        "max_story_chars": config.MAX_STORY_CHARS,
        "workers": config.WORKERS,
        # What the page tells the user while it waits out a 529. Published for
        # the same reason the story limit is: repeating it in the markup means
        # it disagrees with config.py the moment anyone tunes it, and disagrees
        # silently -- the page would count down to a retry that already happened.
        "claude_attempts": config.CLAUDE_ATTEMPTS,
        "claude_backoff": list(config.CLAUDE_BACKOFF),
        "claude_patience": config.claude_patience_seconds(),
        "claude_fallback": config.FALLBACK_CLAUDE_MODEL,
        "projects_dir": str(config.PROJECTS_DIR),
    }


@app.get("/api/engines")
def api_engines() -> dict:
    return engines.public_registry()


@app.post("/api/screen")
def api_screen(body: ScreenRequest) -> dict:
    """Read a style direction the way an image engine's classifier will.

    Deliberately free, stateless and attached to no project: it runs before the
    story is sent to Claude, which is the last point at which finding a problem
    costs nothing. Everything after it -- segmentation, then every scene prompt
    and every character portrait carrying the same style block -- is billed.

    It answers only for the text it is given. It is a heuristic, not the real
    classifier, so nothing it returns blocks anything: see screening.py.
    """
    text = security.clean(body.text, security.LIMIT_STYLE)
    findings = screening.screen(text)
    return {"findings": findings, "worst": screening.worst(findings)}


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
    activity.record(project["id"], "project.created", engine=project.get("engine"),
                    story_chars=len(body.story), scene_count=body.scene_count)
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
            # An engine with no reference sibling cannot hold a face steady, so
            # the mode follows the engine rather than sitting on claiming to.
            if not engines.supports_references(body.engine):
                project["consistency"] = config.CONSISTENCY_OFF
        if body.consistency is not None:
            if body.consistency not in config.CONSISTENCY_MODES:
                raise engines.ParamError(
                    f"Consistency: {body.consistency!r} is not one of "
                    f"{', '.join(config.CONSISTENCY_MODES)}")
            if (body.consistency == config.CONSISTENCY_CAST
                    and not engines.supports_references(project["engine"])):
                raise engines.ParamError(
                    f"{engines.engine(project['engine'])['name']} has no "
                    f"reference-image model, so it cannot hold characters "
                    f"consistent. Pick another engine or turn this off.")
            project["consistency"] = body.consistency
        if body.cast is not None:
            by_slug = {c.get("slug"): c for c in project.get("cast") or []}
            for patch in body.cast:
                member = by_slug.get(patch.slug)
                if member is not None and patch.description is not None:
                    member["description"] = patch.description.strip()
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
        with orchestrator.running_call(pid, orchestrator.KIND_SEGMENT) as call:
            result = compiler.segment(
                project.get("story", ""), count,
                style_hint=hint,
                api_key=config.anthropic_key(),
                engine_name=eng.get("name", project["engine"]),
                dialect_notes=eng.get("dialect", {}).get("notes", []),
                prompt_language=prompt_lang,
                pid=pid,
                progress=call.note,
                stop=call,
            )
    except compiler.Cancelled:
        raise _cancelled("Segmenting") from None
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
                "asset_refs": list(old.get("asset_refs") or []),
                "cast": list(s.get("cast") or []),
                "seed": old.get("seed"),
                "cost": old.get("cost"),
                "generation_id": old.get("generation_id"),
            })
        proj["scenes"] = scenes
        # A character whose description is unchanged keeps the portrait already
        # paid for; re-segmenting a story must not silently re-buy the cast.
        was = {c.get("slug"): c for c in proj.get("cast") or []}
        proj["cast"] = [
            {**member,
             **{k: v for k, v in was.get(member["slug"], {}).items()
                if k in ("asset", "asset_prompt", "version", "source_url",
                         "generation_id", "cost")}}
            for member in result.get("cast", [])
        ]
        proj["style_profile"] = result["style_profile"]
        proj["style_hint"] = hint
        proj["scene_count"] = len(scenes)
        proj["language"] = result["language"]
        proj["prompt_language"] = prompt_lang
        # Which model actually wrote these scenes. It is the requested one unless
        # that was overloaded and the fallback answered instead.
        proj["claude_model"] = result.get("model") or config.SEGMENT_MODEL
        proj["claude_fell_back"] = bool(result.get("fell_back"))

    updated = store.mutate(pid, apply)
    activity.record(pid, "story.segmented",
                    model=result.get("model"), fell_back=bool(result.get("fell_back")),
                    scenes=len(updated.get("scenes") or []),
                    cast=len(updated.get("cast") or []),
                    language=(updated.get("language") or {}).get("code"),
                    story_chars=len(project.get("story", "")))
    return _decorate(updated)


# --------------------------------------------------------------------------- #
# Step 3: preview cost, then spend
# --------------------------------------------------------------------------- #

@app.get("/api/projects/{pid}/claude/{phase}")
def api_claude_progress(pid: str, phase: str) -> dict:
    """What the Claude call for this phase is doing, while it is still doing it.

    The retry ladder runs inside the POST that started it, so this is the only
    way the page can tell a slow answer from four refusals and a wait. Cheap,
    read-only, and safe to poll: it reads a dict two threads share and nothing
    else.

    `retry_in` is computed here rather than published as a deadline, because the
    page should not have to agree with this machine about what time it is.
    """
    _load(pid)
    if phase not in (orchestrator.KIND_SEGMENT, orchestrator.KIND_NARRATION):
        raise HTTPException(404, f"No Claude phase called {phase!r}.")
    state = orchestrator.call_state(pid, phase)
    retry_at = state.pop("retry_at", None)
    if retry_at is not None:
        state["retry_in"] = max(0.0, round(retry_at - time.time(), 1))
    return state


@app.post("/api/projects/{pid}/segment/cancel")
def api_cancel_segment(pid: str) -> dict:
    """Stop a segmentation in flight. Safe to press when nothing is running."""
    _load(pid)
    return {"cancelling": orchestrator.cancel(pid, orchestrator.KIND_SEGMENT)}


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
    return _asset(store.images_dir(pid), name, "No such image")


@app.get("/api/projects/{pid}/cast/{name}")
def api_cast_image(pid: str, name: str):
    """A character's reference portrait. Its own route because anchors live
    outside images/ -- everything that reads images/ counts what it finds there
    as scenes."""
    _load(pid)
    return _asset(store.cast_dir(pid), name, "No such reference portrait")


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
        with orchestrator.running_call(pid, orchestrator.KIND_NARRATION) as call:
            lines = narration_mod.generate(
                project.get("story", ""), project.get("scenes", []),
                voice=settings.get("voice", ""),
                seconds_per_scene=int(settings.get("seconds_per_scene", 8)),
                language=project.get("language"),
                api_key=config.anthropic_key(),
                pid=pid,
                progress=call.note,
                stop=call,
            )
    except compiler.Cancelled:
        raise _cancelled("Writing the narration") from None
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

    activity.record(pid, "narration.written", lines=len(lines),
                    chars=sum(len(v) for v in lines.values()),
                    seconds_per_scene=int(settings.get("seconds_per_scene", 8)))
    return _decorate(store.mutate(pid, apply))


@app.post("/api/projects/{pid}/narration/cancel")
def api_cancel_narration(pid: str) -> dict:
    """Stop the narration script being written. The audio run has its own."""
    _load(pid)
    return {"cancelling": orchestrator.cancel(pid, orchestrator.KIND_NARRATION)}


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
    return _asset(store.audio_dir(pid), name, "No such audio")


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
    return _asset(store.video_dir(pid), name, "No such video")


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
    """Copy the work out, and say plainly which of it is out of date.

    Decorated deliberately: staleness is _decorate's answer and export reports
    it rather than working it out again. Export is the last place the project
    is still able to say "this picture is not the one this prompt would make
    now" -- after this the files are in an editor, cut into a video, and the
    only evidence left is that something looks wrong.

    It still exports. Stale work is often exactly what somebody wants -- the
    render they liked, kept deliberately -- and a blocked export would just be
    worked around. What it must not do is stay quiet.
    """
    project = _decorate(_load(pid))
    store.write_narration_files(pid, project)
    result = store.export(pid, project, flatten=body.flatten)
    stale = result.get("stale") or {}
    activity.record(pid, "export.written", flatten=body.flatten,
                    files=len(result.get("files") or []),
                    stale_images=len(stale.get("images") or []),
                    stale_audio=len(stale.get("audio") or []),
                    missing_images=len(stale.get("missing_images") or []),
                    dir=result.get("dir"))
    return result


@app.get("/api/projects/{pid}/manifest")
def api_manifest(pid: str) -> dict:
    _load(pid)
    return store.read_manifest(pid)


@app.get("/api/projects/{pid}/activity")
def api_activity(pid: str, limit: int = 200, run_id: str | None = None) -> dict:
    """The business activity log: what happened, and what it cost.

    The manifest next door answers "what exists"; this answers "what was spent,
    including on the things that do not exist". `report` is the reconciliation
    -- estimated against billed, and how much went on attempts that produced
    nothing.
    """
    _load(pid)
    limit = max(1, min(int(limit), 2000))
    return {
        "report": activity.report(pid),
        "events": activity.read(pid, limit=limit, run_id=run_id),
    }
