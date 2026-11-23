"""The one door to the model.

Every LLM call in the kit goes through `complete()` or `structured()`, so model routing,
refusal handling, fallbacks and tracing live in exactly one place.

Routing is by *role*, not by call site: a node says what kind of work it is doing and the
route table decides the model. Swapping a model for cost or latency is a one-line change
here, and an eval run before and after tells you whether quality held.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from kit import tracing
from kit.config import PROFILE


class Role(StrEnum):
    SUPERVISOR = "supervisor"  # plans, routes, synthesizes — strongest model
    WORKER = "worker"  # tool-using specialist
    CLASSIFIER = "classifier"  # cheap, high-volume labels
    JUDGE = "judge"  # eval grading


@dataclass(frozen=True)
class Route:
    model: str
    effort: str  # low | medium | high | xhigh | max
    max_tokens: int


ROUTES: dict[str, dict[Role, Route]] = {
    "quality": {
        Role.SUPERVISOR: Route("claude-opus-5-5", "medium", 16000),
        Role.WORKER: Route("claude-sonnet-5-5", "medium", 16000),
        Role.CLASSIFIER: Route("claude-haiku-5-5", "low", 4096),
        Role.JUDGE: Route("claude-sonnet-5-5", "low", 8000),
    },
    # Interview-day / latency profile: same graph, cheaper and faster models.
    "fast": {
        Role.SUPERVISOR: Route("claude-sonnet-5-5", "low", 16000),
        Role.WORKER: Route("claude-haiku-5-5", "low", 8000),
        Role.CLASSIFIER: Route("claude-haiku-5-5", "low", 4096),
        Role.JUDGE: Route("claude-haiku-5-5", "low", 8000),
    },
}


def prompt_sha(system: str) -> str:
    """Version id of a system prompt: which instructions produced a decision (audit evidence)."""
    return hashlib.sha256(system.encode()).hexdigest()[:16]


def route_for(role: Role) -> Route:
    base = ROUTES[PROFILE][role]
    override = os.getenv(f"KIT_MODEL_{role.value.upper()}")  # e.g. KIT_MODEL_JUDGE=claude-opus-5-5
    return Route(override, base.effort, base.max_tokens) if override else base


# Server-side refusal fallback: Opus/Sonnet/Fable support it; Haiku has none.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _supports_fallback(model: str) -> bool:
    return model.startswith(("claude-opus", "claude-sonnet", "claude-fable"))


class Refused(RuntimeError):
    def __init__(self, model: str, category: str | None, explanation: str | None):
        super().__init__(f"{model} refused ({category}): {explanation}")
        self.category = category


@dataclass
class Result:
    content: list[dict]  # API-shaped content blocks, safe to append back into messages
    stop_reason: str
    model: str
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b["type"] == "text")

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b["type"] == "tool_use"]


_client: anthropic.Anthropic | None = None

# Offline mode: a scripted stand-in for the API, for graph tests and the leak scan.
# Signature: fn(role, system, messages, tools, schema) -> content blocks, or a schema instance.
FakeModel = Callable[[Role, str, list[dict], list[dict] | None, type[BaseModel] | None], Any]
_fake: FakeModel | None = None


@contextmanager
def fake(fn: FakeModel) -> Iterator[None]:
    """Route every call through `fn` instead of the API. Tracing and the content policy still apply."""
    global _fake
    previous, _fake = _fake, fn
    try:
        yield
    finally:
        _fake = previous


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _request(role: Role, system: str, messages: list[dict], **extra: Any) -> dict:
    r = route_for(role)
    req: dict[str, Any] = dict(
        model=r.model,
        max_tokens=r.max_tokens,
        system=system,
        messages=messages,
        output_config={"effort": r.effort},
        **extra,
    )
    if _supports_fallback(r.model):
        req["betas"] = [FALLBACK_BETA]
        req["fallbacks"] = "default"
    return req


def _check(resp: Any, model: str) -> None:
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        raise Refused(model, getattr(details, "category", None), getattr(details, "explanation", None))


def _usage(resp: Any) -> dict:
    u = resp.usage
    return {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
    }


def complete(
    role: Role,
    *,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    node: str = "",
) -> Result:
    """One model turn. Returns content blocks verbatim so they can be appended back unchanged."""
    extra: dict[str, Any] = {"tools": tools} if tools else {}
    req = _request(role, system, messages, **extra)
    t0 = time.perf_counter()
    if _fake is not None:
        content = _fake(role, system, messages, tools, None)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        result = Result(content=content, stop_reason=stop, model="fake", latency_s=0.0)
    else:
        resp = client().beta.messages.create(**req)
        _check(resp, req["model"])
        result = Result(
            content=resp.to_dict()["content"],
            stop_reason=resp.stop_reason,
            model=resp.model,
            usage=_usage(resp),
            latency_s=round(time.perf_counter() - t0, 3),
        )
    tracing.record(
        "llm",
        node=node,
        role=role.value,
        prompt_sha=prompt_sha(system),
        model=result.model,
        stop_reason=result.stop_reason,
        latency_s=result.latency_s,
        usage=result.usage,
        tool_calls=[{"name": b["name"], "input": b["input"]} for b in result.tool_uses],
        text=result.text[:2000],
    )
    return result


# --- embeddings -----------------------------------------------------------------------------
# Anthropic has no embedding model; its docs point to Voyage AI (now part of MongoDB; keys come
# from MongoDB Atlas -> AI Model APIs, set as VOYAGE_API_KEY). It still goes through this module
# (rule 1) so the model choice, the fake hook and tracing live in one place.
#
# Why voyage-4: Voyage's current general-purpose model (Jan 2026), the balanced middle of the
# family (-lite is cheaper, -large is stronger), 32k context. 1024 dims is its default and keeps
# vectors small; Voyage vectors are unit length, so cosine equals dot product. input_type
# ("query" / "document") is always sent: Voyage prefixes each side differently, which retrieves
# better than embedding both the same way. voyage-4-nano is the open-weight sibling for running
# without a key or on-prem. Changing the model or dims means re-embedding any stored index.
EMBED_MODEL = os.getenv("KIT_EMBED_MODEL", "voyage-4")
EMBED_DIM = int(os.getenv("KIT_EMBED_DIM", "1024"))

FakeEmbed = Callable[[list[str], str], list[list[float]]]
_fake_embed: FakeEmbed | None = None
_voyage: Any = None


@contextmanager
def fake_embed(fn: FakeEmbed) -> Iterator[None]:
    """Route every embed() through `fn(texts, input_type)` instead of the API."""
    global _fake_embed
    previous, _fake_embed = _fake_embed, fn
    try:
        yield
    finally:
        _fake_embed = previous


def _voyage_client() -> Any:
    global _voyage
    if _voyage is None:
        try:
            import voyageai
        except ImportError as e:  # pragma: no cover - dependency hint
            raise RuntimeError("embeddings need the voyageai package: uv add voyageai") from e
        kwargs: dict[str, Any] = {}
        if base := os.getenv("VOYAGE_BASE_URL"):  # Atlas-issued keys may use a different endpoint
            kwargs["base_url"] = base
        _voyage = voyageai.Client(**kwargs)  # reads VOYAGE_API_KEY
    return _voyage


def embed(texts: list[str], *, input_type: str = "document", node: str = "") -> list[list[float]]:
    """Embed texts. input_type is "document" when indexing, "query" when searching.

    Text leaves the machine: synthetic or de-identified content only (rule 3).
    """
    if input_type not in ("document", "query"):
        raise ValueError(f"input_type must be 'document' or 'query', got {input_type!r}")
    t0 = time.perf_counter()
    if _fake_embed is not None:
        vectors, model, tokens = _fake_embed(texts, input_type), "fake", 0
    else:
        resp = _voyage_client().embed(
            texts, model=EMBED_MODEL, input_type=input_type, output_dimension=EMBED_DIM
        )
        vectors, model, tokens = resp.embeddings, EMBED_MODEL, getattr(resp, "total_tokens", 0)
    tracing.record(
        "embed",
        node=node,
        model=model,
        n=len(texts),
        input_type=input_type,
        tokens=tokens,
        latency_s=round(time.perf_counter() - t0, 3),
    )
    return vectors


T = TypeVar("T", bound=BaseModel)


def structured(role: Role, *, system: str, messages: list[dict], schema: type[T], node: str = "") -> T:
    """One model turn constrained to a pydantic schema (structured outputs, no forced tool_choice)."""
    if _fake is not None:
        parsed = schema.model_validate(_fake(role, system, messages, None, schema))
        tracing.record(
            "llm",
            node=node,
            role=role.value,
            prompt_sha=prompt_sha(system),
            model="fake",
            parsed=parsed.model_dump(),
        )
        return parsed
    req = _request(role, system, messages, output_format=schema)
    t0 = time.perf_counter()
    resp = client().beta.messages.parse(**req)
    latency = time.perf_counter() - t0
    _check(resp, req["model"])
    parsed = resp.parsed_output
    tracing.record(
        "llm",
        node=node,
        role=role.value,
        prompt_sha=prompt_sha(system),
        model=resp.model,
        stop_reason=resp.stop_reason,
        latency_s=round(latency, 3),
        usage=_usage(resp),
        parsed=parsed.model_dump() if parsed is not None else None,
    )
    if parsed is None:
        raise ValueError(f"{resp.model} returned no parseable {schema.__name__} (stop={resp.stop_reason})")
    return parsed
