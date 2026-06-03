"""Single-flight generation worker.

All MLX work — model load, warmup, and every generation — runs on ONE dedicated
thread (``ThreadPoolExecutor(max_workers=1)``). MLX arrays/streams are
thread-affine, so this is required for correctness, and it also gives us the
"only ever one generation at a time" guarantee for free. The event loop stays
free to accept requests and answer status/files queries while a job runs.
"""

import asyncio
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from .config import Settings
from .files import FileStore
from .generation import run_generation
from .jobs import Job, JobRegistry
from .schemas import GenParams, JobStatus


class Worker:
    def __init__(self, settings: Settings, jobs: JobRegistry, store: FileStore):
        self.settings = settings
        self.jobs = jobs
        self.store = store
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx-video")
        self.warm = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mlx-video-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def submit(self, job: Job) -> None:
        loop = asyncio.get_running_loop()
        job.future = loop.create_future()
        self.jobs.add(job)
        await self.queue.put(job)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job = await self.queue.get()
            if job.status == JobStatus.cancelled:
                continue
            job.status = JobStatus.running
            job.started_at = time.time()
            try:
                metadata = await loop.run_in_executor(
                    self.executor, run_generation, job, self.settings, self.store
                )
                job.file_id = metadata["file_id"]
                job.timings = metadata.get("timings")
                job.status = JobStatus.completed
                if not job.future.done():
                    job.future.set_result(metadata)
            except Exception as exc:  # noqa: BLE001 — surface any failure to the client
                traceback.print_exc()
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = JobStatus.failed
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                job.finished_at = time.time()
                self.store.clear_tmp(job.id)

    async def warmup(self) -> None:
        """Compile MLX kernels and load weights up front (runs on the MLX thread)."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self.executor, self._warmup_blocking)
            self.warm = True
        except Exception:  # noqa: BLE001 — warmup is best-effort
            traceback.print_exc()

    def _warmup_blocking(self) -> None:
        params = GenParams(prompt="a calm landscape", width=256, height=256, num_frames=9)
        job = Job(params=params, mode="T2V")
        metadata = run_generation(job, self.settings, self.store)
        # Discard the warmup artifact; it was only to prime kernels/weights.
        self.store.delete(metadata["file_id"])
