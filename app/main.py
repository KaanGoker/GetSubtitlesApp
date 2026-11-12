"""The app: serves the page, takes uploads, runs jobs one at a time, serves and edits the subtitles."""
import asyncio
import io
import mimetypes
import os
import queue
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, model_validator

from app.gpu import DOWNLOAD_BYTES as GPU_DOWNLOAD_BYTES
from app.audio import AudioError, extract_wav, probe
from app.engine import MODEL_SIZES, Cancelled, Engine, gpu_count
from app.visits import Visits
from app.subtitles import Cue, build_default_cues, build_vertical_cues, to_srt, to_txt, to_vtt

STATIC_DIR = Path(__file__).resolve().parent / "static"
WRITERS = {"srt": to_srt, "vtt": to_vtt, "txt": to_txt}
ACTIVE = ("queued", "preparing", "loading_model", "downloading_model", "transcribing")
DEMO_MAX_AUDIO_MINUTES = 30     # one visitor cannot hold the shared queue for hours
DEMO_MAX_WAITING_JOBS = 10
Format = Literal["srt", "vtt", "txt"]


class JobRequest(BaseModel):
    upload_id: str
    language: str = Field("auto", pattern=r"^(auto|[a-z]{2,3})$")
    task: Literal["transcribe", "translate"] = "transcribe"
    model_choice: Literal["fast", "balanced", "best", "turbo"] = "fast"
    style: Literal["default", "vertical"] = "default"
    audio_track: int = Field(0, ge=0)


class CueIn(BaseModel):
    start: float = Field(ge=0)
    end: float
    text: str

    @model_validator(mode="after")
    def ends_after_it_starts(self):
        if self.end <= self.start:
            raise ValueError("a cue must end after it starts")
        return self


class Upload:
    def __init__(self, upload_id: str, filename: str, path: Path, audio_tracks: List[dict]):
        self.upload_id = upload_id
        self.filename = filename
        self.path = path
        self.audio_tracks = audio_tracks
        self.used_at = time.time()


class Job:
    def __init__(self, job_id: str, upload: Upload, request: JobRequest):
        self.job_id = job_id
        self.upload = upload
        self.request = request
        self.status = "queued"
        self.progress = 0.0
        self.download_progress: Optional[float] = None
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.original_cues: List[Cue] = []
        self.cues: List[Cue] = []
        self.edited = False
        self.cancelled = False
        self.created_at = time.time()
        self.used_at = self.created_at
        self.transcribing_since: Optional[float] = None
        self.finished_at: Optional[float] = None


class NoCacheStatic(StaticFiles):
    """The page's files change with every release; the browser must ask each time."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, minutes, secs = total // 3600, total % 3600 // 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _open_folder(path: Path) -> None:
    """Show a folder in the file manager of this computer."""
    import subprocess
    import sys
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def models_on_disk(cache: Path) -> List[dict]:
    """The Whisper models in a Hugging Face cache folder, with their size on disk."""
    by_size = {size: choice for choice, size in MODEL_SIZES.items()}
    found = []
    for choice in MODEL_SIZES:
        for repo in sorted(cache.glob("models--*--faster-whisper-*")):
            if by_size.get(repo.name.split("--faster-whisper-", 1)[1]) != choice:
                continue
            if not any(repo.glob("snapshots/*/model.bin")):
                continue            # still downloading, or the download stopped half way
            size = sum(f.stat().st_size for f in repo.rglob("*")
                       if f.is_file() and not f.is_symlink() and not f.name.endswith(".incomplete"))
            found.append({"model": choice, "size_bytes": size})
    return found


def _exit_process() -> None:
    def _exit():
        time.sleep(0.1)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()


def create_app(engine: Optional[Engine] = None,
               data_dir: Optional[Path] = None,
               demo: Optional[bool] = None,
               token: Optional[str] = None,
               max_upload_bytes: Optional[int] = None,
               keep_seconds: Optional[float] = None,
               max_audio_seconds: Optional[float] = None,
               max_waiting_jobs: Optional[int] = None,
               cleanup_every_seconds: Optional[float] = None,
               clock: Callable[[], float] = time.time,
               allowed_hosts: Optional[List[str]] = None,
               presence_ping_seconds: float = 10.0,
               exit_fn: Callable[[], None] = _exit_process,
               storage_dir: Optional[Path] = None,
               models_cache: Optional[Path] = None,
               open_folder: Callable[[Path], None] = _open_folder,
               gpu_install: Optional[Callable] = None,
               gpu_present: Callable[[], bool] = lambda: gpu_count() > 0) -> FastAPI:
    """
    demo: the public Space. Only the Fast model, smaller uploads, no shared cleanup.
    token: the desktop app. Every request that changes something must carry it, so another
           website open in the same browser cannot drive the app. It also enables /shutdown.
    max_audio_seconds: the longest file the app takes; the demo takes 30 minutes, the app any length.
    max_waiting_jobs: how many jobs may wait or run at once; the demo turns new ones away beyond it.
    keep_seconds: how long uploads and results stay after they were last used, so a file can be
                  run again with other settings and its subtitles can be edited.
    clock: the time used to decide what is idle.
    allowed_hosts: the addresses the app answers to. The desktop app answers only to itself;
                   Docker reads GETSUBTITLES_ALLOWED_HOSTS (comma separated) and answers only to
                   127.0.0.1 and localhost when it is not set; "*" answers to any address, the
                   default of the public demo.
    storage_dir: the desktop app's own folder (models, log). The page shows it and can open it.
    models_cache: the Hugging Face cache the models download to; the hub cache by default.
    gpu_install: downloads the CUDA libraries into a folder, gpu_install(folder, on_progress).
                 The packaged app (Windows, Linux) offers it on the page when it has an NVIDIA GPU but no libraries.
    """
    if demo is None:
        demo = os.environ.get("GETSUBTITLES_DEMO", "") == "1"
    if token is None:
        token = os.environ.get("GETSUBTITLES_TOKEN") or None
    if max_upload_bytes is None:
        max_upload_bytes = (500 if demo else 8192) * 1024 * 1024
    if keep_seconds is None:
        keep_seconds = 3600 if demo else 6 * 3600
    if max_audio_seconds is None and demo:
        max_audio_seconds = DEMO_MAX_AUDIO_MINUTES * 60
    if max_waiting_jobs is None and demo:
        max_waiting_jobs = DEMO_MAX_WAITING_JOBS
    if cleanup_every_seconds is None:
        cleanup_every_seconds = min(300.0, max(keep_seconds / 4, 1.0))
    engine = engine or Engine()
    allowed_models = ["fast"] if demo else list(MODEL_SIZES)

    data_dir = Path(data_dir) if data_dir else Path(tempfile.gettempdir()) / "getsubtitles-data"
    shutil.rmtree(data_dir, ignore_errors=True)  # leftovers of an earlier run
    data_dir.mkdir(parents=True, exist_ok=True)

    uploads: Dict[str, Upload] = {}
    jobs: Dict[str, Job] = {}
    lock = threading.Lock()
    pending: "queue.Queue[Job]" = queue.Queue()

    # ---------- worker: one job at a time ----------

    def run(job: Job) -> None:
        if job.cancelled:
            return
        wav = data_dir / f"{job.job_id}.wav"
        try:
            job.status = "preparing"
            # The length check at upload reads what the file header says; this caps the real audio.
            extract_wav(job.upload.path, wav, track=job.request.audio_track, max_seconds=max_audio_seconds)

            def on_status(status: str) -> None:
                job.status = status
                if status == "transcribing":
                    job.transcribing_since = time.time()

            def on_progress(value: float) -> None:
                job.progress = min(value, 0.999)

            def on_download(value: float) -> None:
                job.status = "downloading_model"
                job.download_progress = round(value, 3)

            req = job.request
            result = engine.transcribe(
                wav,
                model_choice=req.model_choice,
                language=None if req.language == "auto" else req.language,
                task=req.task,
                on_progress=on_progress,
                on_status=on_status,
                should_cancel=lambda: job.cancelled,
                on_download=on_download,
            )
            segments = result.pop("segments")
            words = [w for s in segments for w in s["words"]]
            if req.style == "vertical" and words:
                cues = build_vertical_cues(words)
            else:
                cues = build_default_cues(segments)
            job.original_cues = cues
            job.cues = list(cues)
            job.result = result
            job.progress = 1.0
            job.status = "done"
        except Cancelled:
            pass
        except Exception as e:
            job.error = str(e) or e.__class__.__name__
            job.status = "error"
        finally:
            job.finished_at = time.time()
            job.used_at = job.upload.used_at = clock()
            wav.unlink(missing_ok=True)

    def worker() -> None:
        while True:
            run(pending.get())

    threading.Thread(target=worker, daemon=True, name="getsubtitles-worker").start()

    def active_jobs() -> List[Job]:
        return [j for j in jobs.values() if j.status in ACTIVE]

    def remove_idle(older_than: float) -> None:
        """Drop uploads and finished jobs not used for the given time, unless a job still needs them."""
        now = clock()
        with lock:
            in_use = {j.upload.upload_id for j in active_jobs()}
            for job_id, job in list(jobs.items()):
                if job.status not in ACTIVE and now - job.used_at >= older_than:
                    del jobs[job_id]
            for upload_id, up in list(uploads.items()):
                if upload_id not in in_use and now - up.used_at >= older_than:
                    up.path.unlink(missing_ok=True)
                    del uploads[upload_id]

    def janitor() -> None:
        while True:
            time.sleep(cleanup_every_seconds)
            remove_idle(keep_seconds)

    threading.Thread(target=janitor, daemon=True, name="getsubtitles-janitor").start()

    def find_job(job_id: str) -> Job:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        job.used_at = job.upload.used_at = clock()
        return job

    def finished_job(job_id: str) -> Job:
        job = find_job(job_id)
        if job.status != "done":
            raise HTTPException(409, "The subtitles are not ready yet.")
        return job

    def need_token(x_token: str = Header("")) -> None:
        if token and x_token != token:
            raise HTTPException(403, "wrong token")

    # ---------- the page ----------

    def close() -> None:
        shutil.rmtree(data_dir, ignore_errors=True)
        exit_fn()

    # The demo counts page loads per day in a private dataset; the desktop app never counts.
    visits = Visits.from_env("getsubtitlesapp") if demo else Visits("getsubtitlesapp")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        visits.start()
        yield
        await asyncio.to_thread(visits.close)

    app = FastAPI(title="GetSubtitles", lifespan=lifespan)
    app.state.visits = visits
    if allowed_hosts is None:
        if token:   # the desktop app: a page on another domain name that points at 127.0.0.1 is refused
            allowed_hosts = ["127.0.0.1", "localhost"]
        else:
            allowed_hosts = [h.strip() for h in os.environ.get("GETSUBTITLES_ALLOWED_HOSTS", "").split(",")
                             if h.strip()] or (["*"] if demo else ["127.0.0.1", "localhost"])
    if allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def same_site_only(request: Request, call_next):
        """A page on another site may not change anything here, with or without a token."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            cross = request.headers.get("sec-fetch-site") == "cross-site"
            if origin and (origin == "null" or urlsplit(origin).netloc != request.headers.get("host")):
                cross = True
            if cross:
                return _error(403, "Requests from other websites are not allowed.")
        return await call_next(request)
    app.state.open_tabs = 0
    app.state.active_jobs = lambda: len(active_jobs())   # for the desktop launcher
    app.state.close = close

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return _error(exc.status_code, str(exc.detail))

    @app.get("/", response_class=HTMLResponse)
    def page(request: Request):
        visits.count(request.method, request.headers)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{token}}", token or ""), headers={"Cache-Control": "no-store"})

    app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

    @app.get("/health")
    def health():
        return {
            "app": "getsubtitles",
            "status": "ok",
            "demo": demo,
            "models": allowed_models,
            "device": engine.device,
            "max_upload_mb": max_upload_bytes // (1024 * 1024),
            "max_audio_minutes": round(max_audio_seconds / 60) if max_audio_seconds else None,
            "active_jobs": len(active_jobs()),
            "open_tabs": app.state.open_tabs,
        }

    if storage_dir and not demo:
        storage_dir = Path(storage_dir)

        @app.get("/storage")
        def storage():
            cache = models_cache
            if cache is None:
                from huggingface_hub import constants
                cache = constants.HF_HUB_CACHE
            return {"path": str(storage_dir), "models": models_on_disk(Path(cache))}

        @app.post("/storage/open", dependencies=[Depends(need_token)])
        def storage_open():
            storage_dir.mkdir(parents=True, exist_ok=True)
            open_folder(storage_dir)
            return {"ok": True}

        if gpu_install is not None:
            gpu = {"running": False, "progress": 0.0, "error": None}

            @app.get("/gpu")
            def gpu_state():
                ready = engine.device == "cuda"
                return {"present": ready or gpu_present(), "ready": ready, "running": gpu["running"],
                        "installed": (storage_dir / "cuda").is_dir(),
                        "progress": round(gpu["progress"], 3), "error": gpu["error"],
                        "download_bytes": GPU_DOWNLOAD_BYTES}

            @app.post("/gpu/install", dependencies=[Depends(need_token)])
            def gpu_start():
                with lock:
                    if gpu["running"]:
                        raise HTTPException(409, "GPU support is already downloading.")
                    gpu.update(running=True, progress=0.0, error=None)

                def run():
                    try:
                        gpu_install(storage_dir / "cuda", lambda f: gpu.update(progress=f))
                    except Exception as e:
                        print(f"[GETSUBTITLES] GPU support did not install: {e!r}")
                        gpu["error"] = f"GPU support could not be downloaded. ({e})"
                    finally:
                        gpu["running"] = False
                threading.Thread(target=run, daemon=True).start()
                return {"ok": True}

    @app.get("/presence")
    async def presence():
        """The page keeps this stream open. The number of open streams is the number of open tabs."""
        async def stream():
            app.state.open_tabs += 1
            try:
                while True:
                    yield ": here\n\n"
                    await asyncio.sleep(presence_ping_seconds)
            finally:
                app.state.open_tabs -= 1
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---------- uploads ----------

    @app.post("/uploads", dependencies=[Depends(need_token)])
    async def create_upload(request: Request, filename: str = Query("file")):
        """The request body is the file itself, streamed to disk."""
        remove_idle(keep_seconds)
        upload_id = uuid4().hex
        path = data_dir / f"{upload_id}.src"
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_upload_bytes:
                        break
                    f.write(chunk)
        except ClientDisconnect:        # the tab was closed or the upload was removed halfway
            path.unlink(missing_ok=True)
            return _error(400, "The upload was interrupted.")
        if size > max_upload_bytes:
            path.unlink(missing_ok=True)
            return _error(413, f"The file is larger than {max_upload_bytes // (1024 * 1024)} MB.")
        try:
            tracks, seconds = await run_in_threadpool(probe, path)
        except AudioError as e:
            path.unlink(missing_ok=True)
            return _error(400, str(e))
        if not tracks:
            path.unlink(missing_ok=True)
            return _error(400, "This file has no audio track.")
        if max_audio_seconds and seconds and seconds > max_audio_seconds:
            path.unlink(missing_ok=True)
            return _error(413, f"This file is {_clock(seconds)} long. The demo takes up to "
                               f"{round(max_audio_seconds / 60)} minutes of audio; the desktop app "
                               f"has no length limit.")
        name = Path(filename.replace("\\", "/")).name or "file"
        with lock:
            uploads[upload_id] = Upload(upload_id, name, path, tracks)
            uploads[upload_id].used_at = clock()
        return {"upload_id": upload_id, "filename": name, "audio_tracks": tracks}

    @app.get("/uploads/{upload_id}")
    def get_upload(upload_id: str):
        upload = uploads.get(upload_id)
        if not upload:
            return _error(404, "This upload is no longer on the server.")
        return {"upload_id": upload_id, "filename": upload.filename, "audio_tracks": upload.audio_tracks}

    @app.get("/uploads/{upload_id}/media")
    def get_media(upload_id: str):
        """The uploaded file itself, so the page can play it next to the subtitles."""
        upload = uploads.get(upload_id)
        if not upload:
            return _error(404, "This upload is no longer on the server.")
        upload.used_at = clock()
        # The type comes from the name the visitor chose, so only audio and video types are kept:
        # a file named page.html must never run as a page on this address.
        media_type = mimetypes.guess_type(upload.filename)[0] or ""
        if not media_type.startswith(("audio/", "video/")):
            media_type = "application/octet-stream"
        return FileResponse(upload.path, media_type=media_type, headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        })

    # ---------- jobs ----------

    @app.post("/jobs", dependencies=[Depends(need_token)])
    def create_job(req: JobRequest):
        upload = uploads.get(req.upload_id)
        if not upload:
            return _error(404, "This upload is no longer on the server. Add the file again.")
        if req.model_choice not in allowed_models:
            return _error(403, "This model is not available in the demo.")
        if req.audio_track >= len(upload.audio_tracks):
            return _error(400, "The file has no audio track with that number.")
        upload.used_at = clock()
        job = Job(uuid4().hex, upload, req)
        job.used_at = clock()
        with lock:
            if max_waiting_jobs and len(active_jobs()) >= max_waiting_jobs:
                return _error(503, "The demo is busy right now. Try again in a few minutes, "
                                   "or use the desktop app.")
            jobs[job.job_id] = job
        pending.put(job)
        return {"job_id": job.job_id}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        job = find_job(job_id)
        eta = None
        if job.status == "transcribing" and job.progress > 0.01 and job.transcribing_since:
            elapsed = time.time() - job.transcribing_since
            eta = round(elapsed * (1.0 - job.progress) / job.progress, 1)
        position = None
        if job.status == "queued":
            position = sum(1 for j in active_jobs() if j.created_at < job.created_at)
        result = job.result or {}
        return {
            "job_id": job.job_id,
            "upload_id": job.upload.upload_id,
            "status": job.status,
            "progress": round(job.progress, 4),
            "download_progress": job.download_progress,
            "eta_sec": eta,
            "queue_position": position,
            "filename": job.upload.filename,
            "style": job.request.style,
            "task": job.request.task,
            "language": result.get("language"),
            "language_auto": job.request.language == "auto",
            "language_probability": result.get("language_probability"),
            "duration_sec": result.get("duration"),
            "model_choice": result.get("model_choice") or job.request.model_choice,
            "device": result.get("device"),
            "elapsed_sec": round((job.finished_at or time.time()) - job.created_at, 1),
            "edited": job.edited,
            "error": job.error,
        }

    @app.delete("/jobs/{job_id}", dependencies=[Depends(need_token)])
    def delete_job(job_id: str):
        """Remove a job. One that is waiting never runs; one that is running stops at its next segment."""
        job = find_job(job_id)
        job.cancelled = True
        with lock:
            jobs.pop(job_id, None)
        return {"removed": True}

    @app.get("/jobs/{job_id}/subtitles")
    def get_subtitles(job_id: str, format: Format = "srt"):
        job = finished_job(job_id)
        name = f"{Path(job.upload.filename).stem}.{format}"
        return PlainTextResponse(
            WRITERS[format](job.cues),
            headers={"Content-Disposition":
                     f"attachment; filename=\"subtitles.{format}\"; filename*=UTF-8''{quote(name)}"},
        )

    @app.get("/jobs/{job_id}/cues")
    def get_cues(job_id: str):
        job = finished_job(job_id)
        return [{"start": c.start, "end": c.end, "text": c.text} for c in job.cues]

    @app.put("/jobs/{job_id}/cues", dependencies=[Depends(need_token)])
    def put_cues(job_id: str, cues: List[CueIn]):
        job = finished_job(job_id)
        job.cues = sorted((Cue(c.start, c.end, c.text) for c in cues), key=lambda c: c.start)
        job.edited = True
        return {"saved": len(job.cues)}

    @app.delete("/jobs/{job_id}/cues", dependencies=[Depends(need_token)])
    def reset_cues(job_id: str):
        """Undo every edit."""
        job = finished_job(job_id)
        job.cues = list(job.original_cues)
        job.edited = False
        return {"saved": len(job.cues)}

    @app.get("/zip")
    def get_zip(jobs: str = Query(""), format: Format = "srt"):
        """The subtitles of several jobs in one zip, each named after its file."""
        chosen = [finished_job(job_id) for job_id in jobs.split(",") if job_id]
        if not chosen:
            return _error(404, "no jobs")
        buffer = io.BytesIO()
        taken = {}
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for job in chosen:
                stem = Path(job.upload.filename).stem
                taken[stem] = taken.get(stem, 0) + 1
                suffix = f" ({taken[stem]})" if taken[stem] > 1 else ""
                z.writestr(f"{stem}{suffix}.{format}", WRITERS[format](job.cues))
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="subtitles.zip"'})

    # ---------- housekeeping ----------

    @app.post("/cleanup", dependencies=[Depends(need_token)])
    def cleanup():
        """Delete uploaded files and finished jobs, and free the loaded models."""
        if demo:
            return _error(403, "Not available in the demo.")
        remove_idle(0)
        engine.clear()
        return {"cleared": True}

    if token:
        @app.post("/shutdown", dependencies=[Depends(need_token)])
        def shutdown():
            close()
            return {"ok": True}

    return app
