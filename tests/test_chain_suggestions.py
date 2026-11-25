"""Evaluate discovery and replay on held-out, read-only MCP tasks."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.chain_suggestions import module_draft
from gateway.module_data import ModuleError
from gateway.module_registry import Module, ModuleRegistry
from gateway.module_runtime import ModuleService
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.session_context import TASK_META
from gateway.telemetry import Journal, Telemetry

WORKFLOWS = {
    "inventory": ("demo.inventory", "demo.status", "demo.summary"),
    "report": ("demo.report", "demo.summary"),
    "health": ("demo.health", "demo.inventory", "demo.status", "demo.summary"),
}
SECRET_MARKER = "synthetic-fresh-value"


def test_code_atlas_drafts_feed_ids_into_following_calls():
    """Run generated drafts with source input and reject missing or ambiguous results."""
    entries = [{"name": "design", "id": "id-from-list", "snapshotId": "snapshot-from-list"}]

    class Atlas:
        """Capture outbound arguments and return distinct IDs from each source."""

        def __init__(self):
            """Keep captured calls and controllable fixture results for each run."""
            self.calls = []
            self.profile = SimpleNamespace(timeout_seconds=5)
            self.nodes = [{"id": "node-from-search"}]

        async def call(self, name, args, *, metadata, require_read_only):
            """Return the expected Atlas JSON and verify the module read requirement."""
            assert require_read_only
            self.calls.append((name, args))
            result = {"nodeId": args.get("nodeId")}
            if name.endswith(".list_projects"):
                result = {"projects": entries}
            elif name.endswith(".search_nodes"):
                result = {"nodes": self.nodes, "snapshotId": "snapshot-from-search"}
            return types.CallToolResult(content=[], structuredContent=result)

    atlas = Atlas()
    service = ModuleService(None, atlas)
    tools = ("code-atlas.list_projects", "code-atlas.search_nodes", "code-atlas.describe_node")
    definition = Module.model_validate(module_draft(tools))
    data = {"project": "design", "query": "Card"}
    result = asyncio.run(service.execute(definition, data))
    assert result["result_3"]["nodeId"] == "node-from-search"
    assert atlas.calls == [
        (tools[0], {}),
        (tools[1], {"projectId": "id-from-list", "snapshotId": "snapshot-from-list", "query": "Card"}),
        (tools[2], {"projectId": "id-from-list", "snapshotId": "snapshot-from-search", "nodeId": "node-from-search"}),
    ]

    atlas.calls.clear()
    atlas.nodes = []
    with pytest.raises(ModuleError, match="JSON Pointer does not exist"):
        asyncio.run(service.execute(definition, data))
    assert [name for name, _ in atlas.calls] == list(tools[:2])

    atlas.calls.clear()
    entries.append(entries[0].copy())
    with pytest.raises(ModuleError, match="more than one item"):
        asyncio.run(service.execute(definition, data))
    assert atlas.calls == [(tools[0], {})]


class FixtureSession:
    """Return a deterministic result that exposes step order and fresh input."""

    def __init__(self):
        """Record actual upstream calls so the pilot can compare both modes."""
        self.calls = []

    async def call_tool(self, name, arguments, *, meta=None):
        """Echo the supplied value in a structured MCP result."""
        self.calls.append(name)
        return types.CallToolResult(content=[], structuredContent={
            "tool": name, "value": arguments["value"],
        })


def append_trace(journal, identity, tools, *, outcome="ok", module_step=False, overlap=False):
    """Write training evidence in the metadata format retained by the gateway."""
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    for index, tool in enumerate(tools):
        event = {"at": (start + timedelta(seconds=index)).isoformat(), "tool": tool,
                 "outcome": outcome, "joined": False, "duration_ms": 1500 if overlap else 10,
                 "module_step": module_step}
        if identity is not None:
            event["trace_id"] = f"{identity:032x}"
        journal.write(event)


def training_history(journal):
    """Seed evidence that the pilot must exclude even if it repeats."""
    identity = 1000
    for _ in range(3):
        append_trace(journal, None, ("demo.inventory", "demo.report"))
        append_trace(journal, identity, ("demo.inventory", "demo.report"), outcome="error")
        append_trace(journal, identity + 1, ("demo.inventory", "demo.report"), module_step=True)
        append_trace(journal, identity + 2, ("demo.inventory", "demo.report"), overlap=True)
        identity += 3


def test_chain_pilot_on_held_out_tasks(tmp_path, monkeypatch):
    """Compare direct calls and an accepted draft through the real management MCP endpoint."""
    names = {name for workflow in WORKFLOWS.values() for name in workflow}
    names.add("demo.unseen")
    profile = Profile.model_validate({
        "name": "pilot", "access_token_env": "FIXTURE_TOKEN", "management_bridge": True,
        "modules_enabled": True, "chain_suggestions": True, "module_chain_tools": sorted(names),
        "backends": {"demo": {"transport": "stdio", "command": "fixture"}},
    })
    journal = Journal(tmp_path / "logs", 65536)
    training_history(journal)
    router = Router(profile, Telemetry(journal))
    router.modules = ModuleRegistry(router, tmp_path / "modules.json")
    session = FixtureSession()

    async def backend(name, _config, _timeout, publish, _authorization, *, probe):
        """Publish read-only schemas used by both paths through the same router."""
        tools = {}
        for qualified in names:
            raw = qualified.partition(".")[2]
            tools[qualified] = types.Tool(
                name=raw, inputSchema={"type": "object", "required": ["value"]},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            )
        publish(name, session, tools, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", backend)
    monkeypatch.setattr("gateway.chain_suggestions._QUIET_SECONDS", 0)
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    app = create_app(router, 18792, "fixture-token", settings=settings)
    headers = {"Authorization": "Bearer fixture-token", "Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://127.0.0.1:18792") as client:
        def call(name, arguments, target="/manage/pilot", trace=None, task=None):
            """Invoke one tool with optional task-level trace correlation."""
            params = {"name": name, "arguments": arguments}
            if trace is not None:
                params["_meta"] = {"traceparent": f"00-{trace:032x}-0000000000000001-01"}
            if task is not None:
                params.setdefault("_meta", {})[TASK_META] = task
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
            result = client.post(target, headers=headers, json=request).json()["result"]
            assert not result.get("isError"), result
            return result["structuredContent"]

        observed = call("gateway_chains_suggest", {"tool": "demo.inventory"})
        assert observed["matching_traces"] == 0 and observed["suggestions"] == []
        trace = 1
        for workflow in WORKFLOWS.values():
            for attempt in range(4):
                for step, tool in enumerate(workflow, 1):
                    if attempt % 2:
                        call(tool, {"value": "training-value"}, target="/mcp/pilot",
                             trace=trace * 10 + step, task=f"training-task-{trace}")
                    else:
                        call(tool, {"value": "training-value"}, target="/mcp/pilot", trace=trace)
                trace += 1
        session.calls.clear()

        evaluated = []
        revision = call("gateway_modules_list", {})["revision"]
        for kind, workflow in WORKFLOWS.items():
            anchor = workflow[0]
            proposal = call("gateway_chains_suggest", {"tool": anchor, "limit": 1})["suggestions"][0]
            assert proposal["tools"] == list(workflow)
            assert proposal["support"] == 4
            draft = proposal["module_draft"]
            saved = call("gateway_module_save", {"module": draft, "expected_revision": revision})
            revision = saved["revision"]
            for attempt in range(4):
                values = [{"value": f"{SECRET_MARKER}-{kind}-{attempt}-{step}"} for step in range(len(workflow))]
                baseline = [call(tool, arguments, target="/mcp/pilot") for tool, arguments in zip(workflow, values)]
                data = {f"args_{step}": args for step, args in enumerate(values, 1)}
                replay = call("gateway_module_run", {"id": draft["id"], "data": data, "expected_revision": revision},
                              task=f"replay-{len(evaluated)}")
                assert list(replay["result"].values()) == baseline
                evaluated.append(len(workflow))

        assert sum(evaluated) == 36 and len(evaluated) == 12 and len(session.calls) == 72
        assert call("gateway_chains_suggest", {"tool": "demo.inventory"})["suggestions"][0]["support"] == 4
        assert call("gateway_chains_suggest", {"tool": "demo.unseen"})["suggestions"] == []
        assert SECRET_MARKER not in json.dumps(list(journal.newest_events(65536)))

        call("gateway_set_tool_policy", {"tool": "demo.status", "policy": "deny", "expected_revision": 0})
        assert call("gateway_chains_suggest", {"tool": "demo.inventory"})["suggestions"] == []
