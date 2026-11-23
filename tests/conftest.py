"""Test isolation: no test writes traces or ledger entries into the repo's real runs/ directory."""

import pytest

from kit import boundary, tracing


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(boundary, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.delenv("KIT_LEDGER", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)  # memory stays in-process: no test reaches a Redis
