"""Deterministic tools with typed inputs.

A tool is a plain Python function taking one pydantic model. The registry turns that model
into a strict JSON schema for the API and validates the model's arguments on the way back
in, so a malformed call becomes an `is_error` tool result the model can recover from —
never an exception that kills the graph.
"""

from __future__ import annotations

import inspect
import json
import time
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from kit import tracing
from kit.boundary import Approval, Boundary, digest

# Strict tool schemas reject numeric bounds (the API answers 400: "For 'integer' type, properties
# maximum, minimum are not supported"). They are moved into the description so the model still
# sees them, and pydantic still enforces them on the way back in — an out-of-range call becomes
# an is_error result the model can correct, not a request the API refuses.
_UNSUPPORTED_BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")


# Keys of these maps are NAMES (field names, definition names), not schema keywords. Treating
# them as keywords once deleted a field literally called `title` from a nested model: the schema
# still listed it as required, strict mode could never produce it, and 66 of 68 tool calls failed.
_NAME_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def _strict(schema: dict) -> dict:
    """Pydantic JSON schema -> API strict schema: no titles, closed objects, bounds as prose."""
    if isinstance(schema, list):
        return [_strict(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    bounds = {k: schema[k] for k in _UNSUPPORTED_BOUNDS if k in schema}
    out: dict = {}
    for key, value in schema.items():
        if key == "title" or key in bounds:  # annotation / unsupported keywords at this schema level
            continue
        if key in _NAME_MAPS and isinstance(value, dict):
            out[key] = {name: _strict(sub) for name, sub in value.items()}
        else:
            out[key] = _strict(value)
    if bounds:
        note = ", ".join(f"{k}={v}" for k, v in bounds.items())
        out["description"] = f"{out.get('description', '')} ({note})".strip()
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[Any], Any]
    boundary: Boundary = "local"  # "external" = side effect outside this process; needs human approval

    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": _strict(self.input_model.model_json_schema()),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(
        self, fn: Callable | None = None, *, name: str | None = None, boundary: Boundary = "local"
    ) -> Callable:
        """Decorator. The docstring is the description the model sees — write it for the model.
        Pass boundary="external" for anything with an effect outside this process."""

        def register(f: Callable) -> Callable:
            params = list(inspect.signature(f).parameters)
            # get_type_hints resolves string annotations (modules using `from __future__ import annotations`).
            hint = typing.get_type_hints(f).get(params[0]) if len(params) == 1 else None
            if not (isinstance(hint, type) and issubclass(hint, BaseModel)):
                raise TypeError(f"{f.__name__}: a tool takes exactly one pydantic-model argument")
            tool_name = name or f.__name__
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=inspect.cleandoc(f.__doc__ or ""),
                input_model=hint,
                fn=f,
                boundary=boundary,
            )
            return f

        return register(fn) if fn else register

    def names(self) -> list[str]:
        return list(self._tools)

    def is_external(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.boundary == "external"

    def definitions(self, names: list[str] | None = None) -> list[dict]:
        chosen = names if names is not None else self.names()
        return [self._tools[n].definition() for n in chosen]

    def execute(self, name: str, raw_input: dict, tool_use_id: str, approval: Approval | None = None) -> dict:
        """Run one call and return an API `tool_result` block. Failures come back as is_error.
        External tools run only with an Approval bound to this exact call."""
        t0 = time.perf_counter()
        is_error, blocked = False, False
        tool = self._tools.get(name)
        if tool is None:
            is_error, content = True, f"Unknown tool '{name}'. Available: {', '.join(self.names())}"
        elif tool.boundary == "external" and not (
            approval and approval.authorizes(name, tool_use_id, raw_input)
        ):
            is_error, blocked = True, True
            denied = approval is not None and approval.decision == "denied"
            content = (
                f"{name} was not executed: a human reviewer "
                + ("denied this action." if denied else "has not approved this action.")
                + " Do not retry it; tell the user it needs staff review."
            )
        else:
            try:
                output = tool.fn(tool.input_model.model_validate(raw_input))
                if isinstance(output, BaseModel):
                    content = output.model_dump_json()
                elif isinstance(output, str):
                    content = output
                else:
                    content = json.dumps(output, default=str)
            except ValidationError as e:
                is_error, content = True, f"Invalid arguments for {name}: {e.errors(include_url=False)}"
            except Exception as e:  # tool bugs surface to the model and the trace, not as a crash
                is_error, content = True, f"{name} failed: {type(e).__name__}: {e}"
        tracing.record(
            "tool",
            name=name,
            tool_use_id=tool_use_id,  # with input_digest, matches the approval of this exact call
            input_digest=digest(raw_input),
            boundary=tool.boundary if tool else None,
            blocked=blocked,
            input=raw_input,
            is_error=is_error,
            output=content[:2000],
            latency_s=round(time.perf_counter() - t0, 4),
        )
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            block["is_error"] = True
        return block
