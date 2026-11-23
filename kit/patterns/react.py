"""Tool-calling loop as a LangGraph graph:

    agent -> [approve] -> tools -> agent ... -> END

LangGraph owns state, routing, step limits, interrupts and checkpointing; `kit.llm` owns
the model call. Content blocks are appended back exactly as the API returned them
(thinking blocks included), because editing earlier turns invalidates them.

If the model asks for any `boundary="external"` tool, the turn routes through `approve`
first — a LangGraph interrupt that waits for a human decision (see kit.patterns.approval).
Local-only turns go straight to `tools`.

No PII on disk: graphs with external tools need a checkpointer for the interrupt, and the
only one accepted by default is the in-memory saver.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from kit import llm
from kit.boundary import Approval, Ledger
from kit.patterns.approval import make_approval_node, pending_external
from kit.tools.registry import ToolRegistry


class ReactState(TypedDict, total=False):
    messages: Annotated[list[dict], operator.add]
    steps: int
    approvals: dict[str, dict]  # tool_use_id -> Approval, for the current turn only
    stopped: str  # "done" | "max_steps"


def build(
    *,
    role: llm.Role,
    system: str,
    registry: ToolRegistry,
    tool_names: list[str] | None = None,
    max_steps: int = 8,
    name: str = "react",
    checkpointer=None,
    ledger: Ledger | None = None,
    allow_persistent_checkpointer: bool = False,
    terminal_tools: frozenset[str] = frozenset(),
):
    """`terminal_tools`: tools that END the loop once they succeed (a "submit your answer" tool).
    Without this the model is asked for another turn after submitting, and tends to submit again."""
    chosen = tool_names if tool_names is not None else registry.names()
    tools = registry.definitions(chosen)
    has_external = any(registry.is_external(n) for n in chosen)

    if has_external and checkpointer is None:
        checkpointer = InMemorySaver()  # interrupts need a checkpointer; memory keeps PII off disk
    if (
        checkpointer is not None
        and not isinstance(checkpointer, InMemorySaver)
        and not allow_persistent_checkpointer
    ):
        raise ValueError(
            "Checkpoints hold full conversation state. Only InMemorySaver is allowed by default "
            "(no PII on disk); pass allow_persistent_checkpointer=True for a reviewed, encrypted store."
        )

    def agent(state: ReactState) -> dict:
        result = llm.complete(
            role, system=system, messages=state["messages"], tools=tools, node=f"{name}.agent"
        )
        return {
            "messages": [{"role": "assistant", "content": result.content}],
            "steps": state.get("steps", 0) + 1,
        }

    def run_tools(state: ReactState) -> dict:
        last = state["messages"][-1]["content"]
        approvals = state.get("approvals") or {}
        # All results for one assistant turn go back in ONE user message (keeps parallel calls working).
        results = []
        for b in last:
            if b["type"] != "tool_use":
                continue
            raw = approvals.get(b["id"])
            approval = Approval.model_validate(raw) if raw else None
            results.append(registry.execute(b["name"], b["input"], tool_use_id=b["id"], approval=approval))
        return {"messages": [{"role": "user", "content": results}], "approvals": {}}

    def after_tools(state: ReactState) -> str:
        last = state["messages"][-2]["content"]  # the assistant turn whose tools just ran
        results = {r["tool_use_id"]: r for r in state["messages"][-1]["content"]}
        finished = any(
            b["type"] == "tool_use" and b["name"] in terminal_tools and not results[b["id"]].get("is_error")
            for b in last
        )
        return "done" if finished else "agent"

    def next_step(state: ReactState) -> str:
        content = state["messages"][-1]["content"]
        if not any(b["type"] == "tool_use" for b in content):
            return "done"
        if state.get("steps", 0) >= max_steps:
            return "max_steps"
        return "approve" if pending_external(content, registry) else "tools"

    def mark(reason: str):
        return lambda state: {"stopped": reason}

    g = StateGraph(ReactState)
    g.add_node("agent", agent)
    g.add_node("approve", make_approval_node(registry, ledger))
    g.add_node("tools", run_tools)
    g.add_node("done", mark("done"))
    g.add_node("max_steps", mark("max_steps"))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", next_step, ["approve", "tools", "done", "max_steps"])
    g.add_edge("approve", "tools")
    g.add_conditional_edges("tools", after_tools, ["agent", "done"])
    g.add_edge("done", END)
    g.add_edge("max_steps", END)
    return g.compile(checkpointer=checkpointer, name=name)


def final_text(state: ReactState) -> str:
    last = state["messages"][-1]
    if last["role"] != "assistant":
        return ""
    return "".join(b.get("text", "") for b in last["content"] if b["type"] == "text")


def tool_calls(state: ReactState) -> list[dict]:
    return [
        {"name": b["name"], "input": b["input"]}
        for m in state["messages"]
        if m["role"] == "assistant"
        for b in m["content"]
        if b["type"] == "tool_use"
    ]


def tool_results(state: ReactState) -> list[dict]:
    return [
        b
        for m in state["messages"]
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    ]
