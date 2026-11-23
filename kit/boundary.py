"""The human gate at the system boundary.

Any tool whose effect leaves this process — messaging a patient, writing to an EHR or
scheduling system, posting a PR comment, provisioning cloud resources — is declared
`boundary="external"`. The registry will not execute it without an `Approval` that matches
the exact call: same tool, same tool_use_id, same input digest. A model cannot talk its
way past this; it is a code path, not a prompt instruction.

Every decision lands in an append-only ledger that records *who decided what about which
call* and never the call's content — the input is stored only as a digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from kit import tracing
from kit.config import RUNS_DIR

Boundary = Literal["local", "external"]


def digest(raw_input: dict) -> str:
    """Stable digest of a tool input, so an approval binds to the exact call approved."""
    canonical = json.dumps(raw_input, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class Approval(BaseModel):
    tool_use_id: str
    tool: str
    decision: Literal["approved", "denied"]
    approver: str
    input_digest: str

    def authorizes(self, tool: str, tool_use_id: str, raw_input: dict) -> bool:
        return (
            self.decision == "approved"
            and self.tool == tool
            and self.tool_use_id == tool_use_id
            and self.input_digest == digest(raw_input)
        )


def standing_approval(tool: str, tool_use_id: str, raw_input: dict, *, policy: str) -> Approval:
    """A pre-authorized approval for one narrowly-defined action, named after the policy that
    authorizes it and recorded in the ledger like any other decision.

    Use sparingly. The gate exists to keep humans in the loop; the only action that should ever
    be pre-approved is one that *hands work to* a human (e.g. escalating a crisis to clinicians),
    where waiting for approval would itself be the harm.
    """
    return Approval(
        tool_use_id=tool_use_id,
        tool=tool,
        decision="approved",
        approver=f"policy:{policy}",
        input_digest=digest(raw_input),
    )


class Ledger:
    """Append-only. There is deliberately no update or delete method."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.getenv("KIT_LEDGER", RUNS_DIR / "ledger.jsonl"))

    def append(self, approval: Approval, *, teardown: str | None = None) -> None:
        run = tracing.current_run()
        entry = {
            "ts": time.time(),
            "kind": "approval",
            "run": run.name if run else None,
            **approval.model_dump(),
        }
        if teardown is not None:
            entry["teardown"] = teardown  # required for durable cloud artifacts; engineer-written
        tracing.write_jsonl(self.path, entry)
        tracing.record("approval", **approval.model_dump())

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines()]
