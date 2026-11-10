"""Verify data operations, registry fences, browser forms and normal MCP routing."""

import asyncio
import json
import re
import time
from base64 import b64encode

import pytest
from mcp import types
from pydantic import ValidationError
from starlette.testclient import TestClient

from gateway.hook_lifecycle import HOOK_META, GenerationRules
from gateway.http_app import create_app
from gateway.module_data import ModuleError, pointer
from gateway.module_registry import (ActivateHook, Bind, Delete, Module, ModuleRegistry, PinHook, Run, Save,
                                     SetGenerations)
from gateway.module_runtime import ModuleService, result_data, run_local
from gateway.policy_settings import PolicyChange, ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry

ORIGIN = "http://127.0.0.1:18792"
TOKEN = "fixture-modules-token"


def test_chain_text_fallback_keeps_json_data_limits():
    """Treat plain text as text while preserving the validation of parsed JSON."""

    def result(text):
        """Build the single text block accepted by a manual MCP chain."""
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    assert result_data(result("plain response"), allow_text=True) == "plain response"
    assert result_data(result("[1,2]"), allow_text=True) == [1, 2]
    with pytest.raises(ModuleError, match="structural limit"):
        result_data(result(json.dumps([0] * 12_001)), allow_text=True)


def definition(identity, *steps):
    """Create one reusable synthetic module definition."""
    return Module.model_validate({"id": identity, "title": identity, "steps": list(steps)})


def test_local_pipeline():
    """Compose filtering, a numeric reduction, comparison and a projected result."""
    module = definition(
        "local",
        {"op": "contains", "source": "/input/expected", "pattern": {"tags": ["a"]}, "save_as": "contained"},
        {"op": "filter", "source": "/input/rows", "pattern": {"tags": ["a"]}, "save_as": "matched"},
        {"op": "aggregate", "method": "sum", "source": "/value", "item": "/amount", "save_as": "total"},
        {"op": "math", "expression": "total * 2 + 1", "variables": {"total": "/steps/total"}, "save_as": "score"},
        {"op": "compare", "left": "/input/expected", "right": "/input/actual", "save_as": "diff"},
        {"op": "object", "fields": {"matched": "/steps/matched", "score": "/steps/score",
                                    "diff": "/steps/diff", "contained": "/steps/contained"}},
    )
    data = {"rows": [{"tags": ["b", "a"], "amount": 5}, {"tags": ["b"], "amount": 10}],
            "expected": {"tags": ["a"], "a/b": True}, "actual": {"tags": ["a"], "a/b": 1}}
    assert run_local(module, data) == {
        "matched": [data["rows"][0]], "score": 11.0,
        "diff": {"equal": False, "changes": [{"path": "/a~1b", "change": "changed"}]}, "contained": True,
    }
    whole = definition("whole", {"op": "select", "source": "", "save_as": "context"})
    assert run_local(whole, {"value": 3}) == {"input": {"value": 3}, "value": {"value": 3}, "steps": {}}
    assert pointer({"a/b": {"~key": 7}}, "/a~1b/~0key") == 7
    with pytest.raises(ModuleError, match="Pointer"):
        pointer([0], "/00")
    with pytest.raises(ValidationError):
        definition("bad", {"op": "math", "expression": "x ** 2", "variables": {"x": "/value"}})
    divide = definition("divide", {"op": "math", "expression": "1 / x", "variables": {"x": "/value"}})
    with pytest.raises(ModuleError, match="zero"):
        run_local(divide, 0)
    with pytest.raises(ModuleError, match="byte limit"):
        run_local(module, {"text": "x" * 262145})


@pytest.fixture
def fixture_gateway(tmp_path, monkeypatch):
    """Provide two read-only routes and one mutation in an isolated authenticated profile."""
    profile = Profile.model_validate({"name": "test", "management_bridge": True, "modules_enabled": True,
        "access_token_env": "TEST_GATEWAY", "backends": {"demo": {"transport": "stdio", "command": "fixture"}},
        "singleflight": ["demo.read"], "denied": ["demo.blocked"],
        "module_chain_tools": ["demo.read", "demo.other", "demo.mutate", "demo.blocked"]})
    router = Router(profile, Telemetry(Journal(tmp_path / "logs", 4096)))
    settings = ToolPolicySettings(router, tmp_path / "tool-policy.json")
    registry = ModuleRegistry(router, tmp_path / "modules.json")
    router.modules = registry
    seen = []
    string_arg = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"], "additionalProperties": False}
    output = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    class Session:
        """Simulate upstream data while recording actual dispatches."""

        async def call_tool(self, name, arguments, meta):
            """Return synthetic content and a distinct raw text block."""
            assert meta is None or HOOK_META not in meta
            seen.append((name, arguments))
            await asyncio.sleep(0.01)
            value = arguments.get("value", "default")
            return types.CallToolResult(content=[types.TextContent(type="text", text="unfiltered fixture")],
                                        structuredContent={"value": value, "internal": "raw fixture"})

    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Publish a normal catalog and keep its worker running."""
        tools = {}
        for tool in ("read", "other", "mutate", "blocked", "unlisted"):
            annotations = types.ToolAnnotations(readOnlyHint=tool != "mutate")
            tools[f"demo.{tool}"] = types.Tool(name=tool, inputSchema=string_arg, outputSchema=output,
                                               annotations=annotations)
        publish(name, Session(), tools, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    return router, settings, registry, seen


def test_hooks_and_explicit_workflow(fixture_gateway):
    """Apply hooks around singleflight and preserve deny/read-only/schema gates."""
    router, _, registry, seen = fixture_gateway

    async def scenario():
        """Run two equivalent transformed calls and an explicit, two-step read chain."""
        async with router.running():
            prepare = definition("prepare", {"op": "object", "fields": {"value": "/input/value"}})
            clean = definition("clean", {"op": "object", "fields": {"value": "/input/value"}})
            chain = definition("chain",
                               {"op": "call", "tool": "demo.read", "save_as": "first"},
                               {"op": "object", "fields": {"value": "/steps/first/value"}},
                               {"op": "call", "tool": "demo.other", "save_as": "second"},
                               {"op": "object", "fields": {"first": "/steps/first/value", "second": "/steps/second/value"}})
            for revision, module in enumerate((prepare, clean, chain)):
                registry.save(Save(expected_revision=revision, module=module))
            registry.bind(Bind(expected_revision=3, tool="demo.read", phase="before", module="prepare"))
            registry.bind(Bind(expected_revision=4, tool="demo.read", phase="after", module="clean"))
            with pytest.raises(ModuleError):
                registry.bind(Bind(expected_revision=5, tool="demo.read", phase="after", module="chain"))
            with pytest.raises(ModuleError):
                registry.delete(Delete(expected_revision=5, id="prepare"))
            with pytest.raises(ModuleError, match="revision"):
                registry.save(Save(expected_revision=2, module=clean))
            loaded = ModuleRegistry(router, registry.path)
            assert loaded.catalog() == registry.catalog()

            first, second = await asyncio.gather(router.call("demo.read", {"value": "x", "ignored": 1}),
                                                  router.call("demo.read", {"value": "x", "ignored": 2}))
            assert seen == [("read", {"value": "x"})]
            assert first.structuredContent == second.structuredContent == {"value": "x"}
            assert "unfiltered" not in first.content[0].text and "raw fixture" not in first.content[0].text
            rejected = await router.call("demo.read", {"value": 12})
            assert rejected.isError and len(seen) == 1

            service = ModuleService(registry, router)
            with pytest.raises(ModuleError, match="revision"):
                await service.run(Run(id="chain", data={"value": "y"}, expected_revision=3))
            result = await service.run(Run(id="chain", data={"value": "y"}, expected_revision=5))
            assert result["result"] == {"first": "y", "second": "y"}
            assert [name for name, _ in seen] == ["read", "read", "other"]

            for target in ("demo.mutate", "demo.blocked", "demo.unlisted"):
                blocked = definition("unsafe", {"op": "call", "tool": target})
                revision = registry.save(Save(expected_revision=registry.state.revision, module=blocked))["revision"]
                with pytest.raises(ModuleError, match="read-only MCP step"):
                    await service.run(Run(id="unsafe", data={"value": "z"}, expected_revision=revision))
            assert [name for name, _ in seen] == ["read", "read", "other"]

            wrong = definition("wrong", {"op": "object", "fields": {"renamed": "/input/value"}})
            revision = registry.save(Save(expected_revision=registry.state.revision, module=wrong))["revision"]
            registry.bind(Bind(expected_revision=revision, tool="demo.read", phase="after", module="wrong"))
            failure = await router.call("demo.read", {"value": "y"})
            assert failure.isError and "after the upstream call" in failure.content[0].text
            assert len(seen) == 4

    asyncio.run(scenario())


def test_module_page_and_management_share_state(fixture_gateway):
    """Verify browser Basic/CSRF and MCP Bearer share durable revisioned state."""
    router, settings, registry, seen = fixture_gateway
    app = create_app(router, 18792, TOKEN, settings=settings)
    browser = {"Authorization": "Basic " + b64encode(f"gateway:{TOKEN}".encode()).decode()}
    bearer = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}

    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/modules").status_code == 401
        assert client.get("/modules", headers=bearer).status_code == 401
        page = client.get("/modules", headers=browser)
        assert 'aria-current="page">Модули' in page.text
        assert 'href="/modules"' in client.get("/", headers=browser).text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        draft = {"id": "module", "title": "<script>demo</script>",
                 "steps": [{"op": "select", "source": "/input/value"}]}
        fields = {"csrf": csrf, "action": "save", "revision": "0", "data": json.dumps(draft)}
        assert client.post("/modules", headers=browser, data=fields).status_code == 403
        headers = {**browser, "Origin": ORIGIN}
        assert client.post("/modules", headers=headers, data={**fields, "csrf": "invalid"}).status_code == 403
        saved = client.post("/modules", headers=headers, data=fields, follow_redirects=False)
        assert saved.status_code == 303 and registry.state.revision == 1
        assert "<script>demo</script>" not in client.get("/modules", headers=browser).text

        def mcp(name, arguments, endpoint="/manage/test"):
            """Invoke the SDK endpoint and decode the MCP result."""
            response = client.post(endpoint, headers=bearer, json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return response.json()["result"]

        observed = mcp("gateway_modules_list", {})["structuredContent"]
        assert observed["revision"] == 1 and "module" in observed["modules"]
        args = {"id": "module", "expected_revision": 1, "data": {"value": "from module"}}
        assert mcp("gateway_module_run", args)["structuredContent"]["result"] == "from module"
        assert mcp("gateway_module_run", args, "/mcp/test")["isError"]
        rendered = client.post("/modules", headers=headers, data={"csrf": csrf, "action": "run",
                              "revision": "1", "id": "module", "data": '{"value":"<script>unsafe</script>"}'})
        assert rendered.status_code == 200 and "&lt;script&gt;unsafe&lt;/script&gt;" in rendered.text
        assert seen == []


def test_hook_sessions_promote_once_and_remain_isolated(fixture_gateway):
    """Count only completed applications in distinct server-issued sessions."""
    router, _, registry, seen = fixture_gateway

    async def scenario():
        """Activate a projection in two sessions and verify counts, isolation and revocation."""
        async with router.running():
            module = definition("scoped", {"op": "object", "fields": {"value": "/input/value"}})
            registry.save(Save(expected_revision=0, module=module))
            registry.set_generations(SetGenerations(expected_revision=1, rules=GenerationRules(gen2_sessions=2,
                                                                                                gen1_sessions=3)))
            for count in (1, 2, 3):
                lease = registry.sessions.open()
                token = lease["capability"]
                request = ActivateHook(expected_revision=2, capability=token, tool="demo.read",
                                       phase="after", module="scoped")
                registry.activate(request)
                meta = {HOOK_META: token}
                first = await router.call("demo.read", {"value": "x"}, metadata=meta)
                second = await router.call("demo.read", {"value": "x"}, metadata=meta)
                assert first.structuredContent == second.structuredContent == {"value": "x"}
                ordinary = await router.call("demo.read", {"value": "x"})
                assert ordinary.structuredContent["internal"] == "raw fixture"
                catalog = registry.catalog()
                life = catalog["hooks"]["scoped"]
                assert life["generation"] == 4 - count and len(life["sessions"]) == count
                assert life["sessions"][-1]["id"] == lease["session_id"]
                assert token not in json.dumps(catalog) and token not in registry.path.read_text()
                registry.sessions.close(token)
                assert (await router.call("demo.read", {"value": "x"}, metadata=meta)).isError
                assert len(seen) == count * 3

            reload = ModuleRegistry(router, registry.path)
            assert len(reload.catalog()["hooks"]["scoped"]["sessions"]) == 3
            assert reload.catalog()["active_sessions"] == []
            assert (await router.call("demo.read", {"value": "x"}, metadata={HOOK_META: "fake"})).isError
            assert len(seen) == 9

    asyncio.run(scenario())


def test_hook_sweep_protects_pins_and_active_bindings(fixture_gateway):
    """Collect idle templates, preserving permanent bindings, pins and in-flight use."""
    router, _, registry, _ = fixture_gateway

    async def scenario():
        """Exercise template collection and reject attempts to shadow profile hooks."""
        async with router.running():
            for revision, name in enumerate(("idle", "pinned", "shared", "active")):
                module = definition(name, {"op": "select", "source": "/input"})
                registry.save(Save(expected_revision=revision, module=module))
            registry.pin(PinHook(expected_revision=4, id="pinned", pinned=True))
            registry.bind(Bind(expected_revision=5, tool="demo.read", phase="before", module="shared"))
            lease = registry.sessions.open()
            meta = lease["capability"]
            with pytest.raises(ModuleError, match="cannot replace"):
                registry.activate(ActivateHook(expected_revision=6, capability=meta, tool="demo.read",
                                               phase="before", module="active"))
            registry.activate(ActivateHook(expected_revision=6, capability=meta, tool="demo.read",
                                           phase="after", module="active"))
            now = int(time.time())
            aged = {key: life.model_copy(update={"created_at": now - 86400})
                    for key, life in registry.state.lifecycle.items()}
            registry._write(registry.state.model_copy(update={"lifecycle": aged}))
            with pytest.raises(ModuleError, match="template expired"):
                registry.activate(ActivateHook(expected_revision=6, capability=meta, tool="demo.other",
                                               phase="after", module="idle"))
            registry.sweep(now)
            assert set(registry.state.modules) == {"pinned", "shared", "active"}
            _, _, claim = registry.for_tool("demo.read", router.routes["demo.read"], meta)
            registry.sessions.close(meta)
            registry.sweep(now)
            assert "active" in registry.state.modules
            registry.release(claim)
            registry.sweep(now)
            assert set(registry.state.modules) == {"pinned", "shared"}
            assert registry.state.revision == 8
            assert (await router.call("demo.read", {"value": "x"}, metadata={HOOK_META: meta})).isError

    asyncio.run(scenario())


def test_required_null_survives_disk_catalog_and_resave(fixture_gateway):
    """Persist an explicit JSON null in filter definitions across each serialization."""
    router, _, registry, _ = fixture_gateway
    module = definition("nullable", {"op": "filter", "source": "/input", "pattern": None})
    registry.save(Save(expected_revision=0, module=module))
    document = json.loads(registry.path.read_text())
    assert document["modules"]["nullable"]["steps"][0]["pattern"] is None
    reopened = ModuleRegistry(router, registry.path)
    restored = reopened.catalog()["modules"]["nullable"]
    assert restored["steps"][0]["pattern"] is None
    reopened.save(Save.model_validate({"expected_revision": 1, "module": restored}))
    assert run_local(reopened.state.modules["nullable"], [None, False, 0]) == [None]


def test_scoped_hook_stays_revoked_after_schema_policy_or_definition_reverts(fixture_gateway):
    """Reject old activations after ABA edits, including in-flight success accounting."""
    router, settings, registry, seen = fixture_gateway

    async def scenario():
        """Revert each kind of edit and require explicit reactivation."""
        async with router.running():
            old = definition("scope", {"op": "contains", "source": "/input/value", "pattern": True},
                             {"op": "select", "source": "/input"})
            registry.save(Save(expected_revision=0, module=old))
            cap = registry.sessions.open()["capability"]
            meta = {HOOK_META: cap}
            binding = ActivateHook(expected_revision=1, capability=cap, tool="demo.read",
                                   phase="after", module="scope")
            registry.activate(binding)
            route = router.routes["demo.read"]
            worker = route.worker
            catalog = worker.tools
            original = route.tool.model_copy(update={"outputSchema": {"properties": {"value": {"const": True}}}})
            worker.tools = {**catalog, "demo.read": original}
            router._publish("demo", worker)
            registry.activate(binding)
            revised_tool = original.model_copy(update={"outputSchema": {"properties": {"value": {"const": 1}}}})
            worker.tools = {**catalog, "demo.read": revised_tool}
            router._publish("demo", worker)
            worker.tools = {**catalog, "demo.read": original}
            router._publish("demo", worker)
            rejected = await router.call("demo.read", {"value": "x"}, metadata=meta)
            assert rejected.isError and "Session hook changed" in rejected.content[0].text

            worker.tools = catalog
            router._publish("demo", worker)
            registry.activate(binding)
            settings.change(PolicyChange(expected_revision=0, tool="demo.read", policy="deny"))
            settings.change(PolicyChange(expected_revision=1, tool="demo.read", policy="singleflight"))
            rejected = await router.call("demo.read", {"value": "x"}, metadata=meta)
            assert rejected.isError and "Session hook changed" in rejected.content[0].text

            registry.activate(binding)
            _, _, in_flight = registry.for_tool("demo.read", router.routes["demo.read"], cap)
            changed = definition("scope", {"op": "contains", "source": "/input/value", "pattern": 1},
                                 {"op": "select", "source": "/input"})
            registry.save(Save(expected_revision=1, module=changed))
            registry.save(Save(expected_revision=2, module=old))
            registry.succeeded(in_flight)
            registry.release(in_flight)
            rejected = await router.call("demo.read", {"value": "x"}, metadata=meta)
            assert rejected.isError and "Session hook changed" in rejected.content[0].text
            assert seen == []
            assert not registry.catalog()["hooks"]["scope"]["sessions"]

            registry.activate(binding.model_copy(update={"expected_revision": 3}))
            accepted = await router.call("demo.read", {"value": "x"}, metadata=meta)
            assert not accepted.isError
            assert len(registry.catalog()["hooks"]["scope"]["sessions"]) == 1

    asyncio.run(scenario())
