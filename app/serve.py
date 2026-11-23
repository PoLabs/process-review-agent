"""One process for a cloud deploy: the built React app at /, the API at /api.

    SITE_PASSWORD=... uvicorn app.serve:app --host 0.0.0.0 --port $PORT

The React app already calls /api/... (in development Vite forwards those to the API), so served
from the same origin it needs no change and no CORS. Run ONE instance with one worker: review
runs, pending sign-offs and archived reports live in this process's memory.

The whole site sits behind one shared password (HTTP Basic: the browser asks once, any username).
It fails closed: with SITE_PASSWORD unset, everything but the health check answers 503. Leave
KIT_API_TOKEN unset here, because the API would read the browser's Basic header as a wrong token.
This keeps strangers off the model key; it is not per-person identity for sign-offs.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api import app as api

DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
OPEN_PATHS = frozenset({"/api/healthz"})  # the host's health check; it returns only {"ok": true}


def password_ok(header: bytes, password: str) -> bool:
    """True if an Authorization header carries this password, under any username."""
    scheme, _, value = header.partition(b" ")
    if scheme.lower() != b"basic":
        return False
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    _, colon, sent = decoded.partition(b":")
    return bool(colon) and hmac.compare_digest(sent, password.encode())


class SitePassword:
    """Every HTTP request needs the shared password, read per request from SITE_PASSWORD."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.inner(scope, receive, send)
            return
        password = os.getenv("SITE_PASSWORD", "")
        if not password:
            refused = PlainTextResponse("SITE_PASSWORD is not set on the server.", status_code=503)
        elif password_ok(dict(scope["headers"]).get(b"authorization", b""), password):
            await self.inner(scope, receive, send)
            return
        else:
            refused = PlainTextResponse(
                "Password required.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Process Review Desk", charset="UTF-8"'},
            )
        await refused(scope, receive, send)


site = FastAPI(title="Process Review Desk", docs_url=None, redoc_url=None, openapi_url=None)
site.mount("/api", api)
if DIST.is_dir():  # absent when the image was built without the web stage: the API still serves
    site.mount("/", StaticFiles(directory=DIST, html=True), name="web")

app = SitePassword(site)
