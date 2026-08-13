"""Render orchestration (FR-701..FR-705, FR-902).

One in-process job per project: a ThreadPoolExecutor of 3 workers, a stop event
that trips the moment the account is out of credit or rate limited, and
idempotent resume driven by the *stored prompt*, never by file timestamps.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import compiler, config, engines, narration, renderful, store, tts
from .naming import asset_stem, audio_stem

# Images and narration audio are separate runs over the same project, so they get
# separate job slots: cancelling a voice batch must not stop a render.
KIND_RENDER = "render"
KIND_AUDIO = "audio"


class Job:
    def __init__(self, pid: str, scene_numbers: list[int], kind: str = KIND_RENDER):
        self.pid = pid
        self.kind = kind
        self.scenes = list(scene_numbers)
        self.stop = threading.Event()
        self.fatal: str | None = None
        self.started_at = store.now()
        self.finished_at: str | None = None
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "kind": self.kind,
            "scenes": self.scenes,
            "fatal": self.fatal,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stopping": self.stop.is_set() and self.running,
        }


_jobs: dict[str, Job] = {}
_jobs_guard = threading.Lock()


def _slot(pid: str, kind: str) -> str:
    return f"{kind}:{pid}"


def job_for(pid: str, kind: str = KIND_RENDER) -> Job | None:
    with _jobs_guard:
        return _jobs.get(_slot(pid, kind))


def status(pid: str, kind: str = KIND_RENDER) -> dict | None:
    job = job_for(pid, kind)
    return job.as_dict() if job else None


def cancel(pid: str, kind: str = KIND_RENDER) -> bool:
    job = job_for(pid, kind)
    if job and job.running:
        job.stop.set()
        return True
    return False


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def compiled_prompt(project: dict, scene: dict) -> str:
    dialect = engines.engine(project["engine"]).get("dialect", {})
    return compiler.compile_prompt(scene.get("body", ""), project.get("style_profile", ""),
                                   dialect)


def plan(project: dict, scene_numbers: list[int] | None = None,
         force: bool = False) -> dict:
    """What a render would do, without doing it (FR-303 preview-before-spend)."""
    engine_key = project["engine"]
    params = engines.validate(engine_key, project.get("params"))
    model = engines.model_id(engine_key, params)

    wanted = set(scene_numbers) if scene_numbers else {s["n"] for s in project["scenes"]}
    to_render, skipped = [], []
    for scene in sorted(project["scenes"], key=lambda s: s["n"]):
        if scene["n"] not in wanted:
            continue
        prompt = compiled_prompt(project, scene)
        asset = scene.get("asset")
        on_disk = bool(asset) and (store.images_dir(project["id"]) / asset).is_file()
        unchanged = on_disk and scene.get("asset_prompt") == prompt
        if unchanged and not force:
            skipped.append({"n": scene["n"], "title": scene.get("title", ""),
                            "asset": asset, "reason_key": "current",
                            "reason": f"{asset} already matches this prompt"})
        else:
            key = "forced" if on_disk and force else "changed" if on_disk else "new"
            to_render.append({
                "n": scene["n"],
                "title": scene.get("title", ""),
                "prompt": prompt,
                "prompt_chars": len(prompt),
                # reason is the English sentence; reason_key is what the UI localises.
                "reason_key": key,
                "reason": {"forced": "re-render (forced)",
                           "changed": "prompt changed since last render",
                           "new": "not yet rendered"}[key],
            })

    price = engines.price_per_image(engine_key, params)
    return {
        "engine": engine_key,
        "model": model,
        "params": params,
        "render": to_render,
        "skip": skipped,
        "count": len(to_render),
        "price_per_image": price,
        "estimate": round(price * len(to_render), 4),
        "price_note": engines.engine(engine_key).get("price_note", ""),
        "warnings": engines.unconfirmed_values(engine_key, params),
        "verified_engine": bool(engines.engine(engine_key).get("verified")),
    }


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def _stopped_or_failed(job: "Job", error: Exception) -> dict:
    """A user cancel is not a failure: the scene simply did not run, so it goes
    back to pending and can be picked up again without `force`."""
    if job.stop.is_set() and job.fatal is None:
        return {"status": "pending", "detail": "cancelled"}
    return {"status": "failed", "detail": str(error)}


def _set_scene(pid: str, n: int, **fields) -> None:
    def apply(project):
        for scene in project["scenes"]:
            if scene["n"] == n:
                scene.update(fields)
                break
    store.mutate(pid, apply)


def _render_one(job: Job, project_snapshot: dict, scene_n: int, key: str,
                model: str, params: dict) -> None:
    pid = job.pid
    project = store.load(pid)
    scene = next((s for s in project["scenes"] if s["n"] == scene_n), None)
    if scene is None:
        return

    if job.stop.is_set():
        _set_scene(pid, scene_n, status="pending", detail="cancelled before submission")
        return

    prompt = compiled_prompt(project, scene)
    version = int(scene.get("version") or 1)
    if scene.get("asset"):
        version += 1

    _set_scene(pid, scene_n, status="rendering", detail="submitting", version=version)

    try:
        created = renderful.submit(prompt, key, model, params)
    except renderful.FatalAPIError as e:
        job.stop.set()
        job.fatal = str(e)
        _set_scene(pid, scene_n, status="failed", detail=str(e))
        return
    except Exception as e:  # noqa: BLE001 - one prompt's problem, keep the batch going
        _set_scene(pid, scene_n, **_stopped_or_failed(job, e))
        return

    gen_id = created.get("id")
    if not gen_id:
        _set_scene(pid, scene_n, status="failed",
                   detail=f"no generation id in response: {created}")
        return

    _set_scene(pid, scene_n, status="rendering", detail=f"queued ({gen_id})",
               generation_id=gen_id)

    try:
        status_doc = renderful.wait_for(
            gen_id, key,
            should_stop=job.stop.is_set,
            on_state=lambda st: _set_scene(pid, scene_n, detail=str(st)),
        )
    except renderful.FatalAPIError as e:
        job.stop.set()
        job.fatal = str(e)
        _set_scene(pid, scene_n, status="failed", detail=str(e))
        return
    except Exception as e:  # noqa: BLE001
        _set_scene(pid, scene_n, **_stopped_or_failed(job, e))
        return

    urls = renderful.outputs_of(status_doc)
    if not urls:
        _set_scene(pid, scene_n, status="failed",
                   detail=f"completed with no outputs: {status_doc}")
        return

    try:
        data = renderful.download(urls[0])
    except Exception as e:  # noqa: BLE001
        _set_scene(pid, scene_n, status="failed", detail=f"download: {e}")
        return

    stem = asset_stem(pid, scene_n, scene["slug"], version, params.get("seed"))
    dest = store.images_dir(pid) / stem
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = renderful.save_delivered(data, dest, convert_png=False)

    cost = status_doc.get("cost", created.get("cost"))
    store.record_asset(pid, store.manifest_entry(
        pid, {**scene, "version": version}, project,
        engine_model=model, prompt=prompt, params=params,
        generation_id=gen_id, file_name=written.name, cost=cost,
        source_url=urls[0],
    ))

    def apply(proj):
        for s in proj["scenes"]:
            if s["n"] == scene_n:
                s.update({
                    "status": "done",
                    "detail": "",
                    "asset": written.name,
                    "asset_prompt": prompt,
                    "version": version,
                    "seed": params.get("seed"),
                    "cost": cost,
                    "generation_id": gen_id,
                })
                break
        spend = proj.setdefault("spend", {"images": 0, "actual": 0.0})
        spend["images"] = int(spend.get("images", 0)) + 1
        try:
            spend["actual"] = round(float(spend.get("actual", 0.0)) + float(cost or 0.0), 4)
        except (TypeError, ValueError):
            pass
    store.mutate(pid, apply)


def _run(job: Job, key: str, model: str, params: dict, snapshot: dict) -> None:
    try:
        with ThreadPoolExecutor(max_workers=config.WORKERS) as pool:
            futures = [
                pool.submit(_render_one, job, snapshot, n, key, model, params)
                for n in job.scenes
            ]
            for f in futures:
                try:
                    f.result()
                except Exception:  # noqa: BLE001 - already recorded per scene
                    pass
    finally:
        job.finished_at = store.now()
        # Anything still queued when a fatal error tripped goes back to pending.
        def apply(project):
            for scene in project["scenes"]:
                if scene["n"] in job.scenes and scene.get("status") in ("queued", "rendering"):
                    scene["status"] = "pending"
                    scene["detail"] = job.fatal or "stopped"
        store.mutate(job.pid, apply)


def start(pid: str, scene_numbers: list[int] | None = None, force: bool = False) -> dict:
    """Validate, plan, then spend. Raises before any submission if anything is off."""
    existing = job_for(pid)
    if existing and existing.running:
        raise RuntimeError("A render is already running for this project.")

    project = store.load(pid)
    if not project.get("scenes"):
        raise RuntimeError("Segment the story into scenes first.")

    key = config.renderful_key()
    if not key:
        raise RuntimeError(
            "No Renderful API key. Set RENDERFUL_API_KEY or put the key in api_key.txt."
        )

    the_plan = plan(project, scene_numbers, force)
    if not the_plan["render"]:
        return {"started": False, "plan": the_plan,
                "message": "Nothing to render â€” every selected scene already matches its prompt."}

    params = the_plan["params"]
    model = the_plan["model"]
    numbers = [s["n"] for s in the_plan["render"]]

    def apply(proj):
        for scene in proj["scenes"]:
            if scene["n"] in numbers:
                scene["status"] = "queued"
                scene["detail"] = "waiting for a worker"
    store.mutate(pid, apply)

    job = Job(pid, numbers, KIND_RENDER)
    with _jobs_guard:
        _jobs[_slot(pid, KIND_RENDER)] = job
    job.thread = threading.Thread(
        target=_run, args=(job, key, model, params, project),
        name=f"render-{pid}", daemon=True,
    )
    job.thread.start()
    return {"started": True, "plan": the_plan, "job": job.as_dict()}


# --------------------------------------------------------------------------- #
# Narration audio
# --------------------------------------------------------------------------- #

def voice_settings(project: dict) -> tuple[str, dict]:
    """The voice engine and validated params for this project.

    Blank language code follows the story's own language, which is the whole
    point of segmenting it in the story's language in the first place.
    """
    settings = project.get("narration") or {}
    voice_key = settings.get("voice_engine") or engines.default_voice_key()
    params = engines.validate(voice_key, settings.get("voice_params"),
                              engines.SECTION_VOICES)
    if "language_code" in params and not params["language_code"]:
        params["language_code"] = (project.get("language") or {}).get("code", "")
    return voice_key, params


def plan_audio(project: dict, scene_numbers: list[int] | None = None,
               force: bool = False) -> dict:
    """What a voice run would speak, and what it would cost, without spending."""
    pid = project["id"]
    voice_key, params = voice_settings(project)
    model = engines.model_id(voice_key, params)

    wanted = set(scene_numbers) if scene_numbers else {s["n"] for s in project["scenes"]}
    to_speak, skipped, missing = [], [], []
    for scene in sorted(project["scenes"], key=lambda s: s["n"]):
        if scene["n"] not in wanted:
            continue
        text = (scene.get("narration") or "").strip()
        if not text:
            missing.append({"n": scene["n"], "title": scene.get("title", "")})
            continue

        spoken = scene.get("audio")
        on_disk = bool(spoken) and (store.audio_dir(pid) / spoken).is_file()
        unchanged = on_disk and scene.get("audio_text") == text
        if unchanged and not force:
            skipped.append({"n": scene["n"], "title": scene.get("title", ""),
                            "audio": spoken, "reason_key": "current",
                            "reason": f"{spoken} already matches this line"})
            continue

        key = "forced" if on_disk and force else "changed" if on_disk else "new"
        to_speak.append({
            "n": scene["n"],
            "title": scene.get("title", ""),
            "chars": len(text),
            "estimate": round(engines.price_for_text(voice_key, text), 4),
            "estimated_seconds": narration.estimate_seconds(text),
            "reason_key": key,
            "reason": {"forced": "re-synthesise (forced)",
                       "changed": "narration edited since it was spoken",
                       "new": "not yet spoken"}[key],
        })

    voice_entry = engines.voice(voice_key)
    # Totalled from the raw character count, not by adding up the per-line
    # figures: those are rounded for display, and the error compounds per line.
    total_chars = sum(row["chars"] for row in to_speak)
    return {
        "voice_engine": voice_key,
        "model": model,
        "params": params,
        "speak": to_speak,
        "skip": skipped,
        "missing_narration": missing,
        "count": len(to_speak),
        "chars": total_chars,
        "price_per_1k_chars": engines.price_per_1k_chars(voice_key),
        "estimate": round(
            engines.price_per_1k_chars(voice_key) * total_chars
            / engines.CHARS_PER_PRICE_UNIT, 4),
        "price_note": voice_entry.get("price_note", ""),
        "notes": voice_entry.get("notes", []),
        "warnings": engines.unconfirmed_values(voice_key, params, engines.SECTION_VOICES),
        "verified_engine": bool(voice_entry.get("verified")),
    }


def _speak_one(job: Job, scene_n: int, key: str, voice_key: str, model: str,
               params: dict) -> None:
    pid = job.pid
    project = store.load(pid)
    scene = next((s for s in project["scenes"] if s["n"] == scene_n), None)
    if scene is None:
        return

    text = (scene.get("narration") or "").strip()
    if not text:
        _set_scene(pid, scene_n, audio_status="failed",
                   audio_detail="no narration line to speak")
        return

    if job.stop.is_set():
        _set_scene(pid, scene_n, audio_status="pending",
                   audio_detail="cancelled before submission")
        return

    _set_scene(pid, scene_n, audio_status="speaking", audio_detail="submitting")

    try:
        speech = tts.synthesize(
            text, key, model, params,
            should_stop=job.stop.is_set,
            on_state=lambda st: _set_scene(pid, scene_n, audio_detail=str(st)),
        )
    except renderful.FatalAPIError as e:
        job.stop.set()
        job.fatal = str(e)
        _set_scene(pid, scene_n, audio_status="failed", audio_detail=str(e))
        return
    except Exception as e:  # noqa: BLE001 - one line's problem, keep the batch going
        state = _stopped_or_failed(job, e)
        _set_scene(pid, scene_n, audio_status=state["status"],
                   audio_detail=state["detail"])
        return

    dest = store.audio_dir(pid) / audio_stem(pid, scene_n, scene["slug"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = dest.with_suffix("." + speech.extension)
    written.write_bytes(speech.data)

    # A container we cannot parse still produced usable audio; fall back to the
    # estimate rather than losing the line.
    seconds = speech.seconds if speech.measured else narration.estimate_seconds(text)

    store.record_asset(pid, store.narration_manifest_entry(
        pid, scene, project, voice_key=voice_key, model=model, text=text,
        params=params, generation_id=speech.generation_id, file_name=written.name,
        cost=speech.cost, seconds=seconds, measured=speech.measured,
        source_url=speech.source_url,
    ))

    def apply(proj):
        for s in proj["scenes"]:
            if s["n"] == scene_n:
                s.update({
                    "audio_status": "done",
                    "audio_detail": "",
                    "audio": written.name,
                    "audio_text": text,
                    "audio_seconds": seconds,
                    "audio_measured": speech.measured,
                    "audio_cost": speech.cost,
                    "audio_generation_id": speech.generation_id,
                })
                break
        spend = proj.setdefault("spend", {"images": 0, "lines": 0, "actual": 0.0})
        spend["lines"] = int(spend.get("lines", 0)) + 1
        try:
            spend["actual"] = round(
                float(spend.get("actual", 0.0)) + float(speech.cost or 0.0), 4)
        except (TypeError, ValueError):
            pass
    store.mutate(pid, apply)


def _run_audio(job: Job, key: str, voice_key: str, model: str, params: dict) -> None:
    try:
        with ThreadPoolExecutor(max_workers=config.WORKERS) as pool:
            futures = [
                pool.submit(_speak_one, job, n, key, voice_key, model, params)
                for n in job.scenes
            ]
            for f in futures:
                try:
                    f.result()
                except Exception:  # noqa: BLE001 - already recorded per scene
                    pass
    finally:
        job.finished_at = store.now()
        def apply(project):
            for scene in project["scenes"]:
                if (scene["n"] in job.scenes
                        and scene.get("audio_status") in ("queued", "speaking")):
                    scene["audio_status"] = "pending"
                    scene["audio_detail"] = job.fatal or "stopped"
        store.mutate(job.pid, apply)


def start_audio(pid: str, scene_numbers: list[int] | None = None,
                force: bool = False) -> dict:
    """Validate, plan, then spend -- the same contract as start()."""
    existing = job_for(pid, KIND_AUDIO)
    if existing and existing.running:
        raise RuntimeError("A narration audio run is already going for this project.")

    project = store.load(pid)
    if not project.get("scenes"):
        raise RuntimeError("Segment the story into scenes first.")

    key = config.renderful_key()
    if not key:
        raise RuntimeError(
            "No Renderful API key. Set RENDERFUL_API_KEY or put the key in api_key.txt."
        )

    the_plan = plan_audio(project, scene_numbers, force)
    if not the_plan["speak"]:
        return {"started": False, "plan": the_plan,
                "message": "Nothing to speak - every selected line already has audio."}

    numbers = [row["n"] for row in the_plan["speak"]]

    def apply(proj):
        for scene in proj["scenes"]:
            if scene["n"] in numbers:
                scene["audio_status"] = "queued"
                scene["audio_detail"] = "waiting for a worker"
    store.mutate(pid, apply)

    job = Job(pid, numbers, KIND_AUDIO)
    with _jobs_guard:
        _jobs[_slot(pid, KIND_AUDIO)] = job
    job.thread = threading.Thread(
        target=_run_audio,
        args=(job, key, the_plan["voice_engine"], the_plan["model"], the_plan["params"]),
        name=f"audio-{pid}", daemon=True,
    )
    job.thread.start()
    return {"started": True, "plan": the_plan, "job": job.as_dict()}
