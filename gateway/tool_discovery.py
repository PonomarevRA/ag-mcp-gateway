"""Expose compact tool/skill discovery and explicit feedback through management MCP."""

import asyncio
from heapq import nsmallest
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .module_registry import Module
from .profile import Identifier, ToolName
from .skill_store import query_words
from .skill_tools import SkillSearch
from .tool_feedback import FeedbackError, ToolFeedback
from .tool_keywords import catalog_terms, concepts, words

DISCOVERY_META = "ag-mcp-gateway/discovery"


def trace_field(schema: dict) -> str | None:
    """Select a standalone trace string when no other required inputs are known."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or schema.get("type") != "object":
        return None
    if any(key in schema for key in ("$ref", "allOf", "anyOf", "oneOf", "if", "dependentRequired",
                                      "dependentSchemas", "minProperties", "maxProperties")):
        return None
    required = schema.get("required", [])
    if not isinstance(required, list):
        return None
    for name in ("traceId", "trace_id", "traceID"):
        field = properties.get(name)
        if isinstance(field, dict) and field.get("type") == "string" and all(key == name for key in required):
            return name
    return None


class Query(BaseModel):
    """Bound search input and return a small number of candidates per category."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    kind: Literal["all", "tools", "skills"] = "all"
    backend: Identifier | None = Field(default=None, description="Filter tool results by upstream MCP name.")
    sort: Literal["usage", "relevance", "name"] = Field(
        default="usage", description="Rank matching tools by usage, relevance or name. Skills keep their relevance order.",
    )
    limit: int = Field(default=5, ge=1, le=10, strict=True)


class KeywordQuery(Query):
    """Find technical capabilities and optionally propose a read-only trace module."""

    kind: Literal["tools"] = "tools"
    sort: Literal["usage", "relevance", "name"] = "relevance"
    draft_trace_module: bool = False


class ToolReference(BaseModel):
    """Select one fully qualified MCP tool from the current profile."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tool: ToolName


class ToolVote(ToolReference):
    """Submit one explicit usefulness vote with a retry-stable identifier."""

    useful: bool = Field(strict=True)
    feedback_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


class Discovery:
    """Join the current allowed catalog, rolling latency and persisted usefulness."""

    def __init__(self, router, feedback: ToolFeedback, skills):
        """Bind discovery commands to the profile catalogue and stores."""
        self.router = router
        self.feedback = feedback
        self.skills = skills
        self._search_gate = asyncio.Semaphore(1)
        self.commands = {
            "gateway_tools_search": (Query, "Find suitable live MCP tools and approved skills. Results show measured latency and usefulness.", True, self.search),
            "gateway_tools_find_keywords": (
                KeywordQuery, "Find MCP tools by backend, technical keywords and schema fields such as traceId. "
                "Optionally draft a trace module from compatible read-only tools.", True, self.find_keywords,
            ),
            "gateway_tool_describe": (ToolReference, "Read one live allowed tool's full schemas, latency and usefulness.", True, self.describe),
            "gateway_tool_rate": (ToolVote, "Record whether an MCP tool was useful after applying it. Reuse feedback_id on retries.", False, self.rate),
        }

    def available(self, name: str):
        """Resolve only currently allowed tools with a usable upstream route."""
        if name in self.router.profile.denied:
            return None
        route = self.router.routes.get(name)
        if route is None or (route.worker is not None and not route.worker.live):
            return None
        return route

    def signals(self, name: str) -> dict:
        """Keep observed duration distinct from subjective feedback."""
        helpful, votes = self.feedback.current(name)
        mean, samples = self.router.telemetry.tool_latency(name)
        return {"latency": {"mean_ms": mean, "samples": samples},
                "usefulness": {"score": round(helpful / votes, 3) if votes else None,
                               "helpful": helpful, "votes": votes}}

    def decorate(self, tools, *, backend: str | None = None):
        """Attach the same namespaced observations to aggregate and scoped descriptors."""
        for tool in tools:
            name = tool.name if backend is None else f"{backend}.{tool.name}"
            if self.available(name) is not None:
                tool.meta = {**(tool.meta or {}), DISCOVERY_META: self.signals(name)}

    def summary(self, name: str, route) -> dict:
        """Build a compact tool card without its schema or private route details."""
        tool = route.tool
        return {"name": name, "backend": name.partition(".")[0], "uses": self.router.telemetry.tool_uses(name),
                "title": (tool.title or "")[:120],
                "description": (tool.description or "")[:600], **self.signals(name)}

    @staticmethod
    def matching(request: Query, candidates, ratings, observed=None):
        """Yield candidate scores with lexical relevance ahead of usefulness."""
        terms = query_words(request.query)
        exact = request.query.casefold()
        labels = words(request.query)
        topics = concepts(labels)
        for name, route in candidates:
            relevance = 100 if name.casefold() == exact else 0
            if terms:
                label = f"{name} {(route.tool.title or '')[:120]}"
                relevance += 3 * len(terms & query_words(label))
                relevance += len(terms & query_words((route.tool.description or "")[:600]))
            if isinstance(request, KeywordQuery):
                specific, description = catalog_terms(name, route.tool)
                runtime = observed.get(name, ()) if observed else ()
                relevance += 6 * len(labels & specific)
                relevance += 2 * len(labels & description)
                relevance += 4 * len(topics & concepts(specific | description))
                relevance += 2 * len(topics.intersection(runtime))
            if relevance:
                helpful, votes = ratings.get(name, (0, 0))
                yield -relevance, -(helpful + 1) / (votes + 2), name, route

    def rank_tools(self, request: Query, candidates, ratings, usage, observed):
        """Rank lexical matches using the selected order and a stable name tie-break."""
        matches = self.matching(request, candidates, ratings, observed)
        key = None
        if request.sort == "usage":
            key = lambda match: (-usage[match[2]], *match[:3])
        elif request.sort == "name":
            key = lambda match: match[2]
        matches = nsmallest(request.limit, matches, key=key)
        return [(name, route) for _, _, name, route in matches]

    def _finish_ranking(self, future: asyncio.Future):
        """Release admission only after the worker actually finishes, even on client cancellation."""
        self._search_gate.release()
        if not future.cancelled():
            future.exception()

    async def find_tools(self, request: Query) -> tuple[int, list[dict]]:
        """Run one ranking at a time and enforce the current catalogue before returning."""
        await self._search_gate.acquire()
        try:
            revision = self.router.catalog_revision
            prefix = request.backend + "." if request.backend is not None else ""
            candidates = tuple((name, route) for name, route in self.router.routes.items()
                               if name.startswith(prefix) and self.available(name) is route)
            usage = {name: self.router.telemetry.tool_uses(name) for name, _ in candidates}
            observed = {}
            if isinstance(request, KeywordQuery):
                observed = {name: self.router.keywords.observed(name) for name, _ in candidates}
            future = asyncio.get_running_loop().run_in_executor(
                None, self.rank_tools, request, candidates, self.feedback.ranking_snapshot(), usage, observed,
            )
        except BaseException:
            self._search_gate.release()
            raise
        future.add_done_callback(self._finish_ranking)
        matches = await asyncio.shield(future)
        if self.router.catalog_revision != revision:
            raise FeedbackError("Catalog changed during discovery. Retry the search.")
        cards = [self.summary(name, route) for name, route in matches if self.available(name) is route]
        return revision, cards

    async def search(self, request: Query) -> dict:
        """Search both sources, or return only the requested category."""
        skills = []
        if request.kind != "tools" and self.skills is not None:
            result = await self.skills.search(SkillSearch(query=request.query, limit=request.limit))
            skills = result["skills"]
        revision, tools = self.router.catalog_revision, []
        if request.kind != "skills":
            revision, tools = await self.find_tools(request)
        skills = [item for item in skills if all(self.available(name) is not None for name in item["tools"])]
        return {
            "catalog_revision": revision,
            "tools": tools,
            "skills": skills,
            "skill_search_enabled": self.skills is not None,
        }

    async def find_keywords(self, request: KeywordQuery) -> dict:
        """Return keyword matches and, on request, an unsaved module proposal."""
        revision, cards = await self.find_tools(request)
        for card in cards:
            name = card["name"]
            card.update(self.router.keywords.describe(name, self.router.routes[name].tool))
        draft = self.trace_module(cards) if request.draft_trace_module else None
        return {"catalog_revision": revision, "tools": cards, "module_draft": draft}

    def trace_module(self, cards: list[dict]) -> dict | None:
        """Map one trace input across at most four allowlisted MCPs with simple schemas."""
        if self.router.modules is None:
            return None
        steps = []
        outputs = {}
        for card in cards:
            name, backend = card["name"], card["backend"]
            if backend in outputs or not self.router.allows_module_read(name):
                continue
            field = trace_field(self.router.routes[name].tool.inputSchema)
            if field is None:
                continue
            step = f"result_{len(outputs) + 1}"
            steps.extend([{"op": "object", "fields": {field: "/input/trace_id"}},
                          {"op": "call", "tool": name, "save_as": step}])
            outputs[backend] = f"/steps/{step}"
            if len(outputs) == 4:
                break
        if not outputs:
            return None
        steps.append({"op": "object", "fields": outputs})
        module = Module.model_validate({"id": "trace_search_draft", "title": "Search by trace ID", "steps": steps})
        return {"module": module.model_dump(exclude_none=True), "input_fields": {"trace_id": "string"}}

    async def describe(self, request: ToolReference) -> dict:
        """Return the selected tool's schema and annotations after a live policy check."""
        route = self.available(request.tool)
        if route is None:
            raise FeedbackError("Tool is unavailable")
        tool = route.tool
        return {**self.summary(request.tool, route), "catalog_revision": self.router.catalog_revision,
                **self.router.keywords.describe(request.tool, tool),
                "input_schema": tool.inputSchema, "output_schema": tool.outputSchema,
                "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else None}

    async def rate(self, request: ToolVote) -> dict:
        """Record a vote without blocking the MCP event loop."""
        if self.available(request.tool) is None:
            raise FeedbackError("Tool is unavailable")
        await asyncio.to_thread(self.feedback.rate, request.tool, request.useful, request.feedback_id)
        return {"name": request.tool, **self.signals(request.tool)}
