"""Render orchestration (FR-701..FR-705, FR-902).

One in-process job per project: a ThreadPoolExecutor of 3 workers, a stop event
that trips the moment the account is out of credit or rate limited, and
idempotent resume driven by the *stored prompt*, never by file timestamps.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import compiler, config, engines, renderful, store
from .naming import asset_stem


class Job:
    def __init__(self, pid: str, scene_numbers: list[int]):
        self.pid = pid
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
            "scenes": self.scenes,
            "fatal": self.fatal,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stopping": self.stop.is_set() and self.running,
        }


_jobs: dict[str, Job] = {}
_jobs_guard = threading.Lock()


def job_for(pid: str) -> Job | None:
    with _jobs_guard:
        return _jobs.get(pid)


def status(pid: str) -> dict | None:
    job = job_for(pid)
    return job.as_dict() if job else None


def cancel(pid: str) -> bool:
    job = job_for(pid)
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

    job = Job(pid, numbers)
    with _jobs_guard:
        _jobs[pid] = job
    job.thread = threading.Thread(
        target=_run, args=(job, key, model, params, project),
        name=f"render-{pid}", daemon=True,
    )
    job.thread.start()
    return {"started": True, "plan": the_plan, "job": job.as_dict()}
