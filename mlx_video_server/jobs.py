"""In-memory job registry.

Jobs are ephemeral: they live only in this process and vanish on restart. The
generated files outlive them (see ``files.FileStore``).
"""

import asyncio
import itertools
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import GenParams, JobOut, JobStatus

_seq = itertools.count()


@dataclass
class Job:
    params: GenParams
    mode: str
    image_path: Path | None = None
    end_image_path: Path | None = None
    audio_path: Path | None = None

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int = field(default_factory=lambda: next(_seq))
    status: JobStatus = JobStatus.queued
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    file_id: str | None = None
    error: str | None = None
    timings: dict | None = None

    # Set when the job is enqueued (on the event loop); sync callers await it.
    future: "asyncio.Future | None" = None

    def to_out(self, queue_position: int | None = None) -> JobOut:
        return JobOut(
            job_id=self.id,
            status=self.status,
            mode=self.mode,
            queue_position=queue_position if self.status == JobStatus.queued else None,
            file_id=self.file_id,
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            timings=self.timings,
        )


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def add(self, job: Job) -> None:
        self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.seq, reverse=True)

    def queue_position(self, job: Job) -> int:
        """0-based number of still-queued jobs ahead of ``job``."""
        return sum(
            1
            for j in self._jobs.values()
            if j.status == JobStatus.queued and j.seq < job.seq
        )
