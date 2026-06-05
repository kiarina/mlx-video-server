"""End-to-end API tests that stub out the actual MLX generation.

The real ``run_generation`` (which loads LTX-2 and renders a video) is replaced
with a fake that writes a tiny artifact through the real ``FileStore``. This
exercises the whole server — queue, single worker, sync/async endpoints,
validation, and the files API — without needing the model.
"""

import time

import pytest
from fastapi.testclient import TestClient

import mlx_video_server.worker as worker_module
from mlx_video_server.main import app


def _fake_run_generation(job, settings, store):
    file_id, fdir = store.new_file()
    (fdir / "video.mp4").write_bytes(b"FAKE_MP4_BYTES")
    seed = job.params.seed if job.params.seed is not None else 999
    meta = {
        "file_id": file_id,
        "source_job_id": job.id,
        "mode": job.mode,
        "prompt": job.params.prompt,
        "params": {**job.params.model_dump(), "seed": seed},
        "has_audio": False,
        "video_bytes": (fdir / "video.mp4").stat().st_size,
        "created_at": time.time(),
        "timings": {"total_s": 0.0},
    }
    store.write_metadata(file_id, meta)
    return meta


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_VIDEO_FILES_ROOT", str(tmp_path / "files"))
    monkeypatch.setenv("MLX_VIDEO_WARMUP", "0")
    monkeypatch.delenv("MLX_VIDEO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(worker_module, "run_generation", _fake_run_generation)
    with TestClient(app) as c:
        yield c


def _wait_for(client, job_id, target="completed", timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/jobs/{job_id}").json()
        if body["status"] in (target, "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {target}")


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["warm"] is False  # warmup disabled


def test_help(client):
    body = client.get("/help").json()
    assert body["service"] == "mlx-video-server"
    assert {"modes", "endpoints", "params", "constraints", "know_how"} <= body.keys()
    # caps reflect the live settings
    assert body["params"]["num_frames"]["max"] == 161
    # the discovered know-how is present
    assert any("image_strength" in k["tip"] for k in body["know_how"])


def test_async_job_lifecycle(client):
    r = client.post(
        "/v1/jobs",
        data={"params": '{"prompt":"a cat","num_frames":9,"width":256,"height":256}'},
    )
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued"
    assert job["mode"] == "T2V"

    done = _wait_for(client, job["job_id"])
    assert done["status"] == "completed"
    file_id = done["file_id"]
    assert file_id

    # metadata
    meta = client.get(f"/v1/files/{file_id}")
    assert meta.status_code == 200
    assert meta.json()["file_id"] == file_id

    # download
    dl = client.get(f"/v1/files/{file_id}/download")
    assert dl.status_code == 200
    assert dl.content == b"FAKE_MP4_BYTES"

    # list
    files = client.get("/v1/files").json()
    assert any(f["file_id"] == file_id for f in files)

    # delete
    assert client.delete(f"/v1/files/{file_id}").status_code == 200
    assert client.get(f"/v1/files/{file_id}").status_code == 404
    assert client.get(f"/v1/files/{file_id}/download").status_code == 404


def test_sync_generate_returns_mp4(client):
    r = client.post(
        "/v1/generate",
        data={"params": '{"prompt":"ocean waves","num_frames":9,"width":256,"height":256}'},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["x-file-id"]
    assert r.content == b"FAKE_MP4_BYTES"


def test_image_to_video_mode(client):
    r = client.post(
        "/v1/jobs",
        data={"params": '{"prompt":"open eyes","num_frames":9,"width":256,"height":256,"image_strength":0.7}'},
        files={"image": ("frame.png", b"\x89PNG\r\n", "image/png")},
    )
    assert r.status_code == 202, r.text
    assert r.json()["mode"] == "I2V"


@pytest.mark.parametrize(
    "params,needle",
    [
        ('{"prompt":"x","num_frames":50}', "1 + 8"),       # not 1+8k
        ('{"prompt":"x","width":500}', "multiple of 64"),  # bad width
        ('{"prompt":"x","num_frames":1001}', "exceeds max"),  # 1+8k but over cap
    ],
)
def test_validation_422(client, params, needle):
    r = client.post("/v1/jobs", data={"params": params})
    assert r.status_code == 422
    assert needle in str(r.json()["detail"])


def test_audio_exclusivity_422(client):
    r = client.post(
        "/v1/jobs",
        data={"params": '{"prompt":"x","num_frames":9,"generate_audio":true}'},
        files={"audio": ("a.wav", b"RIFF....", "audio/wav")},
    )
    assert r.status_code == 422
    assert "generate_audio" in str(r.json()["detail"])


def test_cancel_queued_job(client, monkeypatch):
    # Make generation block so the second job stays queued long enough to cancel.
    started = {"flag": False}

    def slow(job, settings, store):
        started["flag"] = True
        time.sleep(0.3)
        return _fake_run_generation(job, settings, store)

    monkeypatch.setattr(worker_module, "run_generation", slow)

    p = '{"prompt":"x","num_frames":9,"width":256,"height":256}'
    first = client.post("/v1/jobs", data={"params": p}).json()
    second = client.post("/v1/jobs", data={"params": p}).json()

    # second should be queued behind first
    cancel = client.delete(f"/v1/jobs/{second['job_id']}")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
    _ = first
