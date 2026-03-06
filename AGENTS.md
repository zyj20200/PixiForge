# Repository Guidelines

## Project Structure & Module Organization
`app/` contains the FastAPI backend; most server logic currently lives in `app/main.py`. `static/` holds the frontend served by FastAPI: `static/index.html` for markup, `static/js/app.js` for the Vue 3 client, and `static/css/style.css` for styles. Runtime data is stored under `data/`: `projects/` for per-project JSON and frames, `uploads/` for user files, and `outputs/` for rendered GIF/MP4 files. Treat `data/` as generated state, not source.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate` — create and activate a local virtual environment.
- `pip install -r requirements.txt` — install FastAPI, HTTP, image, and env dependencies.
- `cp .env.example .env` — create local configuration; set `AI_API_KEY` before testing AI flows.
- `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` — run the app locally with auto-reload.
- `python -m compileall app` — quick syntax sanity check for backend changes.

## Coding Style & Naming Conventions
Follow the existing minimal style rather than introducing new tooling. Use 4-space indentation in Python and 2-space indentation in frontend files. Prefer type hints in Python, `snake_case` for functions and variables, and `UPPER_SNAKE_CASE` for module-level constants. In JavaScript, use `camelCase` for functions, refs, and helpers; keep async API helpers centralized instead of duplicating fetch logic.

## Testing Guidelines
There is no automated test suite yet. For backend changes, run `python -m compileall app` and then start `uvicorn` for manual API checks. For UI changes, verify the full 5-step flow in the browser, especially storyboard editing, first-frame selection/upload, frame generation, and video export. If you add tests, place them in a new `tests/` package and prefer `pytest` with files named `test_*.py`.

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit prefixes such as `feat:`. Continue with concise messages like `fix: handle missing first frame` or `docs: clarify env setup`. Pull requests should summarize user-visible changes, list manual verification steps, link related issues, and include screenshots or short recordings for frontend updates.

## Security & Configuration Tips
Never commit real secrets in `.env`. Keep `AI_API_KEY` local, and document any new environment variables in `.env.example` and `README.md`. Avoid committing generated media from `data/` unless it is intentional sample data for review.
