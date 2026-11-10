"""Expose SQLite skill search and explicit submissions through management MCP."""

import asyncio
import logging
import sqlite3
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .profile import ToolName
from .skill_store import SkillStore, SkillStoreError

_LOG = logging.getLogger("gateway")
SkillId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Instructions = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]


class SkillCommand(BaseModel):
    """Validate management inputs without embedding supplied text in errors."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SkillSearch(SkillCommand):
    """Find approved skills and cached approaches for a bounded text query."""

    query: Summary
    limit: int = Field(default=5, ge=1, le=10, strict=True)


class SkillReference(SkillCommand):
    """Identify one immutable submitted record."""

    id: SkillId


class SkillVote(SkillReference):
    """Submit one usefulness observation for an approved skill."""

    useful: bool = Field(strict=True)


class CachedApproach(SkillCommand):
    """Save an explicitly sanitized summary and tool sequence, including repeats."""

    title: Title
    summary: Summary
    tools: list[ToolName] = Field(min_length=1, max_length=32)


class SkillDraft(CachedApproach):
    """Propose a reusable skill whose instructions require operator approval."""

    instructions: Instructions


class SkillTools:
    """Bind storage operations and annotate candidates against the live profile."""

    def __init__(self, store: SkillStore, router):
        """Describe only operations agents may use, leaving approval to the CLI."""
        self.store = store
        self.router = router
        self.commands = {
            "gateway_skill_search": (SkillSearch, "Search approved MCP skills and cached tool sequences. Use the draft suggestion for a new approach.", True, self.search),
            "gateway_skill_load": (SkillReference, "Load the instructions of one approved MCP skill by ID.", True, self.load),
            "gateway_skill_propose": (SkillDraft, "Submit a sanitized MCP skill draft for operator review. Never include secrets, private identifiers, arguments or results.", False, self.save),
            "gateway_skill_rate": (SkillVote, "Record whether an approved MCP skill was useful after applying it.", False, self.rate),
            "gateway_skill_cache_save": (CachedApproach, "Cache a sanitized approach summary and ordered tool names only. Never include secrets, private identifiers, arguments or results.", False, self.save),
        }

    def _availability(self) -> tuple[set[str], set[str]]:
        """Capture live tools and denied names from one event-loop catalog generation."""
        live = set()
        for name, route in self.router.routes.items():
            if route.worker is None or route.worker.live:
                live.add(name)
        return live, set(self.router.profile.denied)

    @staticmethod
    def _with_availability(record: dict, live: set[str], denied: set[str]) -> dict:
        """Report removed, disconnected and blocked tools without executing the steps."""
        missing, blocked = [], []
        for name in dict.fromkeys(record["tools"]):
            if name in denied:
                blocked.append(name)
            if name not in live:
                missing.append(name)
        return {**record, "missing_tools": missing, "blocked_tools": blocked, "usable": not (missing or blocked)}

    async def search(self, request: SkillSearch) -> dict:
        """Find relevant usable records first and offer a draft when there are none."""
        live, denied = self._availability()
        # Retry once if a policy or connection changed during the SQLite ranking.
        for _ in range(2):
            available = live - denied
            result = await asyncio.to_thread(self.store.find, request.query, request.limit, available)
            live, denied = self._availability()
            if live - denied == available:
                break
        for category in ("skills", "cache"):
            rows = [self._with_availability(row, live, denied) for row in result[category]]
            result[category] = sorted(rows, key=lambda row: not row["usable"])
        result["suggest_skill_draft"] = not any(row["usable"] for row in result["skills"])
        return result

    async def load(self, request: SkillReference) -> dict:
        """Return the stored instructions only after approval."""
        row = await asyncio.to_thread(self.store.get, request.id, "approved")
        return self._with_availability(row, *self._availability())

    async def save(self, request: CachedApproach) -> dict:
        """Validate the profile namespace before storing the explicit submission."""
        for name in request.tools:
            backend, separator, tool = name.partition(".")
            if not separator or not tool or backend not in self.router.profile.backends:
                raise SkillStoreError("Skill steps require configured MCP tool names")
        instructions = request.instructions if isinstance(request, SkillDraft) else None
        return await asyncio.to_thread(self.store.save, request.title, request.summary, request.tools, instructions)

    async def rate(self, request: SkillVote) -> dict:
        """Add feedback without changing the approved instructions or tool order."""
        return await asyncio.to_thread(self.store.rate, request.id, request.useful)


async def maintain_skill_cache(store: SkillStore):
    """Prune at daemon startup and once per minute, tolerating transient storage errors."""
    while True:
        try:
            await asyncio.to_thread(store.cleanup)
        except (OSError, sqlite3.Error, SkillStoreError):
            _LOG.warning("Skill cache cleanup failed")
        await asyncio.sleep(60)
