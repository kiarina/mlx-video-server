"""Runtime configuration, read from the environment (prefix ``MLX_VIDEO_``).

Kept deliberately small and dependency-free (plain ``os.environ`` reads) to match
the sibling mlx-embedding-server's style.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


@dataclass(frozen=True)
class Settings:
    # Model
    model_repo: str
    text_encoder_repo: str | None

    # Storage / network
    files_root: Path
    host: str
    port: int
    auth_token: str | None
    warmup: bool

    # Worker-protection caps (a single in-flight job blocks the queue, so bound it)
    max_num_frames: int
    max_width: int
    max_height: int

    # Defaults applied when a request omits a field
    default_width: int = 512
    default_height: int = 512
    default_num_frames: int = 97
    default_fps: int = 24


def load_settings() -> Settings:
    return Settings(
        model_repo=os.environ.get(
            "MLX_VIDEO_MODEL_REPO", "prince-canuma/LTX-2-distilled"
        ),
        text_encoder_repo=os.environ.get("MLX_VIDEO_TEXT_ENCODER_REPO") or None,
        files_root=_expand(
            os.environ.get("MLX_VIDEO_FILES_ROOT", "~/.mlx-video-server/files")
        ),
        host=os.environ.get("MLX_VIDEO_HOST", "127.0.0.1"),
        port=int(os.environ.get("MLX_VIDEO_PORT", "8800")),
        auth_token=os.environ.get("MLX_VIDEO_AUTH_TOKEN") or None,
        warmup=os.environ.get("MLX_VIDEO_WARMUP", "1") == "1",
        max_num_frames=int(os.environ.get("MLX_VIDEO_MAX_NUM_FRAMES", "161")),
        max_width=int(os.environ.get("MLX_VIDEO_MAX_WIDTH", "768")),
        max_height=int(os.environ.get("MLX_VIDEO_MAX_HEIGHT", "768")),
    )
