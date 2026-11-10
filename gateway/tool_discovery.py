"""Expose compact tool/skill discovery and explicit feedback through management MCP."""

import asyncio
from heapq import nsmallest
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .profile import Identifier, ToolName
from .skill_store import query_words
from .skill_tools import SkillSearch
from .tool_feedback import FeedbackError, ToolFeedback

DISCOVERY_META = "ag-mcp-gateway/discovery"


class Query(BaseModel):
    """Bound search input and return a small number of candidates per category."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    kind: Literal["all", "tools", "skills"] = "all"
    backend: Identifier | None = Field(default=None, description="Filter tool results by upstream MCP name.")
    limit: int = Field(default=5, ge=1, le=10, strict=True)


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

    def decorate(self, tools):
        """Add namespaced observations without modifying upstream descriptors."""
        for tool in tools:
            if self.available(tool.name) is not None:
                tool.meta = {**(tool.meta or {}), DISCOVERY_META: self.signals(tool.name)}

    def summary(self, name: str, route) -> dict:
        """Build a compact tool card without its schema or private route details."""
        tool = route.tool
        return {"name": name, "backend": name.partition(".")[0], "title": (tool.title or "")[:120],
                "description": (tool.description or "")[:600], **self.signals(name)}

    @staticmethod
    def matching(request: Query, candidates, ratings):
        """Yield candidate scores with lexical relevance ahead of usefulness."""
        terms = query_words(request.query)
        exact = request.query.casefold()
        for name, route in candidates:
            relevance = 100 if name.casefold() == exact else 0
            if terms:
                label = f"{name} {(route.tool.title or '')[:120]}"
                relevance += 3 * len(terms & query_words(label))
                relevance += len(terms & query_words((route.tool.description or "")[:600]))
            if relevance:
                helpful, votes = ratings.get(name, (0, 0))
                yield -relevance, -(helpful + 1) / (votes + 2), name, route

    def rank_tools(self, request: Query, candidates, ratings):
        """Score a frozen catalogue off the event loop and keep only the best matches."""
        matches = nsmallest(request.limit, self.matching(request, candidates, ratings))
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
            future = asyncio.get_running_loop().run_in_executor(
                None, self.rank_tools, request, candidates, self.feedback.ranking_snapshot(),
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

    async def describe(self, request: ToolReference) -> dict:
        """Return the selected tool's schema and annotations after a live policy check."""
        route = self.available(request.tool)
        if route is None:
            raise FeedbackError("Tool is unavailable")
        tool = route.tool
        return {**self.summary(request.tool, route), "catalog_revision": self.router.catalog_revision,
                "input_schema": tool.inputSchema, "output_schema": tool.outputSchema,
                "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else None}

    async def rate(self, request: ToolVote) -> dict:
        """Record a vote without blocking the MCP event loop."""
        if self.available(request.tool) is None:
            raise FeedbackError("Tool is unavailable")
        await asyncio.to_thread(self.feedback.rate, request.tool, request.useful, request.feedback_id)
        return {"name": request.tool, **self.signals(request.tool)}
