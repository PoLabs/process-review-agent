"""Human approval as a LangGraph interrupt.

`approval_node` pauses the graph whenever the model asks for an external tool. The
interrupt payload lists the pending calls; the human (or an eval policy) resumes with a
decision per tool_use_id. Anything not explicitly approved is denied — fail closed.

The interrupt payload carries tool inputs so the reviewer can see what they are approving.
It lives only in the in-memory checkpoint; the ledger stores a digest.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.types import Command, interrupt

from kit.boundary import Approval, Ledger, digest
from kit.tools.registry import ToolRegistry

Decider = Callable[[list[dict]], dict[str, dict]]


def pending_external(content: list[dict], registry: ToolRegistry) -> list[dict]:
    return [
        {"tool_use_id": b["id"], "tool": b["name"], "input": b["input"]}
        for b in content
        if b["type"] == "tool_use" and registry.is_external(b["name"])
    ]


def request_approvals(calls: list[dict], ledger: Ledger) -> dict[str, Approval]:
    """Pause the graph for a human decision on each call; return Approvals keyed by tool_use_id.

    Call from inside any node. On first execution `interrupt()` suspends the graph; on resume it
    returns the decisions and this function records them. Anything not explicitly approved is
    denied — fail closed. Keep everything before this call deterministic: LangGraph re-runs the
    node from the top on resume.
    """
    if not calls:
        return {}
    reply = interrupt({"type": "approval_request", "calls": calls})
    # Decisions arrive wrapped as {"decisions": {...}}. A bare dict would be ambiguous: LangGraph
    # reads a dict whose keys all look like interrupt ids as a resume *map* — and an empty dict
    # passes that test vacuously, so "nobody answered" would re-interrupt forever.
    decisions: dict[str, dict] = reply.get("decisions", {}) if isinstance(reply, dict) else {}
    # Which actions the approver was asked about, by tool and digest (the same digest the ledger
    # and the approval bind to). Not the inputs: those can hold patient text.
    from kit import tracing

    tracing.record(
        "gate",
        node="approval",
        n_calls=len(calls),
        evidence=[
            {"kind": "approval", "id": f"{c['tool']}:{c['tool_use_id']}", "sha256": digest(c["input"])}
            for c in calls
        ],  # fmt: skip
    )
    approvals: dict[str, Approval] = {}
    for call in calls:
        d = decisions.get(call["tool_use_id"], {})
        approval = Approval(
            tool_use_id=call["tool_use_id"],
            tool=call["tool"],
            decision="approved" if d.get("decision") == "approved" else "denied",
            approver=str(d.get("approver", "unknown")),
            input_digest=digest(call["input"]),
        )
        ledger.append(approval)
        approvals[call["tool_use_id"]] = approval
    return approvals


def make_approval_node(registry: ToolRegistry, ledger: Ledger | None = None):
    """Node for the react pattern: approve the external tool_use blocks of the last model turn."""
    ledger = ledger or Ledger()

    def approve(state: dict) -> dict:
        calls = pending_external(state["messages"][-1]["content"], registry)
        approvals = request_approvals(calls, ledger)
        return {"approvals": {k: a.model_dump() for k, a in approvals.items()}}

    return approve


# --- deciders: who answers the interrupt -------------------------------------------------


def approve_all(approver: str = "eval-policy") -> Decider:
    return lambda calls: {c["tool_use_id"]: {"decision": "approved", "approver": approver} for c in calls}


def deny_all(approver: str = "eval-policy") -> Decider:
    return lambda calls: {c["tool_use_id"]: {"decision": "denied", "approver": approver} for c in calls}


def console(approver: str) -> Decider:
    """Interactive reviewer for demos. Shows the call in the terminal only — never written to disk."""

    def decide(calls: list[dict]) -> dict[str, dict]:
        out = {}
        for c in calls:
            print(f"\n[approval needed] {c['tool']}  {c['input']}")
            ok = input("approve? [y/N] ").strip().lower() == "y"
            out[c["tool_use_id"]] = {"decision": "approved" if ok else "denied", "approver": approver}
        return out

    return decide


def run_with_approvals(
    graph, inputs: dict, decide: Decider, config: dict[str, Any], *, max_rounds: int = 20
) -> dict:
    """Invoke a graph, answering each approval interrupt with `decide`, until it finishes.
    Bounded: an approval loop that never settles is a bug, and it fails loudly."""
    result = graph.invoke(inputs, config)
    for _ in range(max_rounds):
        if not result.get("__interrupt__"):
            return result
        request = result["__interrupt__"][0].value
        result = graph.invoke(Command(resume={"decisions": decide(request["calls"])}), config)
    raise RuntimeError(f"approval loop did not settle after {max_rounds} rounds")
