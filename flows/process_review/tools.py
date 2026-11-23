"""The one external action in this flow: a human approver's sign-off, and the archive it lands in.

`sign_off_process` is boundary="external": it executes only with an Approval bound to this exact
call (role, report digest, recommendation), so the agent cannot sign anything off by itself. The
archive is write-once and keyed by sha256, so the submission and the report a person signed can
be found again from the digests in the decision record. Here it is in-process; in a deployment
it is a WORM store with 6-year retention, owned by records management (README).
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from flows.process_review.state import ApproverRole, Recommendation, Strict
from kit.tools.registry import ToolRegistry


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class Archive:
    """Write-once, content-addressed. put() of the same content is idempotent; nothing is ever
    overwritten or deleted through this interface."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}
        self.signoffs: list[dict] = []
        self._lock = threading.Lock()

    def put(self, obj: Any) -> str:
        raw = json.dumps(obj, sort_keys=True, default=str)
        sha = hashlib.sha256(raw.encode()).hexdigest()
        with self._lock:
            self._items.setdefault(sha, raw)
        return sha

    def get(self, sha: str) -> Any:
        return json.loads(self._items[sha])

    def __contains__(self, sha: str) -> bool:
        return sha in self._items


# The archive the app shares across runs: an approver's screen looks up the exact report it is
# asked to sign by the digest in the pending sign-off call (GET /process/reports/{digest}).
ARCHIVE = Archive()


class SignOff(Strict):
    report_digest: str
    role: ApproverRole
    recommendation: Recommendation


def build_registry(archive: Archive) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool(boundary="external")
    def sign_off_process(args: SignOff) -> dict:
        """Record a human approver's sign-off on the exact report they were shown. Runs only after
        that approver said yes to this call; the agent never calls it on its own judgment."""
        if args.report_digest not in archive:
            raise ValueError("no archived report with that digest")
        archive.signoffs.append(args.model_dump())
        return {"recorded": True, "role": args.role}

    return registry
