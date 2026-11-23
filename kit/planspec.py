"""A plan as data: what the plan skill writes, what the UI draws, what `make graph` checks against.

A hand-typed Mermaid diagram in a plan drifts from the code the moment the code is written. A
PlanSpec is the same diagram as a pydantic model, so it can be drawn before any code exists and
compared node-for-node with the compiled LangGraph graph afterwards (`drift`).

    spec = PlanSpec.load("plans/my_flow.spec.json")
    print(spec.mermaid())
    print(drift(spec, compiled.get_graph()))
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeKind = Literal["model", "code", "gate", "retrieval"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanNode(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="The LangGraph node name it will compile to.")
    kind: NodeKind
    label: str = ""
    role: str | None = None  # kit.llm.Role value for model nodes
    tools: list[str] = []
    note: str = ""  # one line: what it decides or does


class PlanEdge(_Strict):
    src: str
    dst: str
    label: str = ""  # routing condition; empty = always runs


class NeverEvent(_Strict):
    id: str
    text: str
    eval_case: str | None = None  # the eval case id that proves it can't happen


class PlanSpec(_Strict):
    flow: str
    ask: str
    based_on: str | None = None  # nearest existing flow it adapts
    nodes: list[PlanNode]
    edges: list[PlanEdge]
    never_events: list[NeverEvent] = []
    phi: bool = False  # touches patient / personal data
    regulated: bool = False  # HIPAA / HITRUST / SOC2-style evidence path required

    @classmethod
    def load(cls, path: str | Path) -> PlanSpec:
        return cls.model_validate_json(Path(path).read_text())

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(json.dumps(self.model_dump(), indent=2) + "\n")
        return p

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}

    def problems(self) -> list[str]:
        """Structural errors a plan must not be approved with."""
        out, ids = [], self.node_ids()
        terminals = {"__start__", "__end__", "start", "end"}
        for e in self.edges:
            for end in (e.src, e.dst):
                if end not in ids and end not in terminals:
                    out.append(f"edge {e.src}->{e.dst}: unknown node {end!r}")
        if len(ids) != len(self.nodes):
            out.append("duplicate node ids")
        if self.phi and not self.regulated:
            out.append("phi=True requires regulated=True (run the phi-check skill)")
        out += [f"never-event {n.id} has no eval case" for n in self.never_events if not n.eval_case]
        return out

    def mermaid(self) -> str:
        """Same visual language as `make graph`: model steps rounded, gates amber hexagons."""
        shapes = {
            "model": ('(["', '"])'),
            "code": ('["', '"]'),
            "gate": ('{{"', '"}}'),
            "retrieval": ('[("', '")]'),
        }
        alias = {"start": "__start__", "end": "__end__"}
        lines = ["flowchart TD", '    __start__(["start"]):::terminal', '    __end__(["end"]):::terminal']
        for n in self.nodes:
            o, c = shapes[n.kind]
            lines.append(f"    {n.id}{o}{n.label or n.id.replace('_', ' ')}{c}:::{n.kind}")
        for e in self.edges:
            src, dst = alias.get(e.src, e.src), alias.get(e.dst, e.dst)
            arrow = f"-.->|{e.label}|" if e.label else "-->"
            lines.append(f"    {src} {arrow} {dst}")
        lines += [
            "    classDef model fill:#e0e7ff,stroke:#4f46e5,color:#111827",
            "    classDef code fill:#f3f4f6,stroke:#374151,color:#111827",
            "    classDef retrieval fill:#dcfce7,stroke:#15803d,color:#111827",
            "    classDef gate fill:#fde68a,stroke:#b45309,stroke-width:3px,color:#111827",
            "    classDef terminal fill:#e5e7eb,stroke:#6b7280,color:#111827",
        ]
        return "\n".join(lines)


def drift(spec: PlanSpec, graph) -> dict[str, list[str]]:
    """Planned vs built. `graph` is `compiled.get_graph()` (anything with .nodes and .edges)."""
    built_nodes = {n for n in graph.nodes if not n.startswith("__")}
    planned = spec.node_ids()
    alias = {"start": "__start__", "end": "__end__"}
    planned_edges = {(alias.get(e.src, e.src), alias.get(e.dst, e.dst)) for e in spec.edges}
    built_edges = {(e.source, e.target) for e in graph.edges}
    return {
        "missing_nodes": sorted(planned - built_nodes),
        "unplanned_nodes": sorted(built_nodes - planned),
        "missing_edges": sorted(f"{a}->{b}" for a, b in planned_edges - built_edges),
        "unplanned_edges": sorted(f"{a}->{b}" for a, b in built_edges - planned_edges),
    }


def main(argv: list[str] | None = None) -> int:
    """python -m kit.planspec check|mermaid|drift <spec.json>"""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m kit.planspec")
    ap.add_argument("cmd", choices=["check", "mermaid", "drift"])
    ap.add_argument("spec", type=Path)
    args = ap.parse_args(argv)
    spec = PlanSpec.load(args.spec)
    if args.cmd == "mermaid":
        print(spec.mermaid())
        return 0
    if args.cmd == "check":
        problems = spec.problems()
        for p in problems:
            print(f"problem: {p}")
        kinds = {k: sum(n.kind == k for n in spec.nodes) for k in ("model", "code", "gate", "retrieval")}
        print(
            f"{spec.flow}: {len(spec.nodes)} nodes {kinds}, {len(spec.edges)} edges, "
            f"{len(spec.never_events)} never-events, phi={spec.phi}, regulated={spec.regulated}"
        )
        return 1 if problems else 0
    from kit.cli import capture_graph  # deferred: needs the flow to exist

    d = drift(spec, capture_graph(spec.flow).get_graph())
    for k, items in d.items():
        print(f"{k}: {', '.join(items) or '-'}")
    return 1 if any(d.values()) else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
