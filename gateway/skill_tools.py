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

    def _with_availability(self, record: dict) -> dict:
        """Report removed, disconnected and blocked tools without executing the steps."""
        missing, blocked = [], []
        profile = self.router.profile
        for name in dict.fromkeys(record["tools"]):
            if name in profile.denied:
                blocked.append(name)
            route = self.router.routes.get(name)
            if route is None or (route.worker is not None and not route.worker.live):
                missing.append(name)
        return {**record, "missing_tools": missing, "blocked_tools": blocked, "usable": not (missing or blocked)}

    async def search(self, request: SkillSearch) -> dict:
        """Find relevant records and tell the agent when to suggest a skill."""
        result = await asyncio.to_thread(self.store.find, request.query, request.limit)
        for category in ("skills", "cache"):
            result[category] = [self._with_availability(row) for row in result[category]]
        result["suggest_skill_draft"] = not any(row["usable"] for row in result["skills"])
        return result

    async def load(self, request: SkillReference) -> dict:
        """Return the stored instructions only after approval."""
        row = await asyncio.to_thread(self.store.get, request.id, "approved")
        return self._with_availability(row)

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
