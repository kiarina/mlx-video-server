"""Request/response models and the job state enum."""

from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class GenParams(BaseModel):
    """The ``params`` JSON part of a generation request.

    Only the knobs that actually affect the LTX-2 *distilled* pipeline are
    exposed. Mode (T2V / I2V / A2V / ...) is inferred from which files are
    attached, not set here. Validation against the configured caps and the
    1+8k / multiple-of-64 constraints happens in ``generation.validate_params``.
    """

    prompt: str = Field(..., min_length=1)
    width: int = 512
    height: int = 512
    num_frames: int = 97
    fps: int = 24
    seed: int | None = None
    image_strength: float = Field(1.0, ge=0.0, le=1.0)
    end_image_strength: float | None = Field(None, ge=0.0, le=1.0)
    generate_audio: bool = False


class JobOut(BaseModel):
    job_id: str
    status: JobStatus
    mode: str
    queue_position: int | None = None
    file_id: str | None = None
    error: str | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    timings: dict | None = None


class FileOut(BaseModel):
    file_id: str
    mode: str
    prompt: str
    params: dict
    has_audio: bool
    video_bytes: int
    created_at: float
    timings: dict | None = None
    source_job_id: str | None = None
