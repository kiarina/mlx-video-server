"""Directory-based file store.

Each generated artifact lives in ``${files_root}/{file_id}/`` and is fully
self-describing via ``metadata.json``, so the files API keeps working across
restarts even though job state is in-memory and ephemeral.

Layout::

    ${files_root}/{file_id}/
      ├── video.mp4
      ├── audio.wav          # only when audio was generated / conditioned
      ├── input_image.*      # saved inputs, for provenance / reproducibility
      ├── input_end_image.*
      ├── input_audio.*
      └── metadata.json
"""

import json
import shutil
import uuid
from pathlib import Path

VIDEO_NAME = "video.mp4"
AUDIO_NAME = "audio.wav"
METADATA_NAME = "metadata.json"


class FileStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._tmp = self.root / ".tmp"
        self._tmp.mkdir(exist_ok=True)

    # -- temp input staging (handler thread writes uploads here) ----------------

    def tmp_dir(self, job_id: str) -> Path:
        d = self._tmp / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def clear_tmp(self, job_id: str) -> None:
        shutil.rmtree(self._tmp / job_id, ignore_errors=True)

    # -- final artifact dirs ----------------------------------------------------

    def new_file(self) -> tuple[str, Path]:
        file_id = uuid.uuid4().hex
        d = self.root / file_id
        d.mkdir(parents=True, exist_ok=False)
        return file_id, d

    def dir(self, file_id: str) -> Path:
        return self.root / file_id

    def video_path(self, file_id: str) -> Path:
        return self.dir(file_id) / VIDEO_NAME

    def audio_path(self, file_id: str) -> Path:
        return self.dir(file_id) / AUDIO_NAME

    def exists(self, file_id: str) -> bool:
        return self.video_path(file_id).exists()

    def write_metadata(self, file_id: str, metadata: dict) -> None:
        path = self.dir(file_id) / METADATA_NAME
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))

    def read_metadata(self, file_id: str) -> dict | None:
        path = self.dir(file_id) / METADATA_NAME
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def list(self) -> list[dict]:
        out: list[dict] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name == ".tmp":
                continue
            meta = self.read_metadata(child.name)
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        return out

    def delete(self, file_id: str) -> bool:
        d = self.dir(file_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True
