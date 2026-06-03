# mlx-video-server

Single-purpose HTTP server for **video generation** on Apple Silicon, wrapping
[mlx-video](https://github.com/Blaizzy/mlx-video)'s LTX-2 **distilled** pipeline.

One endpoint family accepts **text / image / audio** and produces a video. A
single in-process worker runs generations **one at a time** (MLX is memory-heavy
and thread-affine), while the API stays responsive for status and file queries.

日本語版: [README.ja.md](README.ja.md)

## Features

- **LTX-2 distilled** (fast: ~40s–1.5min per clip at 512×512). Modes are inferred
  from the inputs you attach: T2V / I2V / I2V(first+last) / A2V / A2V+I2V / T2V+Audio.
- **Single-flight queue** — concurrent requests are accepted but generation never
  overlaps. Model is loaded once and kept resident.
- **Sync and async** endpoints sharing one queue.
- **Files API** — download / list / delete. Artifacts outlive jobs (jobs are
  in-memory and reset on restart; files persist on disk and stay self-describing).

## Requirements

- Apple Silicon (MLX)
- `ffmpeg` on `PATH` (used by mlx-video for encoding/muxing)
- The distilled model weights are downloaded on first use from
  `prince-canuma/LTX-2-distilled` (~107 GB). They are cached by Hugging Face.

## Run

```sh
uv run mlx-video-server
# or
uv run python -m mlx_video_server
```

On startup the server loads the model and runs a tiny warmup generation (compiles
MLX kernels) so the first real request is fast. `GET /health` reports `warm`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MLX_VIDEO_MODEL_REPO` | `prince-canuma/LTX-2-distilled` | Model repo (must be the distilled MLX repo) |
| `MLX_VIDEO_TEXT_ENCODER_REPO` | (unset) | Override text encoder repo |
| `MLX_VIDEO_FILES_ROOT` | `~/.mlx-video-server/files` | Where artifacts are stored |
| `MLX_VIDEO_HOST` / `MLX_VIDEO_PORT` | `127.0.0.1` / `8800` | Bind address |
| `MLX_VIDEO_AUTH_TOKEN` | (unset) | If set, all `/v1/*` routes require `Authorization: Bearer <token>` |
| `MLX_VIDEO_WARMUP` | `1` | Warm up kernels at startup |
| `MLX_VIDEO_MAX_NUM_FRAMES` | `161` | Reject larger requests (≈6.7s @ 24fps) |
| `MLX_VIDEO_MAX_WIDTH` / `MLX_VIDEO_MAX_HEIGHT` | `768` / `768` | Resolution caps |

## API

### Generate (sync) — returns the mp4

```
POST /v1/generate   (multipart/form-data)
```

Parts: `params` (JSON string, required), optional `image`, `end_image`, `audio` files.
Responds with `video/mp4` and an `X-File-Id` header. The artifact is also persisted
(retrievable later via the files API).

`params` (only knobs that affect the distilled pipeline):

```jsonc
{
  "prompt": "...",            // required
  "width": 512,               // multiple of 64, <= MAX_WIDTH
  "height": 512,              // multiple of 64, <= MAX_HEIGHT
  "num_frames": 97,           // 1 + 8*k, <= MAX_NUM_FRAMES
  "fps": 24,
  "seed": null,               // null => random (the chosen seed is recorded)
  "image_strength": 1.0,      // 0.0–1.0 (I2V); lower = looser to the input frame
  "end_image_strength": null, // defaults to image_strength
  "generate_audio": false     // generate audio (mutually exclusive with an `audio` file)
}
```

Example:

```sh
curl -s -o out.mp4 -D - http://127.0.0.1:8800/v1/generate \
  -F 'params={"prompt":"Two dogs wearing sunglasses, cinematic, sunset","num_frames":49}' \
  -F 'image=@first_frame.png'
```

### Generate (async)

```
POST   /v1/jobs           -> 202 {job_id, status, queue_position, mode}
GET    /v1/jobs/{job_id}  -> {status, queue_position?, file_id?, error?, timings?}
GET    /v1/jobs           -> recent/active jobs
DELETE /v1/jobs/{job_id}  -> cancel a still-queued job (409 if already running)
```

When a job reaches `completed`, use its `file_id` with the files API.

### Files

```
GET    /v1/files               -> [{file_id, mode, prompt, params, has_audio, video_bytes, created_at, timings}]
GET    /v1/files/{file_id}     -> mp4 download
GET    /v1/files/{file_id}/audio -> wav (only when audio was produced)
DELETE /v1/files/{file_id}     -> delete the artifact directory
```

### Health

```
GET /health -> {status, warm, queue_len, model_repo}
```

### Help (for LLM agents)

```
GET /help -> machine-readable usage guide (public, no auth)
```

A self-describing JSON document: endpoints, parameters (with the live caps),
the input→mode mapping, validation rules, and practical know-how learned from
benchmarking (e.g. distilled has no negative guidance; lower `image_strength`
to let an action happen in I2V; "looking at the camera" tends to induce zoom —
use "looking ahead"; vary `seed` for composition). Point an agent at `/help`
before it constructs requests.

## Validation

Bad requests are rejected with `422` before they reach the model: `width`/`height`
must be multiples of 64 and within caps; `num_frames` must be `1 + 8*k` and within
the cap; an `audio` file (A2V) and `generate_audio` cannot be combined.

## Storage layout

```
${files_root}/{file_id}/
  ├── video.mp4
  ├── audio.wav          # only when audio was produced
  ├── input_image.*      # saved inputs (provenance)
  ├── input_end_image.*
  ├── input_audio.*
  └── metadata.json      # prompt, params (incl. resolved seed), mode, timings
```

## Notes / roadmap

- v1 calls mlx-video's `generate_video()` per request; weights stay in the OS/HF
  cache and MLX kernel cache, so per-call overhead is just the load (~seconds),
  not the first-run compile. A future optimization is to hold the loaded pipeline
  resident to drop even that load cost.
