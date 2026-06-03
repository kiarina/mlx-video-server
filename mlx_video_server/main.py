"""FastAPI application: sync + async generation, jobs, and files.

    uv run mlx-video-server          # or: uv run python -m mlx_video_server

All generation is funneled through a single worker (see ``worker.py``) so only
one video is ever produced at a time; everything else stays responsive.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import ValidationError as PydanticValidationError

from .config import Settings, load_settings
from .files import FileStore
from .generation import ValidationError, detect_mode, validate_params
from .help import help_payload
from .jobs import Job, JobRegistry
from .schemas import FileOut, GenParams, JobOut, JobStatus
from .worker import Worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    store = FileStore(settings.files_root)
    jobs = JobRegistry()
    worker = Worker(settings, jobs, store)

    app.state.settings = settings
    app.state.store = store
    app.state.jobs = jobs
    app.state.worker = worker

    worker.start()
    if settings.warmup:
        asyncio.create_task(worker.warmup())

    yield

    await worker.stop()


app = FastAPI(title="mlx-video-server", lifespan=lifespan)


# --- shared accessors / auth --------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> FileStore:
    return request.app.state.store


def _jobs(request: Request) -> JobRegistry:
    return request.app.state.jobs


def _worker(request: Request) -> Worker:
    return request.app.state.worker


async def require_auth(request: Request) -> None:
    token = request.app.state.settings.auth_token
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="unauthorized")


PROTECTED = [Depends(require_auth)]


# --- request staging ----------------------------------------------------------


async def _save_upload(upload: UploadFile, dest_dir: Path, stem: str, default_suffix: str) -> Path:
    suffix = Path(upload.filename or "").suffix or default_suffix
    dest = dest_dir / f"{stem}{suffix}"
    with dest.open("wb") as f:
        while chunk := await upload.read(1 << 20):
            f.write(chunk)
    return dest


async def _prepare_job(
    request: Request,
    params_json: str,
    image: UploadFile | None,
    end_image: UploadFile | None,
    audio: UploadFile | None,
) -> Job:
    settings = _settings(request)
    store = _store(request)

    try:
        params = GenParams.model_validate_json(params_json)
    except PydanticValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    has_audio = audio is not None
    try:
        validate_params(params, settings, has_audio=has_audio)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    mode = detect_mode(
        has_image=image is not None,
        has_end_image=end_image is not None,
        has_audio=has_audio,
        generate_audio=params.generate_audio,
    )
    job = Job(params=params, mode=mode)

    tmp = store.tmp_dir(job.id)
    if image is not None:
        job.image_path = await _save_upload(image, tmp, "image", ".png")
    if end_image is not None:
        job.end_image_path = await _save_upload(end_image, tmp, "end_image", ".png")
    if audio is not None:
        job.audio_path = await _save_upload(audio, tmp, "audio", ".wav")

    return job


# --- generation endpoints -----------------------------------------------------


@app.post("/v1/generate", dependencies=PROTECTED)
async def generate_sync(
    request: Request,
    params: str = Form(...),
    image: UploadFile | None = File(None),
    end_image: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
):
    """Synchronous generation: blocks until the video is ready, returns the mp4."""
    job = await _prepare_job(request, params, image, end_image, audio)
    worker = _worker(request)
    await worker.submit(job)

    try:
        metadata = await job.future
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"generation failed: {exc}")

    file_id = metadata["file_id"]
    return FileResponse(
        _store(request).video_path(file_id),
        media_type="video/mp4",
        filename=f"{file_id}.mp4",
        headers={"X-File-Id": file_id, "X-Mode": job.mode},
    )


@app.post("/v1/jobs", status_code=202, dependencies=PROTECTED)
async def create_job(
    request: Request,
    params: str = Form(...),
    image: UploadFile | None = File(None),
    end_image: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
) -> JobOut:
    """Asynchronous generation: enqueue and return a job_id immediately."""
    job = await _prepare_job(request, params, image, end_image, audio)
    worker = _worker(request)
    await worker.submit(job)
    return job.to_out(queue_position=_jobs(request).queue_position(job))


@app.get("/v1/jobs", dependencies=PROTECTED)
async def list_jobs(request: Request) -> list[JobOut]:
    jobs = _jobs(request)
    return [j.to_out(queue_position=jobs.queue_position(j)) for j in jobs.all()]


@app.get("/v1/jobs/{job_id}", dependencies=PROTECTED)
async def get_job(request: Request, job_id: str) -> JobOut:
    jobs = _jobs(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_out(queue_position=jobs.queue_position(job))


@app.delete("/v1/jobs/{job_id}", dependencies=PROTECTED)
async def cancel_job(request: Request, job_id: str) -> JobOut:
    jobs = _jobs(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status == JobStatus.queued:
        job.status = JobStatus.cancelled
    elif job.status == JobStatus.running:
        raise HTTPException(status_code=409, detail="job is running and cannot be cancelled")
    return job.to_out()


# --- files endpoints ----------------------------------------------------------


@app.get("/v1/files", dependencies=PROTECTED)
async def list_files(request: Request) -> list[FileOut]:
    return [FileOut(**_file_fields(m)) for m in _store(request).list()]


@app.get("/v1/files/{file_id}", dependencies=PROTECTED)
async def download_file(request: Request, file_id: str):
    store = _store(request)
    if not store.exists(file_id):
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        store.video_path(file_id), media_type="video/mp4", filename=f"{file_id}.mp4"
    )


@app.get("/v1/files/{file_id}/audio", dependencies=PROTECTED)
async def download_file_audio(request: Request, file_id: str):
    store = _store(request)
    audio = store.audio_path(file_id)
    if not audio.exists():
        raise HTTPException(status_code=404, detail="audio not found")
    return FileResponse(audio, media_type="audio/wav", filename=f"{file_id}.wav")


@app.delete("/v1/files/{file_id}", dependencies=PROTECTED)
async def delete_file(request: Request, file_id: str) -> dict:
    if not _store(request).delete(file_id):
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": file_id}


@app.get("/health")
async def health(request: Request) -> dict:
    worker = _worker(request)
    return {
        "status": "ok",
        "warm": worker.warm,
        "queue_len": worker.queue.qsize(),
        "model_repo": _settings(request).model_repo,
    }


@app.get("/help")
async def help_(request: Request) -> dict:
    """Self-describing usage guide for LLM agents (public, no auth)."""
    return help_payload(_settings(request))


def _file_fields(meta: dict) -> dict:
    return {
        "file_id": meta.get("file_id", ""),
        "mode": meta.get("mode", ""),
        "prompt": meta.get("prompt", ""),
        "params": meta.get("params", {}),
        "has_audio": meta.get("has_audio", False),
        "video_bytes": meta.get("video_bytes", 0),
        "created_at": meta.get("created_at", 0.0),
        "timings": meta.get("timings"),
        "source_job_id": meta.get("source_job_id"),
    }


def run() -> None:
    settings = load_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
