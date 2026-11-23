"""The shared site password: nothing but the health check is reachable without it."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.serve import app

client = TestClient(app)
PASSWORD = "test-only:pässword"  # a colon and non-ASCII must both survive


def basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", PASSWORD)


@pytest.mark.parametrize("path", ["/", "/index.html", "/api/process/examples", "/api/runs", "/api/audit"])
def test_every_read_needs_the_password(locked, path):
    resp = client.get(path)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize("path", ["/api/process/ask", "/api/process/review", "/api/process/extract"])
def test_every_write_needs_the_password(locked, path):
    assert client.post(path, json={}).status_code == 401


@pytest.mark.parametrize("user", ["", "anyone"])
def test_the_right_password_gets_in_under_any_username(locked, user):
    assert client.get("/api/process/examples", headers=basic(user, PASSWORD)).status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        basic("anyone", "wrong")["Authorization"],
        basic("anyone", "")["Authorization"],
        basic(PASSWORD, "x")["Authorization"],
        "Basic not-base64!",
        "Basic " + base64.b64encode(b"no-separator").decode(),
        "Bearer test-only-token",
        "",
    ],
)
def test_anything_else_is_refused(locked, header):
    assert client.get("/api/runs", headers={"Authorization": header}).status_code == 401


def test_it_fails_closed_when_the_variable_is_missing(monkeypatch):
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    assert client.get("/api/process/examples").status_code == 503
    assert client.get("/api/process/examples", headers=basic("a", "")).status_code == 503


def test_an_empty_variable_also_fails_closed(monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "")
    assert client.get("/api/runs", headers=basic("a", "")).status_code == 503


def test_the_health_check_stays_open_and_says_nothing(monkeypatch):
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    resp = client.get("/api/healthz")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
