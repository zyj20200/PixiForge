# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

- Create venv: `python -m venv .venv`
- Activate venv (Linux/macOS): `source .venv/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Start dev server: `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
- Open app: `http://127.0.0.1:8000`
- Health check: `curl http://127.0.0.1:8000/api/health`

### Environment setup

1. Copy env file: `cp .env.example .env` (Windows: `copy .env.example .env`)
2. Set `AI_API_KEY` in `.env` (required)
3. Optional overrides in `.env`: `AI_BASE_URL`, `AI_CHAT_MODEL`, `AI_IMAGE_MODEL`, `AI_IMAGE_EDIT_MODEL`

## Testing and linting status

- There is currently no configured automated test suite in this repository.
- There are currently no configured lint/format/type-check commands in this repository.

## Architecture overview

### Stack and entrypoints

- Backend: FastAPI app in `app/main.py`.
- Frontend: static SPA served by FastAPI (`static/index.html`, `static/js/app.js`, `static/css/style.css`).
- App root route (`/`) serves `static/index.html`; API and static/project file routes are served from the same process.

### End-to-end workflow model (5-step pipeline)

The product is implemented as a strict stateful pipeline:

1. Create project (`POST /api/projects`) with scene/style/fps/duration.
2. Generate storyboard via LLM (`POST /api/projects/{pid}/storyboard/generate`).
3. Set first frame (AI generate or upload).
4. Generate subsequent frames in background (`POST /api/projects/{pid}/generate-frames`).
5. Render output video (`POST /api/projects/{pid}/render-video`).

Frontend step navigation in `static/js/app.js` mirrors backend project statuses (`draft`, `storyboard_ready`, `first_frame_ready`, `generating_frames`, `frames_ready`, `rendering`, `completed`, `failed`).

### Data persistence model

- Runtime state is kept in-memory in `projects` (protected by `projects_lock`) in `app/main.py`.
- Durable state is JSON-on-disk under `data/projects/<pid>/project.json`.
- On startup, FastAPI lifespan loads all saved projects from disk back into memory.
- Generated assets:
  - Project-local frames and first frame: `data/projects/<pid>/...`
  - Final outputs: `data/outputs/<pid>.mp4` or `<pid>.gif`
  - Temporary uploads/edit files: `data/uploads/`

This means the app is single-process/single-node oriented; task coordination is not distributed.

### AI integration details

`app/main.py` wraps OpenAI-compatible endpoints via `httpx`:

- Chat: `/v1/chat/completions` for storyboard JSON generation.
- Text-to-image: `/v1/images/generations` for first frame creation.
- Image edit: `/v1/images/edits` for frame-to-frame generation.

Important implementation detail: storyboard generation requests strict JSON and then normalizes frame count to the project’s `frame_count` (truncates or pads).

### Frame generation and rendering

- Frame generation runs as a FastAPI `BackgroundTasks` job (`run_frame_generation`).
- Frame 1 is copied from `first_frame.jpg`; each subsequent frame is produced from the previous frame with `edit_prompt` plus consistency hints.
- Progress fields (`generation_current`, `generation_total`, `generation_progress`, `generation_message`) are updated on every frame and polled by frontend every ~2.5s.
- Rendering prefers ffmpeg (MP4 via `libx264`); falls back to Pillow GIF when ffmpeg is unavailable/fails.

### Frontend structure

- `static/index.html` contains a single Vue template with 5 visible step panels.
- `static/js/app.js` contains all UI state and API orchestration in one Vue app:
  - step transitions
  - storyboard editing
  - first-frame upload/generation
  - polling long-running frame generation
  - rendering trigger and history management
- Project history is loaded from `/api/projects` and can restore UI state mid-pipeline.

## Operational notes

- `/project-files`, `/uploads`, `/outputs`, `/static` are mounted as static routes directly from local disk.
- Deleting a project via `DELETE /api/projects/{pid}` removes in-memory state, project directory, and matching output files.
- ffmpeg availability changes output type and completion message semantics (MP4 vs GIF fallback).
