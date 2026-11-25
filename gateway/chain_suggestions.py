"""Propose reusable read-only modules from repeated, correlated MCP calls."""

import asyncio
import re
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from .module_registry import Module
from .profile import ToolName
from .telemetry import Journal
from .tool_feedback import FeedbackError

_TRACE = re.compile(r"[a-f0-9]{32}")
_READ_BYTES = 1024 * 1024
_MIN_SUPPORT = 2
_QUIET_SECONDS = 10
_ATLAS_LIST = "code-atlas.list_projects"
_ATLAS_SEARCH = "code-atlas.search_nodes"
_ATLAS_DESCRIBE = "code-atlas.describe_node"


class SuggestChains(BaseModel):
    """Select the known first tool and limit proposals for this discovery request."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tool: ToolName
    limit: int = Field(default=3, ge=1, le=5, strict=True)


def observed_chains(
    journal: Journal, eligible: set[str], anchor: str, retention_days: int, now: float,
) -> tuple[Counter[tuple[str, ...]], int]:
    """Count settled sequences of two to four manual calls in the retained window."""
    tasks = {}
    cutoff = now - retention_days * 86400
    for event in journal.newest_events(_READ_BYTES):
        task = event.get("task_tag")
        kind = "task" if task is not None else "trace"
        identity = task if task is not None else event.get("trace_id")
        if not isinstance(identity, str) or not _TRACE.fullmatch(identity) or identity == "0" * 32:
            continue
        calls = tasks.setdefault((kind, identity), [])
        if len(calls) == 5:
            continue
        calls.append({key: event.get(key) for key in
                      ("tool", "outcome", "joined", "module_step", "duration_ms", "at")})

    counts = Counter()
    samples = 0
    for events in tasks.values():
        signature = sequential_signature(events, eligible, cutoff, now - _QUIET_SECONDS)
        if signature is not None and signature[0] == anchor:
            counts[signature] += 1
            samples += 1
    return counts, samples


def sequential_signature(
    events: list[dict], eligible: set[str], cutoff: float, settled: float,
) -> tuple[str, ...] | None:
    """Reject failed, joined, overlapping, stale and automatically replayed traces."""
    if not 2 <= len(events) <= 4:
        return None
    tools = []
    previous = cutoff
    for event in reversed(events):
        tool = event.get("tool")
        duration = event.get("duration_ms")
        if (not isinstance(tool, str) or tool not in eligible or event.get("outcome") != "ok"
                or event.get("joined") is not False or event.get("module_step")
                or type(duration) not in (int, float) or not 0 <= duration < 300_000):
            return None
        try:
            at = datetime.fromisoformat(event["at"])
            if at.tzinfo is None:
                return None
            finished = at.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None
        if finished > settled or finished - duration / 1000 < previous - 0.002:
            return None
        tools.append(tool)
        previous = finished
    return tuple(tools)


def atlas_bindings(tools: tuple[str, ...]) -> dict[int, list[dict]]:
    """Build explicit bindings for known Code Atlas responses and preserve call order."""
    if tools == (_ATLAS_SEARCH, _ATLAS_DESCRIBE):
        return {1: [], 2: [{"op": "object", "fields": {
            "nodeId": "/steps/result_1/nodes/0/id", "projectId": "/input/projectId",
            "snapshotId": "/steps/result_1/snapshotId",
        }}]}
    if tools not in {(_ATLAS_LIST, _ATLAS_SEARCH), (_ATLAS_LIST, _ATLAS_SEARCH, _ATLAS_DESCRIBE)}:
        return {}
    return {1: [{"op": "object", "fields": {}}],
            2: [{"op": "find", "source": "/steps/result_1/projects", "field": "/name",
                 "match": "/input/project", "save_as": "project"},
                {"op": "object", "fields": {"projectId": "/steps/project/id",
                                           "snapshotId": "/steps/project/snapshotId", "query": "/input/query"}}],
            3: [{"op": "object", "fields": {"nodeId": "/steps/result_2/nodes/0/id",
                                           "projectId": "/steps/project/id",
                                           "snapshotId": "/steps/result_2/snapshotId"}}]}


def module_draft(tools: tuple[str, ...], bindings: dict[int, list[dict]] | None = None) -> dict:
    """Use known output bindings when available, otherwise request arguments per step."""
    if bindings is None:
        bindings = atlas_bindings(tools)
    identity_parts = (*tools, "bound-v1") if bindings else tools
    identity = sha256("\0".join(identity_parts).encode("ascii")).hexdigest()[:16]
    steps = []
    fields = {}
    for index, tool in enumerate(tools, start=1):
        result = f"result_{index}"
        if bindings:
            preparation = bindings[index]
            steps.extend(preparation)
            arguments = "/value" if preparation else "/input"
        else:
            arguments = f"/input/args_{index}"
        steps.append({"op": "call", "tool": tool, "arguments": arguments, "save_as": result})
        fields[result] = f"/steps/{result}"
    steps.append({"op": "object", "fields": fields})
    module = Module(id=f"chain_{identity}", title=f"Observed chain for {tools[0]}"[:100], steps=steps)
    return module.model_dump(exclude_none=True)


class ChainSuggestions:
    """Serve a bounded read-only recommendation tool on the management endpoint."""

    def __init__(self, router):
        """Bind mining to the same profile, journal and policy as the module runner."""
        self.router = router
        self._scan = None
        self.commands = {
            "gateway_chains_suggest": (
                SuggestChains, "Suggest unsaved read-only module drafts from repeated task IDs or traces. "
                "Pass the first tool of the intended workflow. Known Code Atlas chains bind IDs between steps.",
                True, self.suggest,
            ),
        }

    def eligible(self) -> set[str]:
        """Require current policy, a read-only annotation and a live upstream route."""
        tools = set()
        for name in self.router.profile.module_chain_tools:
            route = self.router.routes.get(name)
            if self.router.allows_module_read(name) and (route.worker is None or route.worker.live):
                tools.add(name)
        return tools

    async def suggest(self, request: SuggestChains) -> dict:
        """Rank by distinct task attempts and return only current, unsaved suggestions."""
        eligible = self.eligible()
        revision = self.router.catalog_revision
        result = {"catalog_revision": revision, "matching_traces": 0, "suggestions": []}
        if request.tool not in eligible:
            return result
        if self._scan is not None and not self._scan.done():
            raise FeedbackError("Chain history is being read. Retry the suggestion request.")
        self._scan = asyncio.create_task(asyncio.to_thread(
            observed_chains, self.router.telemetry.journal, eligible, request.tool,
            self.router.profile.log_retention_days, datetime.now(timezone.utc).timestamp(),
        ))
        counts, samples = await asyncio.shield(self._scan)
        if self.router.catalog_revision != revision:
            raise FeedbackError("Catalog changed while reading chain history. Retry the suggestion request.")
        eligible = self.eligible()
        result["matching_traces"] = samples
        for tools, support in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            if support < _MIN_SUPPORT or not all(name in eligible for name in tools):
                continue
            bindings = atlas_bindings(tools)
            suggestion = {"tools": list(tools), "support": support, "module_draft": module_draft(tools, bindings)}
            if bindings:
                fields = ["projectId", "query"] if tools[0] == _ATLAS_SEARCH else ["project", "query"]
                suggestion["inputs"] = dict.fromkeys(fields, "string")
            result["suggestions"].append(suggestion)
            if len(result["suggestions"]) == request.limit:
                break
        return result
