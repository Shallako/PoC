"""Render orchestration (FR-701..FR-705, FR-902).

One in-process job per project: a ThreadPoolExecutor of 3 workers, a stop event
that trips the moment the account is out of credit or rate limited, and
idempotent resume driven by the *stored prompt*, never by file timestamps.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from . import (captions, compiler, config, engines, narration, renderful, store,
               timeline, tts, video)
from .naming import (anchor_stem, asset_stem, audio_stem, flat_stem,
                     video_stem)

# Images, narration audio and the video cut are separate runs over the same
# project, so they get separate job slots: cancelling a voice batch must not stop
# a render, and assembling a cut must not stop either.
KIND_RENDER = "render"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"


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


# --------------------------------------------------------------------------- #
# The other shape of work
#
# A render, a voice batch and a video assembly are background jobs with a thread
# each, so cancelling one is setting that thread's stop event. Segmentation and
# the narration script are not: they are a single Claude call made inside the
# request that asked for them, and there is no thread of ours to stop.
#
# What there is, is a call that can take minutes -- the retry ladder waits up to
# twenty seconds between four attempts before it even reaches the fallback model
# -- with the user watching a disabled button. So an in-flight call registers an
# event here, the cancel endpoint sets it from a second request, and the call
# checks it between attempts and between streamed chunks.
# --------------------------------------------------------------------------- #

KIND_SEGMENT = "segment"
KIND_NARRATION = "narration"

# A set per slot rather than one event, because nothing stops two segmentations
# for the same project overlapping -- the button is disabled in the page, which
# is not the same as being impossible. Cancelling a phase stops every call in
# flight for it, and each call removes only its own event on the way out.
_calls: dict[str, set[threading.Event]] = {}
_calls_guard = threading.Lock()


@contextmanager
def running_call(pid: str, kind: str):
    """Register an in-flight Claude call so another request can stop it."""
    slot = _slot(pid, kind)
    event = threading.Event()
    with _calls_guard:
        _calls.setdefault(slot, set()).add(event)
    try:
        yield event
    finally:
        with _calls_guard:
            live = _calls.get(slot)
            if live is not None:
                live.discard(event)
                if not live:
                    del _calls[slot]


def call_running(pid: str, kind: str) -> bool:
    with _calls_guard:
        return bool(_calls.get(_slot(pid, kind)))


def cancel(pid: str, kind: str = KIND_RENDER) -> bool:
    """Stop whatever is running for this project under `kind`.

    One entry point for both shapes of work, so every phase's cancel endpoint is
    the same two lines and a new phase cannot accidentally get a different
    contract from the others.
    """
    job = job_for(pid, kind)
    if job and job.running:
        job.stop.set()
        return True
    with _calls_guard:
        events = list(_calls.get(_slot(pid, kind)) or ())
    stopped = False
    for event in events:
        if not event.is_set():
            event.set()
            stopped = True
    return stopped


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def compiled_prompt(project: dict, scene: dict) -> str:
    dialect = engines.engine(project["engine"]).get("dialect", {})
    return compiler.compile_prompt(scene.get("body", ""), project.get("style_profile", ""),
                                   dialect)


# --------------------------------------------------------------------------- #
# Character consistency
#
# One reference portrait per recurring character, rendered before the scenes and
# passed to every scene that character appears in.
#
# The identity of a scene's asset therefore stops being its prompt alone and
# becomes (prompt, the anchors it was rendered against). Everything below exists
# to make that second half explicit, because the failure it prevents is silent:
# re-render an anchor without restaging its scenes and eleven pictures keep a
# face the twelfth no longer has, with nothing on screen to say why.
# --------------------------------------------------------------------------- #

def cast_of(project: dict) -> list[dict]:
    return [c for c in project.get("cast") or [] if c.get("name")]


def consistency_on(project: dict) -> bool:
    """References requested, supported by the engine, and something to anchor."""
    return (project.get("consistency", config.CONSISTENCY_OFF) == config.CONSISTENCY_CAST
            and engines.supports_references(project["engine"])
            and bool(cast_of(project)))


def anchor_prompt(project: dict, member: dict) -> str:
    dialect = engines.engine(project["engine"]).get("dialect", {})
    return compiler.compile_anchor_prompt(member.get("description", ""),
                                          project.get("style_profile", ""), dialect)


def anchor_params(project: dict, params: dict) -> dict:
    """Scene params, re-framed for a reference portrait.

    A portrait is square whatever the story is: the anchor is never shown, and a
    16:9 reference of one standing figure is mostly background -- which is the
    part of a reference we least want the model to learn.
    """
    out = dict(params)
    if "aspect_ratio" in out:
        out["aspect_ratio"] = config.ANCHOR_ASPECT_RATIO
    return out


def _token(slug: str, version: int) -> str:
    """What a scene records about one anchor it was rendered against.

    A version, not a URL. The URL is not known until the anchor has rendered, so
    a URL could never be compared at planning time -- which is exactly when a
    scene has to be told it is stale.
    """
    return f"{slug}@{version}"


def _slug_of(token: str) -> str:
    """The character a scene's reference token names. The inverse of _token();
    a slug is [a-z0-9-] so the separator can never appear inside one."""
    return token.rsplit("@", 1)[0]


def plan_cast(project: dict, force: bool = False) -> dict:
    """Which anchors need rendering, and what version each character will be at.

    Returns {'render': [...], 'skip': [...], 'versions': {slug: version}}. The
    versions map is what scene planning compares against, so it covers every
    character -- the ones being re-rendered at their *next* version, and the
    settled ones at their current one.
    """
    pid = project["id"]
    to_render, skipped, versions = [], [], {}

    for member in cast_of(project):
        slug = member["slug"]
        prompt = anchor_prompt(project, member)
        asset = member.get("asset")
        on_disk = bool(asset) and (store.cast_dir(pid) / asset).is_file()
        current = int(member.get("version") or 0)
        unchanged = on_disk and member.get("asset_prompt") == prompt and bool(
            member.get("source_url"))

        if unchanged and not force:
            versions[slug] = current
            skipped.append({"name": member["name"], "slug": slug, "asset": asset,
                            "reason_key": "current",
                            "reason": f"{asset} already matches this description"})
        else:
            versions[slug] = current + 1
            key = ("forced" if on_disk and force
                   else "changed" if on_disk else "new")
            to_render.append({
                "name": member["name"], "slug": slug, "prompt": prompt,
                "prompt_chars": len(prompt), "reason_key": key,
                "reason": {"forced": "re-render (forced)",
                           "changed": "description changed since last render",
                           "new": "no reference portrait yet"}[key],
            })
    return {"render": to_render, "skip": skipped, "versions": versions}


def scene_tokens(project: dict, scene: dict, versions: dict) -> list[str]:
    """The anchors this scene should be rendered against, in cast order.

    Cast order rather than the order the scene happens to list its characters,
    so two scenes with the same people always produce the same fingerprint and
    neither is re-rendered for a reordering that changes nothing.
    """
    named = set(scene.get("cast") or [])
    cap = engines.max_references(project["engine"])
    tokens = [_token(m["slug"], versions[m["slug"]])
              for m in cast_of(project)
              if m["name"] in named and m["slug"] in versions]
    return tokens[:cap]


def plan(project: dict, scene_numbers: list[int] | None = None,
         force: bool = False) -> dict:
    """What a render would do, without doing it (FR-303 preview-before-spend)."""
    engine_key = project["engine"]
    params = engines.validate(engine_key, project.get("params"))
    model = engines.model_id(engine_key, params)

    using_cast = consistency_on(project)
    cast_plan = (plan_cast(project, force) if using_cast
                 else {"render": [], "skip": [], "versions": {}})
    versions = cast_plan["versions"]

    wanted = set(scene_numbers) if scene_numbers else {s["n"] for s in project["scenes"]}
    to_render, skipped = [], []
    for scene in sorted(project["scenes"], key=lambda s: s["n"]):
        if scene["n"] not in wanted:
            continue
        prompt = compiled_prompt(project, scene)
        tokens = scene_tokens(project, scene, versions) if using_cast else []
        asset = scene.get("asset")
        on_disk = bool(asset) and (store.images_dir(project["id"]) / asset).is_file()
        # Two halves of one identity. A scene is current only if the prompt it
        # would be sent matches the one it was rendered with *and* the anchors
        # behind it are the same anchors -- a re-rendered anchor makes every
        # scene that references it stale, which is the whole dependency.
        same_prompt = scene.get("asset_prompt") == prompt
        same_refs = list(scene.get("asset_refs") or []) == tokens
        if on_disk and same_prompt and same_refs and not force:
            skipped.append({"n": scene["n"], "title": scene.get("title", ""),
                            "asset": asset, "reason_key": "current",
                            "reason": f"{asset} already matches this prompt"})
        else:
            key = ("forced" if on_disk and force
                   else "restaged" if on_disk and same_prompt and not same_refs
                   else "changed" if on_disk else "new")
            to_render.append({
                "n": scene["n"],
                "title": scene.get("title", ""),
                "prompt": prompt,
                "prompt_chars": len(prompt),
                "cast": list(scene.get("cast") or []),
                "references": tokens,
                # reason is the English sentence; reason_key is what the UI localises.
                "reason_key": key,
                "reason": {"forced": "re-render (forced)",
                           "restaged": "a character's reference portrait changed",
                           "changed": "prompt changed since last render",
                           "new": "not yet rendered"}[key],
            })

    price = engines.price_per_image(engine_key, params)
    anchors = cast_plan["render"]
    billable = len(to_render) + len(anchors)
    return {
        "engine": engine_key,
        "model": model,
        "params": params,
        "render": to_render,
        "skip": skipped,
        "count": len(to_render),
        # Anchors are billed images too, so they are priced here rather than
        # appearing as an unexplained difference on the invoice.
        "consistency": project.get("consistency", config.CONSISTENCY_OFF),
        "consistency_active": using_cast,
        "ref_model": engines.ref_model(engine_key) if using_cast else "",
        "anchors": anchors,
        "anchors_skip": cast_plan["skip"],
        "anchor_count": len(anchors),
        "price_per_image": price,
        "estimate": round(price * billable, 4),
        "price_note": engines.engine(engine_key).get("price_note", ""),
        "warnings": engines.unconfirmed_values(engine_key, params),
        "verified_engine": bool(engines.engine(engine_key).get("verified")),
        "verified_references": bool(
            (engines.ref_spec(engine_key) or {}).get("verified")) if using_cast else True,
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


def _set_member(pid: str, slug: str, **fields) -> None:
    def apply(project):
        for member in project.get("cast") or []:
            if member.get("slug") == slug:
                member.update(fields)
                break
    store.mutate(pid, apply)


def _render_anchor(job: Job, slug: str, key: str, model: str, params: dict) -> None:
    """Render one character's reference portrait.

    A failed anchor is not fatal to the run: the scenes that would have used it
    fall back to text-to-image and say so, because a story rendered without one
    character's face pinned is worth more than no story at all.
    """
    pid = job.pid
    project = store.load(pid)
    member = next((m for m in cast_of(project) if m["slug"] == slug), None)
    if member is None or job.stop.is_set():
        return

    prompt = anchor_prompt(project, member)
    version = int(member.get("version") or 0) + 1
    shot = anchor_params(project, params)
    # The version is deliberately NOT stored yet. It is half of the token a
    # scene records, and source_url is the other half: bumping it here would
    # leave a failed re-render advertising the new version against the old
    # portrait's URL, and the scenes would reference last week's face while
    # recording that they used this week's.
    _set_member(pid, slug, status="rendering", detail="submitting")

    try:
        created = renderful.submit(prompt, key, model, shot)
        gen_id = created.get("id")
        if not gen_id:
            raise RuntimeError(f"no generation id in response: {created}")
        _set_member(pid, slug, detail=f"queued ({gen_id})")
        status_doc = renderful.wait_for(
            gen_id, key, should_stop=job.stop.is_set,
            on_state=lambda st: _set_member(pid, slug, detail=str(st)))
    except renderful.FatalAPIError as e:
        job.stop.set()
        job.fatal = str(e)
        _set_member(pid, slug, status="failed", detail=str(e))
        return
    except Exception as e:  # noqa: BLE001 - one character's problem
        _set_member(pid, slug, **_stopped_or_failed(job, e))
        return

    urls = renderful.outputs_of(status_doc)
    if not urls:
        _set_member(pid, slug, status="failed", detail="completed with no outputs")
        return
    try:
        data = renderful.download(urls[0])
    except Exception as e:  # noqa: BLE001
        _set_member(pid, slug, status="failed", detail=f"download: {e}")
        return

    dest = store.cast_dir(pid) / anchor_stem(pid, slug, version)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = renderful.save_delivered(data, dest, convert_png=False)
    cost = status_doc.get("cost", created.get("cost"))

    store.record_asset(pid, store.anchor_manifest_entry(
        pid, {**member, "version": version}, project, engine_model=model,
        prompt=prompt, params=shot, generation_id=gen_id,
        file_name=written.name, cost=cost, source_url=urls[0]))

    _set_member(pid, slug, status="done", detail="", asset=written.name,
                asset_prompt=prompt, version=version, cost=cost,
                generation_id=gen_id,
                # The URL is the point of the whole exercise: it is what a scene
                # render sends as its reference, and it is Renderful's own CDN,
                # so nothing of ours has to be reachable from the internet.
                source_url=urls[0])

    def apply(proj):
        spend = proj.setdefault("spend", {"images": 0, "actual": 0.0})
        spend["images"] = int(spend.get("images", 0)) + 1
        try:
            spend["actual"] = round(float(spend.get("actual", 0.0)) + float(cost or 0.0), 4)
        except (TypeError, ValueError):
            pass
    store.mutate(pid, apply)


def references_for(project: dict, tokens: list[str]) -> tuple[list[str], list[str]]:
    """(urls to send, tokens actually satisfied).

    A token whose anchor failed or is missing a URL is dropped from both, so a
    scene records what it was really rendered against rather than what was
    intended -- otherwise a failed anchor would look settled forever and never
    re-stage its scenes once it worked.
    """
    by_token = {}
    for member in cast_of(project):
        url = member.get("source_url")
        if url:
            by_token[_token(member["slug"], int(member.get("version") or 0))] = url
    urls, kept = [], []
    for token in tokens:
        url = by_token.get(token)
        if url:
            urls.append(url)
            kept.append(token)
    return urls, kept


def _render_one(job: Job, project_snapshot: dict, scene_n: int, key: str,
                model: str, params: dict, tokens: list[str] | None = None) -> None:
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

    # Resolved here rather than at planning time: the anchors this scene depends
    # on may have rendered seconds ago, in this same run.
    urls, used_tokens = references_for(project, tokens or [])
    send_model, gen_type = model, renderful.GEN_TYPE_IMAGE
    if urls:
        send_model = engines.ref_model(project["engine"])
        gen_type = renderful.GEN_TYPE_IMAGE_REF

    _set_scene(pid, scene_n, status="rendering", detail="submitting", version=version)

    try:
        created = renderful.submit(prompt, key, send_model, params,
                                   gen_type=gen_type, references=urls)
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

    out_urls = renderful.outputs_of(status_doc)
    if not out_urls:
        _set_scene(pid, scene_n, status="failed",
                   detail=f"completed with no outputs: {status_doc}")
        return

    try:
        data = renderful.download(out_urls[0])
    except Exception as e:  # noqa: BLE001
        _set_scene(pid, scene_n, status="failed", detail=f"download: {e}")
        return

    stem = asset_stem(pid, scene_n, scene["slug"], version, params.get("seed"))
    dest = store.images_dir(pid) / stem
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = renderful.save_delivered(data, dest, convert_png=False)

    cost = status_doc.get("cost", created.get("cost"))
    entry = store.manifest_entry(
        pid, {**scene, "version": version}, project,
        engine_model=send_model, prompt=prompt, params=params,
        generation_id=gen_id, file_name=written.name, cost=cost,
        source_url=out_urls[0],
    )
    # Provenance: which pictures this picture was conditioned on. The tokens are
    # what staleness compares; the URLs are what was actually sent.
    entry["references"] = list(urls)
    entry["reference_tokens"] = list(used_tokens)
    store.record_asset(pid, entry)

    def apply(proj):
        for s in proj["scenes"]:
            if s["n"] == scene_n:
                s.update({
                    "status": "done",
                    "detail": "",
                    "asset": written.name,
                    "asset_prompt": prompt,
                    "asset_refs": list(used_tokens),
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


def _run(job: Job, key: str, model: str, params: dict, snapshot: dict,
         anchors: list[str] | None = None,
         tokens_by_scene: dict[int, list[str]] | None = None) -> None:
    tokens_by_scene = tokens_by_scene or {}
    building = set(anchors or ())
    try:
        with ThreadPoolExecutor(max_workers=config.WORKERS) as pool:
            # A scene may not be submitted before the portraits it references
            # exist: it would render the wrong face and bill for it. But it only
            # depends on *its own* characters. Waiting for the whole cast made a
            # landscape scene queue behind six portraits it never uses, and with
            # MAX_CAST at six that is two full rounds of workers sitting idle.
            #
            # So each scene waits for exactly what it names, and the waiting is
            # done here rather than inside a worker -- a worker that blocked on
            # another worker could deadlock the pool it is holding a slot in.
            anchor_futures = {
                pool.submit(_render_anchor, job, slug, key, model, params): slug
                for slug in building
            }
            # Only anchors being rendered *now* can be waited for. One that was
            # skipped as already current has its portrait on disk already.
            waiting = {n: {_slug_of(t) for t in tokens_by_scene.get(n, [])} & building
                       for n in job.scenes}
            futures = []

            def start_ready() -> None:
                for n in [n for n, needs in waiting.items() if not needs]:
                    del waiting[n]
                    futures.append(pool.submit(_render_one, job, snapshot, n, key,
                                               model, params, tokens_by_scene.get(n, [])))

            start_ready()                       # scenes with nobody in them go now
            for done in as_completed(anchor_futures):
                slug = anchor_futures[done]
                try:
                    done.result()
                except Exception:  # noqa: BLE001 - recorded per character
                    pass
                # Released whether the portrait worked or not: a failed anchor is
                # dropped by references_for and the scene falls back to
                # text-to-image, which is better than never rendering it.
                for needs in waiting.values():
                    needs.discard(slug)
                start_ready()

            start_ready()                       # belt and braces: nothing left behind
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
    if not the_plan["render"] and not the_plan["anchors"]:
        return {"started": False, "plan": the_plan,
                "message": "Nothing to render â€” every selected scene already matches its prompt."}

    params = the_plan["params"]
    model = the_plan["model"]
    numbers = [s["n"] for s in the_plan["render"]]
    anchors = [a["slug"] for a in the_plan["anchors"]]
    tokens_by_scene = {s["n"]: s.get("references") or [] for s in the_plan["render"]}

    def apply(proj):
        for scene in proj["scenes"]:
            if scene["n"] in numbers:
                scene["status"] = "queued"
                scene["detail"] = ("waiting for a character portrait" if anchors
                                   else "waiting for a worker")
        for member in proj.get("cast") or []:
            if member.get("slug") in anchors:
                member["status"] = "queued"
                member["detail"] = "waiting for a worker"
    store.mutate(pid, apply)

    job = Job(pid, numbers, KIND_RENDER)
    with _jobs_guard:
        _jobs[_slot(pid, KIND_RENDER)] = job
    job.thread = threading.Thread(
        target=_run,
        args=(job, key, model, params, project, anchors, tokens_by_scene),
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


# --------------------------------------------------------------------------- #
# Video assembly
# --------------------------------------------------------------------------- #

def plan_video(project: dict) -> dict:
    """The cut this project would produce, without producing it.

    Costs nothing to ask, so unlike the render and speak plans there is no spend
    to confirm -- what this preview is for is telling you the runtime is a guess
    before you sit through an encode of one.
    """
    settings = store.video_settings(project)
    beats, skipped = timeline.build(project, settings)
    cues = captions.build(beats)
    width, height = timeline.canvas(project, settings["aspect"])

    return {
        "profile": (project.get("video") or {}).get("profile")
                   or engines.default_video_key(),
        "params": settings,
        "scenes": [
            {"n": b.n, "title": b.title, "start": b.start, "duration": b.duration,
             "speech_seconds": b.speech_seconds, "measured": b.measured,
             "has_audio": b.audio is not None}
            for b in beats
        ],
        "skip": skipped,
        "count": len(beats),
        "cues": len(cues),
        "width": width,
        "height": height,
        "runtime_seconds": timeline.total_seconds(beats),
        "runtime_measured": timeline.all_measured(beats),
        "silent_scenes": sum(1 for b in beats if b.audio is None),
        "ffmpeg": {"available": video.available(), "version": video.version(),
                   "hint": video.install_hint()},
    }


def _assemble(job: Job, project: dict, settings: dict) -> None:
    """Encode one segment per scene, join them, then attach the captions."""
    pid = job.pid
    vdir = store.video_dir(pid)
    vdir.mkdir(parents=True, exist_ok=True)

    beats, _ = timeline.build(project, settings)
    width, height = timeline.canvas(project, settings["aspect"])
    fps = int(settings["fps"])

    segments: list[Path] = []
    for beat in beats:
        if job.stop.is_set():
            _set_scene(pid, beat.n, video_status="pending",
                       video_detail="cancelled before encoding")
            continue
        _set_scene(pid, beat.n, video_status="encoding", video_detail="")
        dest = vdir / f"{flat_stem(pid, beat.n, beat.slug)}_seg{store.VIDEO_SUFFIX}"
        try:
            video.encode_segment(
                beat, dest, width=width, height=height, fps=fps,
                motion=settings["motion"], lead_in=float(settings["lead_in"]),
            )
        except video.FfmpegMissing as e:
            job.stop.set()
            job.fatal = str(e)
            _set_scene(pid, beat.n, video_status="failed", video_detail=str(e))
            return
        except Exception as e:  # noqa: BLE001 - one scene's problem
            _set_scene(pid, beat.n, video_status="failed", video_detail=str(e))
            continue
        segments.append(dest)
        _set_scene(pid, beat.n, video_status="done", video_detail="",
                   video_segment=dest.name, video_seconds=beat.duration)

    if job.stop.is_set() or not segments:
        job.fatal = job.fatal or "no scene segments were produced"
        return

    joined = vdir / f"{video_stem(pid)}_joined{store.VIDEO_SUFFIX}"
    final = vdir / f"{video_stem(pid)}{store.VIDEO_SUFFIX}"
    video.join(segments, joined, workdir=vdir)

    mode = settings["subtitles"]
    cues = captions.build(beats)
    if mode != video.SUBTITLES_NONE and cues:
        srt = vdir / f"{video_stem(pid)}{store.CAPTIONS_SUFFIX}"
        srt.write_text(captions.to_srt(cues), encoding="utf-8")
        video.add_subtitles(joined, srt, final, mode=mode, workdir=vdir, fps=fps)
        joined.unlink(missing_ok=True)
    else:
        joined.replace(final)

    def apply(proj):
        proj["video"] = {
            **(proj.get("video") or {}),
            "file": final.name,
            "seconds": timeline.total_seconds(beats),
            "measured": timeline.all_measured(beats),
            "width": width, "height": height, "fps": fps,
            "assembled_at": store.now(),
        }
    store.mutate(pid, apply)


def _run_video(job: Job, project: dict, settings: dict) -> None:
    try:
        _assemble(job, project, settings)
    except Exception as e:  # noqa: BLE001 - reported on the job, not swallowed
        job.fatal = str(e)
    finally:
        job.finished_at = store.now()
        def apply(proj):
            for scene in proj["scenes"]:
                if (scene["n"] in job.scenes
                        and scene.get("video_status") in ("queued", "encoding")):
                    scene["video_status"] = "pending"
                    scene["video_detail"] = job.fatal or "stopped"
        store.mutate(job.pid, apply)


def start_video(pid: str) -> dict:
    """Assemble the cut. Local and free, so there is nothing to confirm."""
    existing = job_for(pid, KIND_VIDEO)
    if existing and existing.running:
        raise RuntimeError("A video assembly is already going for this project.")

    project = store.load(pid)
    if not video.available():
        raise RuntimeError(video.install_hint())

    the_plan = plan_video(project)
    if not the_plan["scenes"]:
        return {"started": False, "plan": the_plan,
                "message": "Nothing to assemble - no scene has a rendered image yet."}

    numbers = [row["n"] for row in the_plan["scenes"]]

    def apply(proj):
        for scene in proj["scenes"]:
            if scene["n"] in numbers:
                scene["video_status"] = "queued"
                scene["video_detail"] = "waiting to encode"
    store.mutate(pid, apply)

    job = Job(pid, numbers, KIND_VIDEO)
    with _jobs_guard:
        _jobs[_slot(pid, KIND_VIDEO)] = job
    job.thread = threading.Thread(
        target=_run_video, args=(job, project, the_plan["params"]),
        name=f"video-{pid}", daemon=True,
    )
    job.thread.start()
    return {"started": True, "plan": the_plan, "job": job.as_dict()}
