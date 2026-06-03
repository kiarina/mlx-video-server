"""Validation, mode detection, and the blocking call into mlx-video.

Everything here runs on the single MLX worker thread (see ``worker.py``), except
``validate_params`` / ``detect_mode`` which are pure and called from the request
handlers to reject bad input early with a 422.
"""

import random
import shutil
import time
from pathlib import Path

from .config import Settings
from .jobs import Job
from .schemas import GenParams


class ValidationError(ValueError):
    """Raised for request-level problems; mapped to HTTP 422 by the API layer."""


def detect_mode(
    *, has_image: bool, has_end_image: bool, has_audio: bool, generate_audio: bool
) -> str:
    is_i2v = has_image or has_end_image
    if has_audio:
        return "A2V+I2V" if is_i2v else "A2V"
    if is_i2v:
        if has_image and has_end_image:
            return "I2V(first+last)"
        if has_end_image:
            return "I2V(last)"
        return "I2V"
    return "T2V+Audio" if generate_audio else "T2V"


def validate_params(
    params: GenParams,
    settings: Settings,
    *,
    has_audio: bool,
) -> None:
    if params.width <= 0 or params.width % 64 != 0:
        raise ValidationError(
            f"width must be a positive multiple of 64 (got {params.width})"
        )
    if params.height <= 0 or params.height % 64 != 0:
        raise ValidationError(
            f"height must be a positive multiple of 64 (got {params.height})"
        )
    if params.width > settings.max_width:
        raise ValidationError(
            f"width {params.width} exceeds max {settings.max_width}"
        )
    if params.height > settings.max_height:
        raise ValidationError(
            f"height {params.height} exceeds max {settings.max_height}"
        )
    if params.num_frames < 1 or (params.num_frames - 1) % 8 != 0:
        raise ValidationError(
            f"num_frames must be 1 + 8*k (e.g. 33, 49, 97, 161); got {params.num_frames}"
        )
    if params.num_frames > settings.max_num_frames:
        raise ValidationError(
            f"num_frames {params.num_frames} exceeds max {settings.max_num_frames}"
        )
    if params.fps <= 0:
        raise ValidationError(f"fps must be positive (got {params.fps})")
    if has_audio and params.generate_audio:
        raise ValidationError(
            "cannot combine an audio file (A2V) with generate_audio; choose one"
        )


def run_generation(job: Job, settings: Settings, store) -> dict:
    """Blocking generation. Returns the metadata dict written to the file dir."""
    from mlx_video.models.ltx_2.generate import PipelineType, generate_video

    params = job.params
    file_id, fdir = store.new_file()

    # Copy staged inputs into the artifact dir for provenance, and use those paths.
    image = _stage(job.image_path, fdir, "input_image")
    end_image = _stage(job.end_image_path, fdir, "input_end_image")
    audio_file = _stage(job.audio_path, fdir, "input_audio")

    has_audio_output = audio_file is not None or params.generate_audio
    video_path = store.video_path(file_id)
    audio_out = str(store.audio_path(file_id)) if has_audio_output else None

    seed = params.seed if params.seed is not None else random.randint(0, 2**31 - 1)

    t0 = time.time()
    generate_video(
        model_repo=settings.model_repo,
        text_encoder_repo=settings.text_encoder_repo,
        prompt=params.prompt,
        pipeline=PipelineType.DISTILLED,
        height=params.height,
        width=params.width,
        num_frames=params.num_frames,
        fps=params.fps,
        seed=seed,
        output_path=str(video_path),
        image=image,
        image_strength=params.image_strength,
        end_image=end_image,
        end_image_strength=params.end_image_strength,
        audio=params.generate_audio,
        audio_file=audio_file,
        output_audio_path=audio_out,
        verbose=False,
    )
    total_s = round(time.time() - t0, 2)

    if not video_path.exists():
        raise RuntimeError("generation finished but no video file was produced")

    metadata = {
        "file_id": file_id,
        "source_job_id": job.id,
        "mode": job.mode,
        "prompt": params.prompt,
        "params": {**params.model_dump(), "seed": seed},
        "has_audio": has_audio_output and store.audio_path(file_id).exists(),
        "video_bytes": video_path.stat().st_size,
        "created_at": time.time(),
        "timings": {"total_s": total_s},
    }
    store.write_metadata(file_id, metadata)
    return metadata


def _stage(src: Path | None, dest_dir: Path, stem: str) -> str | None:
    if src is None:
        return None
    dest = dest_dir / f"{stem}{src.suffix}"
    shutil.copyfile(src, dest)
    return str(dest)
