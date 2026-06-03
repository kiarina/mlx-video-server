"""Machine-readable usage guide served at ``GET /help``.

Aimed at LLM agents: it describes the endpoints, the parameters (with the
distilled-pipeline constraints), the input->mode mapping, and the practical
know-how discovered while benchmarking LTX-2. Caps/defaults are filled in from
the live ``Settings`` so the guide never drifts from the running config.
"""

from . import __version__
from .config import Settings


def help_payload(settings: Settings) -> dict:
    return {
        "service": "mlx-video-server",
        "version": __version__,
        "summary": (
            "Generate short videos from text, an image, and/or audio using the "
            "LTX-2 distilled pipeline (Apple Silicon / MLX). One generation runs "
            "at a time behind a queue; the model stays resident."
        ),
        "model": {
            "repo": settings.model_repo,
            "pipeline": "LTX-2 distilled (two-stage, no CFG, ~11 internal steps)",
            "note": (
                "Distilled is fast but has NO classifier-free guidance: "
                "negative prompts and 'do not ...' / 'no zoom' style instructions "
                "are ignored. Describe what you WANT, not what to avoid."
            ),
        },
        "modes": {
            "_how": "Mode is inferred from which files you attach; you don't set it.",
            "T2V": "prompt only",
            "I2V": "attach `image` (used as the first frame)",
            "I2V(first+last)": "attach both `image` and `end_image`",
            "I2V(last)": "attach `end_image` only",
            "A2V": "attach `audio` (drives motion/timing; mixed into the output mp4)",
            "A2V+I2V": "attach both `image` and `audio`",
            "T2V+Audio": "set params.generate_audio=true (generate audio; no audio file)",
        },
        "endpoints": [
            {
                "method": "POST",
                "path": "/v1/generate",
                "kind": "sync",
                "request": "multipart/form-data",
                "returns": "video/mp4 (blocks until done); header X-File-Id, X-Mode",
                "desc": "Generate and return the mp4 in one call. Also persisted (see files API).",
            },
            {
                "method": "POST",
                "path": "/v1/jobs",
                "kind": "async",
                "request": "multipart/form-data",
                "returns": "202 {job_id, status, queue_position, mode}",
                "desc": "Enqueue a generation; poll the job, then fetch by file_id.",
            },
            {"method": "GET", "path": "/v1/jobs/{job_id}", "returns": "job status incl. file_id when completed"},
            {"method": "GET", "path": "/v1/jobs", "returns": "recent/active jobs"},
            {"method": "DELETE", "path": "/v1/jobs/{job_id}", "returns": "cancel a queued job (409 if running)"},
            {"method": "GET", "path": "/v1/files", "returns": "list artifacts with metadata"},
            {"method": "GET", "path": "/v1/files/{file_id}", "returns": "download mp4"},
            {"method": "GET", "path": "/v1/files/{file_id}/audio", "returns": "download wav (if audio produced)"},
            {"method": "DELETE", "path": "/v1/files/{file_id}", "returns": "delete the artifact"},
            {"method": "GET", "path": "/health", "returns": "{status, warm, queue_len, model_repo}"},
        ],
        "multipart_parts": {
            "params": "JSON string (required) — see `params` below",
            "image": "file (optional) — first-frame conditioning (I2V)",
            "end_image": "file (optional) — last-frame conditioning",
            "audio": "file (optional) — A2V driving audio",
        },
        "params": {
            "prompt": {"type": "string", "required": True, "desc": "What to generate. Concrete, comma-separated descriptors work best."},
            "width": {"type": "int", "default": settings.default_width, "constraint": "multiple of 64", "max": settings.max_width},
            "height": {"type": "int", "default": settings.default_height, "constraint": "multiple of 64", "max": settings.max_height},
            "num_frames": {"type": "int", "default": settings.default_num_frames, "constraint": "1 + 8*k (e.g. 33, 49, 97, 161)", "max": settings.max_num_frames, "desc": "duration_seconds = num_frames / fps"},
            "fps": {"type": "int", "default": settings.default_fps, "desc": "Output frame rate; does not change generation time."},
            "seed": {"type": "int|null", "default": None, "desc": "null => random; the chosen seed is recorded in metadata. Vary it to change composition."},
            "image_strength": {"type": "float", "default": 1.0, "range": "0.0-1.0", "desc": "I2V only. 1.0 locks tightly to the input frame (little change); lower (~0.7) loosens it so larger changes can occur."},
            "end_image_strength": {"type": "float|null", "default": None, "desc": "Defaults to image_strength."},
            "generate_audio": {"type": "bool", "default": False, "desc": "Generate synchronized audio. Mutually exclusive with an `audio` file."},
        },
        "constraints": [
            "width and height must be positive multiples of 64 (rejected with 422 otherwise).",
            "num_frames must be 1 + 8*k and <= max_num_frames (rejected with 422 otherwise).",
            "An `audio` file (A2V) and generate_audio=true cannot be combined.",
        ],
        "performance": {
            "engine": "LTX-2 distilled",
            "approx": "512x512: ~40s-1.5min depending on num_frames; 256x256/9f: ~20-30s.",
            "peak_memory": "~37 GB unified memory",
            "first_request": "Slower if kernels aren't warm yet; check GET /health -> warm. Warmup runs at startup by default.",
            "caps": {
                "max_num_frames": settings.max_num_frames,
                "max_width": settings.max_width,
                "max_height": settings.max_height,
            },
        },
        "know_how": [
            {
                "topic": "No negative guidance in distilled",
                "tip": "Suppression phrases ('static camera', 'no zoom', 'don't ...') are ignored because distilled has no CFG. Steer by describing the desired result and by choosing parameters, not by forbidding things in text.",
            },
            {
                "topic": "I2V image_strength controls how much can change",
                "tip": "At strength 1.0 the subject barely changes from the input frame. To make a clear action happen (e.g. a character opening closed eyes), lower image_strength to ~0.7.",
            },
            {
                "topic": "'looking at the camera' induces zoom",
                "tip": "Camera-gaze phrasing tends to trigger an unwanted push-in/zoom in distilled. If you want the subject to look forward without zooming, write 'looking ahead' instead of 'looking at the camera'.",
            },
            {
                "topic": "Use seed sweeps for composition",
                "tip": "Framing/zoom/pose vary with the seed. If the first result's composition is off, retry with a few different seeds (each is cheap, tens of seconds) rather than over-engineering the prompt.",
            },
            {
                "topic": "Frame count & duration",
                "tip": "num_frames must be 1+8*k. Common values: 33 (~1.4s), 49 (~2s), 97 (~4s), 161 (~6.7s) at 24fps. duration = num_frames / fps.",
            },
            {
                "topic": "Resolution sweet spot",
                "tip": "512x512 is a good balance of quality and speed. Larger resolutions are slower; keep both width and height multiples of 64.",
            },
            {
                "topic": "Audio-to-video (A2V)",
                "tip": "Attach an `audio` file to drive motion/timing; it is auto-trimmed to the clip length and mixed into the output mp4. Use it instead of generate_audio (they are exclusive).",
            },
        ],
        "examples": [
            {
                "title": "Text-to-video (sync)",
                "curl": (
                    "curl -s -o out.mp4 -D - http://HOST:PORT/v1/generate "
                    "-F 'params={\"prompt\":\"a calm ocean at sunset, cinematic\",\"num_frames\":97}'"
                ),
            },
            {
                "title": "Image-to-video: make a portrait open its eyes (async)",
                "curl": (
                    "curl -s -X POST http://HOST:PORT/v1/jobs "
                    "-F 'params={\"prompt\":\"the character slowly opens her eyes, looking ahead, gentle smile\",\"num_frames\":49,\"image_strength\":0.7}' "
                    "-F 'image=@portrait.png'"
                ),
                "note": "Then GET /v1/jobs/{job_id} until completed, and GET /v1/files/{file_id}.",
            },
            {
                "title": "Audio-to-video",
                "curl": (
                    "curl -s -o out.mp4 http://HOST:PORT/v1/generate "
                    "-F 'params={\"prompt\":\"a singer performing on stage, expressive face\",\"num_frames\":97}' "
                    "-F 'audio=@song.wav'"
                ),
            },
        ],
    }
