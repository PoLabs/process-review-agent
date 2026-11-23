"""One process for a cloud deploy: the built React app at /, the API at /api.

    uvicorn app.serve:app --host 0.0.0.0 --port $PORT

The React app already calls /api/... (in development Vite forwards those to the API), so served
from the same origin it needs no change and no CORS. Run ONE instance with one worker: review
runs, pending sign-offs and archived reports live in this process's memory.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import app as api

DIST = Path(__file__).resolve().parents[1] / "web" / "dist"

app = FastAPI(title="Process Review Desk", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/api", api)
if DIST.is_dir():  # absent when the image was built without the web stage: the API still serves
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
