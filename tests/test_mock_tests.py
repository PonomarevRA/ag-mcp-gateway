"""Exercise saved mock scenarios across a real stdio worker, browser forms and MCP."""

import asyncio
import json
import re
from html import unescape
from os import utime
from time import time

import pytest
from filelock import FileLock
from pydantic import ValidationError
from starlette.testclient import TestClient

from gateway.cli import executable, main
from gateway.http_app import create_app
from gateway.mcp_client import CatalogSession
from gateway.mock_test_git import import_git_catalog, read_git_catalog
from gateway.mock_test_page import draft_fields
from gateway.mock_test_models import SaveGroup
from gateway.mock_tests import GetRun, MockTestError, MockTests, RunScenario, SaveScenario, Scenario, ValidateScenario
from gateway.module_registry import Hooks, Module, ModuleRegistry, ModuleState
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry


ORIGIN = "http://127.0.0.1:18793"
TOKEN = "fixture-mock-tests-token"
AUTH = ("gateway", TOKEN)
MANAGEMENT = "/manage/test/mock-tests"
MCP_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
FORM_HEADERS = {"Origin": ORIGIN}
PROFILE = {
    "name": "mock-tests", "environment": "test", "management_bridge": True,
    "access_token_env": "MOCK_TESTS_GATEWAY_TOKEN", "mock_tests_enabled": True, "backends": {},
    "timeout_seconds": 2,
}
TOOL = "demo.find"
INPUT_SCHEMA = {
    "type": "object", "properties": {"query": {"type": "string"}, "raw": {"type": "string"}},
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"],
}
ARGUMENTS = {"query": "book"}
BEFORE = {
    "id": "before", "title": "Set query",
    "steps": [{"op": "object", "fields": {"query": "/input/raw"}}],
}
AFTER = {
    "id": "after", "title": "Retain items",
    "steps": [{"op": "object", "fields": {"items": "/input/items"}}],
}


def fixture_response(identity="book", body=None, **overrides):
    """Make one synthetic fixture for the expected normalized request."""
    return {
        "id": identity, "match": {"op": "exact", "value": ARGUMENTS},
        "outcome": "result", "structured_content": {"items": [{"id": "book"}]} if body is None else body,
        **overrides,
    }


def scenario_data(identity="find-book", **overrides):
    """Create a current revision candidate with stable tool and checks."""
    return {
        "id": identity, "title": identity, "tool": TOOL, "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA, "arguments": ARGUMENTS, "fixtures": [fixture_response()],
        "checks": [{"op": "error_code", "value": None}, {"op": "schema"},
                   {"op": "equals", "path": "/items/0/id", "value": "book"}],
        **overrides,
    }


@pytest.fixture
def lab(tmp_path):
    """Use a profile with no configured upstream and a dedicated local state directory."""
    profile = Profile.model_validate(PROFILE)
    router = Router(profile, Telemetry(Journal(tmp_path / "journal", 65_536)))
    directory = tmp_path / "tests"
    return MockTests(router, directory, executable()), router, directory


async def run_saved(service, scenario, revision=0):
    """Save the named scenario, execute its exact revision and retrieve the full result."""
    data = Scenario.model_validate(scenario)
    saved = await service.save(SaveScenario(scenario=data, expected_revision=revision))
    request = RunScenario(id=data.id, expected_revision=saved["revision"])
    summary = await service.run(request)
    return await service.get_run(GetRun(run_id=summary["run_id"]))


def rpc(client, method, params=None, headers=MCP_HEADERS):
    """Call the ordinary management JSON-RPC endpoint over ASGI."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    return client.post(MANAGEMENT, headers=headers, json=request)


def call(client, name, arguments):
    """Return the typed management tool result, including protocol-visible errors."""
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200
    return response.json()["result"]


def test_mock_run_uses_real_stdio_and_gateway_hooks(lab, monkeypatch):
    """Observe the SDK request without replacing it, and prohibit an HTTP upstream."""
    _, router, directory = lab
    profile = Profile.model_validate({**PROFILE, "backends": {
        "demo": {"transport": "http", "url": "https://unreachable.example.invalid/mcp"},
    }})
    catalog_router = Router(profile, router.telemetry)
    service = MockTests(catalog_router, directory, executable())
    requests = []
    original = CatalogSession.request_tool

    async def record(self, name, arguments, metadata):
        """Capture exact on-wire arguments, then send the actual request to the worker."""
        requests.append((name, arguments))
        return await original(self, name, arguments, metadata)

    def refuse_http(*_args, **_kwargs):
        """Fail if the test runner attempts to construct a live HTTP upstream."""
        pytest.fail("A mock test opened an HTTP upstream")

    monkeypatch.setattr(CatalogSession, "request_tool", record)
    monkeypatch.setattr("gateway.upstream.streamable_http_client", refuse_http)
    fixture = fixture_response(body={"items": [{"id": "book"}], "internalNote": "drop-this"})
    checks = [
        {"op": "equals", "path": "/items/0/id", "value": "book"},
        {"op": "absent", "path": "/internalNote"},
        {"op": "schema"},
    ]
    run = asyncio.run(run_saved(service, scenario_data(arguments={"raw": "book"}, fixtures=[fixture],
                                                        before=BEFORE, after=AFTER, checks=checks)))
    assert requests == [("find", ARGUMENTS)]
    assert catalog_router.statuses == {"demo": "starting"}
    assert run["passed"] and all(check["passed"] for check in run["checks"])
    assert run["profile"] == "test/mock-tests" and run["upstream_calls"] == 1
    assert run["fixture_id"] == "book" and run["error_code"] is None
    assert run["response"]["structuredContent"] == {"items": [{"id": "book"}]}
    trace = run["trace"]
    assert trace["before_hook"] == trace["after_hook"] == "applied"
    assert trace["arguments_changed"] and trace["dispatched"]
    assert trace["output_schema"] == "passed"


@pytest.mark.parametrize(("case", "fixture", "expected", "denied", "calls"), [
    ("empty", fixture_response(body={"items": []}), None, False, 1),
    ("tool-error", fixture_response(outcome="tool_error", structured_content=None, text="fixture error"),
     "upstream_tool_error", False, 1),
    ("bad-input", fixture_response(), "invalid_mock_arguments", False, 1),
    ("bad-schema", fixture_response(body={"items": "bad type"}), "invalid_upstream_output", False, 1),
    ("timeout", fixture_response(outcome="timeout", structured_content=None), "upstream_timeout", False, 1),
    ("denied", fixture_response(), "policy_denied", True, 0),
    ("no-match", fixture_response(match={"op": "exact", "value": {"query": "other"}}),
     "mock_no_match", False, 1),
])
def test_mock_run_separates_expected_failures(lab, case, fixture, expected, denied, calls):
    """Compare tool, schema, transport, policy and matcher outcomes independently."""
    service, _, _ = lab
    checks = [{"op": "error_code", "value": expected}]
    if case == "empty":
        checks.append({"op": "equals", "path": "/items", "value": []})
    input_schema = {**INPUT_SCHEMA, "required": ["raw"]} if case == "bad-input" else INPUT_SCHEMA
    data = scenario_data(case, input_schema=input_schema, fixtures=[fixture], checks=checks, after=AFTER, denied=denied)
    run = asyncio.run(run_saved(service, data))
    assert run["passed"], run
    assert run["error_code"] == expected and run["upstream_calls"] == calls
    assert bool(run["response"].get("isError")) == (expected is not None)
    if expected is not None:
        assert run["trace"]["after_hook"] == "skipped"
    if case == "bad-schema":
        assert run["trace"]["output_schema"] == "failed"
    if case == "denied":
        assert not run["trace"]["dispatched"] and run["fixture_id"] is None
    if case == "bad-input":
        assert run["trace"]["dispatched"] and not run["trace"]["fixture_rules"]
        assert run["fixture_id"] is None


def test_mock_validation_detects_ambiguous_match(lab):
    """Reject distinct matching rules and expose their overlap to a scenario author."""
    service, _, _ = lab
    pointer_rule = fixture_response("by-pointer", match={"op": "pointer_equals", "path": "/query", "value": "book"})
    scenario = Scenario.model_validate(scenario_data(fixtures=[fixture_response(), pointer_rule]))

    async def verify():
        """Ask the validator and persistence fence about the same overlap."""
        result = await service.validate(ValidateScenario(scenario=scenario))
        assert not result["valid"]
        assert "mock_ambiguous_match" in json.dumps(result)
        with pytest.raises(MockTestError):
            await service.save(SaveScenario(scenario=scenario, expected_revision=0))

    asyncio.run(verify())


def test_profile_before_hook_changes_the_fixture_candidates(lab):
    """Treat inherited hook output as the arguments eventually seen by the matcher."""
    service, router, directory = lab
    registry = ModuleRegistry(router, directory / "live-modules.json")
    registry.state = ModuleState(modules={"before": Module.model_validate(BEFORE)},
                                 bindings={TOOL: Hooks(before="before")}, revision=3)
    router.modules = registry
    initial = {"query": "book", "raw": "changed"}
    fixtures = [
        fixture_response(match={"op": "exact", "value": initial}),
        fixture_response("by-pointer", match={"op": "pointer_equals", "path": "/query", "value": "book"}),
        fixture_response("changed", match={"op": "exact", "value": {"query": "changed"}}),
    ]
    run = asyncio.run(run_saved(service, scenario_data(arguments=initial, fixtures=fixtures)))
    assert run["passed"] and run["fixture_id"] == "changed"
    trace = run["trace"]
    assert trace["effective_hooks"]["before"] == {"id": "before", "source": "profile"}
    assert trace["module_revision"] == 3 and trace["arguments_changed"]


def test_mock_can_intentionally_exercise_ambiguous_runtime_rules(lab):
    """Return an ambiguity diagnostic from the worker when the scenario explicitly expects it."""
    service, _, _ = lab
    pointer_rule = fixture_response("by-pointer", match={"op": "pointer_equals", "path": "/query", "value": "book"})
    data = scenario_data(fixtures=[fixture_response(), pointer_rule],
                         checks=[{"op": "error_code", "value": "mock_ambiguous_match"}])
    run = asyncio.run(run_saved(service, data))
    assert run["passed"] and run["error_code"] == "mock_ambiguous_match"
    assert run["upstream_calls"] == 1 and run["fixture_id"] is None
    assert run["response"]["isError"]


def test_failed_delivery_keeps_transport_outcome_even_with_a_mismatch_receipt(lab, monkeypatch):
    """A local match receipt cannot turn an unobserved tool error into a received one."""
    service, _, _ = lab
    original = CatalogSession.request_tool

    async def delay_delivery(self, name, arguments, metadata):
        """Send to the real stdio worker but delay returning its response to Router."""
        await original(self, name, arguments, metadata)
        await asyncio.Event().wait()

    monkeypatch.setattr(CatalogSession, "request_tool", delay_delivery)
    fixture = fixture_response(match={"op": "exact", "value": {"query": "other"}})
    data = scenario_data(fixtures=[fixture], checks=[{"op": "error_code", "value": "upstream_timeout"}])
    run = asyncio.run(run_saved(service, data))
    assert run["passed"] and run["error_code"] == "upstream_timeout"
    assert run["upstream_calls"] == 1 and run["fixture_id"] is None
    assert run["trace"]["match_error"] == "mock_no_match"


def test_parallel_mock_runs_isolate_responses_and_keep_original_revision(lab):
    """Run identical arguments in parallel and reopen an old immutable report."""
    service, router, directory = lab
    first = scenario_data("first")
    second = scenario_data("second", fixtures=[fixture_response("second", {"items": [{"id": "other"}]})],
                           checks=[{"op": "equals", "path": "/items/0/id", "value": "other"}])

    async def verify():
        """Assert isolation, conflict rejection and report recovery after service restart."""
        left, right = await asyncio.gather(run_saved(service, first), run_saved(service, second))
        assert left["passed"] and right["passed"]
        assert left["run_id"] != right["run_id"]
        assert left["fixture_id"] == "book" and right["fixture_id"] == "second"
        assert left["response"]["structuredContent"]["items"][0]["id"] == "book"
        assert right["response"]["structuredContent"]["items"][0]["id"] == "other"

        original = await service.save(SaveScenario(scenario=Scenario.model_validate({**first, "title": "Revised"}),
                                                   expected_revision=left["revision"]))
        assert original["revision"] > left["revision"]
        with pytest.raises(MockTestError) as stale:
            await service.run(RunScenario(id="first", expected_revision=left["revision"]))
        assert stale.value.code == "revision_conflict"
        restored = MockTests(router, directory, executable())
        report = await restored.get_run(GetRun(run_id=left["run_id"]))
        assert report == left
        other = Router(Profile.model_validate({**PROFILE, "name": "another"}), router.telemetry)
        separate = MockTests(other, directory.with_name("another-tests"), executable())
        with pytest.raises(MockTestError) as unknown:
            await separate.get_run(GetRun(run_id=left["run_id"]))
        assert unknown.value.code == "run_not_found"

    asyncio.run(verify())


def test_editing_during_a_mock_run_applies_to_the_next_revision(lab):
    """Keep fixture selection and assertions tied to the revision originally dispatched."""
    service, _, _ = lab
    timeout_fixture = fixture_response("slow", outcome="timeout", structured_content=None)
    original = scenario_data(fixtures=[timeout_fixture], checks=[{"op": "error_code", "value": "upstream_timeout"}])

    async def verify():
        """Edit while the original worker starts, then run the newly saved definition."""
        old = await service.save(SaveScenario(scenario=Scenario.model_validate(original), expected_revision=0))
        running = asyncio.create_task(service.run(RunScenario(id=original["id"], expected_revision=old["revision"])))
        await asyncio.sleep(0)
        assert not running.done()
        new = await run_saved(service, scenario_data(), revision=old["revision"])
        previous = await running
        assert previous["passed"] and previous["fixture_id"] == "slow"
        assert previous["revision"] == old["revision"] and previous["error_code"] == "upstream_timeout"
        assert new["passed"] and new["fixture_id"] == "book" and new["revision"] > previous["revision"]

    asyncio.run(verify())


def test_mock_reports_redact_secret_fields_and_preserve_failed_check(lab):
    """Keep only safe values in persisted reports and explain the failed check."""
    service, _, directory = lab
    marker = "fixture-private-token-value"
    fixture = fixture_response(
        body={"items": [{"id": "book"}], "access_token": marker},
        match={"op": "pointer_equals", "path": "/access_token", "value": marker},
        text=json.dumps({"api_key": marker}),
    )
    checks = [
        {"op": "equals", "path": "/items/0/id", "value": "wrong-book"},
        {"op": "equals", "path": "/access_token", "value": marker},
    ]
    data = scenario_data(input_schema={"type": "object"}, arguments={"access_token": marker},
                         fixtures=[fixture], checks=checks)
    run = asyncio.run(run_saved(service, data))
    assert not run["passed"]
    check = run["checks"][0]
    assert not check["passed"] and check["path"] == "/items/0/id"
    assert check["expected"] == "wrong-book" and check["actual"] == "book"
    assert marker not in json.dumps(run)
    reports = [path for path in directory.rglob("*.json") if run["run_id"] in path.name]
    assert reports
    assert all(marker not in path.read_text(encoding="utf-8") for path in reports)


def test_idle_mock_sweep_deletes_expired_reports_without_a_new_run(lab):
    """Remove an old report while preserving a recent report in the same profile."""
    service, _, directory = lab
    run = asyncio.run(run_saved(service, scenario_data()))
    recent = directory / "runs" / f'{run["run_id"]}.json'
    expired_id = "e" * 32
    expired = recent.with_name(f"{expired_id}.json")
    expired.write_bytes(recent.read_bytes())
    old_timestamp = time() - 2 * 86_400
    utime(expired, (old_timestamp, old_timestamp))

    service.prune_reports()

    assert recent.exists() and not expired.exists()
    assert asyncio.run(service.get_run(GetRun(run_id=run["run_id"]))) == run
    with pytest.raises(MockTestError) as missing:
        asyncio.run(service.get_run(GetRun(run_id=expired_id)))
    assert missing.value.code == "run_not_found"


def test_browser_and_mcp_share_a_saved_scenario_and_escape_responses(lab, tmp_path):
    """Verify auth, actual management calls, mutation CSRF and a browser report URL."""
    service, router, _ = lab
    settings = ToolPolicySettings(router, tmp_path / "policies.json")
    app = create_app(router, 18793, TOKEN, settings=settings, mock_tests=service)
    markup = "<b>synthetic plain text</b>"
    fixture = fixture_response(body={"items": [{"id": markup}]}, text=markup)
    scenario = scenario_data(fixtures=[fixture], checks=[{"op": "equals", "path": "/items/0/id", "value": markup}])
    commands = {"gateway_test_list", "gateway_test_validate", "gateway_test_save", "gateway_test_group_save",
                "gateway_test_move", "gateway_test_run",
                "gateway_test_run_get"}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/tests").status_code == 401
        assert rpc(client, "tools/list", headers={"Accept": MCP_HEADERS["Accept"]}).status_code == 401
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert commands <= {tool["name"] for tool in tools}
        write_actions = {"gateway_test_save", "gateway_test_group_save", "gateway_test_move", "gateway_test_run"}
        for tool in tools:
            if tool["name"] in commands:
                assert tool["annotations"]["readOnlyHint"] == (tool["name"] not in write_actions)
        saved = call(client, "gateway_test_save", {"scenario": scenario, "expected_revision": 0})["structuredContent"]
        suite = {"id": scenario["id"], "expected_revision": saved["revision"]}
        agent = call(client, "gateway_test_run", suite)["structuredContent"]
        agent_report = call(client, "gateway_test_run_get", {"run_id": agent["run_id"]})["structuredContent"]

        editor = client.get("/tests", params={"edit": scenario["id"]}, auth=AUTH)
        assert editor.status_code == 200 and markup not in editor.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', editor.text)
        assert csrf is not None
        form = {"action": "run", "id": scenario["id"], "revision": saved["revision"], "csrf": csrf.group(1)}
        assert client.post("/tests", data=form, auth=AUTH).status_code == 403
        assert client.post("/tests", data={**form, "csrf": "wrong"}, auth=AUTH, headers=FORM_HEADERS).status_code == 403
        submitted = client.post("/tests", data=form, auth=AUTH, headers=FORM_HEADERS, follow_redirects=False)
        assert submitted.status_code == 303
        report_url = submitted.headers["location"]
        assert report_url.startswith("/tests/runs/")
        page = client.get(report_url, auth=AUTH)
        assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
        assert all(f'id="{section}"' in page.text for section in ("checks", "response", "trace"))
        assert markup not in page.text and "&lt;b&gt;synthetic plain text&lt;/b&gt;" in page.text
        browser = client.get(report_url + "/json", auth=AUTH)
        assert browser.status_code == 200
        web_report = browser.json()
        assert web_report["run_id"] != agent["run_id"]
        for key in ("passed", "revision", "checks", "response", "error_code", "fixture_id"):
            assert web_report[key] == agent_report[key], key
        assert "mock-result-tab-response" in page.text
        assert client.get("/assets/mock-test-workspace.css", auth=AUTH).status_code == 200
        assert client.get("/assets/mock-test-tabs.js", auth=AUTH).status_code == 200

        edited = draft_fields(saved["scenario"], saved["revision"])
        saved_run = client.post("/tests", data={**edited, "csrf": csrf.group(1), "action": "save-run"},
                                auth=AUTH, headers=FORM_HEADERS, follow_redirects=False)
        assert saved_run.status_code == 303 and saved_run.headers["location"].startswith("/tests?edit=")
        workspace = client.get(saved_run.headers["location"], auth=AUTH)
        assert 'id="scenario-editor"' in workspace.text and 'id="run-summary"' in workspace.text
        assert "mock-request-tab-fixtures" in workspace.text and "mock-result-tab-trace" in workspace.text
        assert 'aria-keyshortcuts="Meta+Enter Control+Enter"' in workspace.text
        assert 'form="mock-saved-run" name="action" value="run"' in workspace.text
        assert 'class="mock-properties" data-ag-width="fill">' in workspace.text
        assert "&lt;b&gt;synthetic plain text&lt;/b&gt;" in workspace.text and markup not in workspace.text


def test_agent_collections_and_browser_moves_use_independent_revisions(lab, tmp_path):
    """Move legacy and grouped requests without invalidating their execution snapshots."""
    service, router, directory = lab
    settings = ToolPolicySettings(router, tmp_path / "policies.json")
    app = create_app(router, 18793, TOKEN, settings=settings, mock_tests=service)
    with TestClient(app, base_url=ORIGIN) as client:
        for key, title in (("mixed", "<Mixed MCP>"), ("orders", "Orders")):
            group = {"id": key, "title": title, "expected_revision": 0}
            assert call(client, "gateway_test_group_save", group)["structuredContent"]["revision"] == 1
        legacy = Scenario.model_validate(scenario_data("legacy"))
        path = service._scenario_path(legacy.id)
        path.write_text(json.dumps({"scenario": legacy.model_dump(), "revision": 1}), encoding="utf-8")
        commands = [
            scenario_data("from-demo"),
            scenario_data("from-orders", tool="orders.find", arguments={"raw": "book"}, before=BEFORE, after=AFTER),
        ]
        for data in commands:
            assert call(client, "gateway_test_save", {
                "scenario": data, "expected_revision": 0, "group_id": "mixed",
            })["structuredContent"]["group_id"] == "mixed"
        listed = call(client, "gateway_test_list", {"group_id": "mixed"})["structuredContent"]
        assert {item["backend"] for item in listed["scenarios"]} == {"demo", "orders"}
        only_orders = call(client, "gateway_test_list", {"backend": "orders"})["structuredContent"]
        assert [item["id"] for item in only_orders["scenarios"]] == ["from-orders"]
        ungrouped = call(client, "gateway_test_list", {"group_id": None})["structuredContent"]
        assert [item["id"] for item in ungrouped["scenarios"]] == ["legacy"]

        viewing = client.get("/tests?edit=from-orders&mode=view", auth=AUTH)
        assert "mock-request-tab-hooks" in viewing.text and "Retain items" in viewing.text
        assert 'id="mock-call-form"' not in viewing.text and 'action="/tests/move"' not in viewing.text
        assert "<Mixed MCP>" not in viewing.text and "&lt;Mixed MCP&gt;" in viewing.text
        editing = client.get("/tests?edit=from-orders", auth=AUTH)
        assert 'id="mock-call-form"' in editing.text and 'action="/tests/move"' in editing.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', editing.text).group(1)
        invalid_hook = draft_fields(commands[1], 1)
        invalid_hook.update(csrf=csrf, action="save", after="{broken")
        rejected = client.post("/tests", data=invalid_hook, auth=AUTH, headers=FORM_HEADERS)
        assert rejected.status_code == 400
        assert 'data-selected="mock-request-hooks"' in rejected.text and "{broken" in rejected.text
        assert 'name="after" rows="4"' in rejected.text and 'aria-invalid="true"' in rejected.text
        move_form = {"csrf": csrf, "id": "from-orders", "group_id": "orders", "revision": "0",
                     "current": "from-orders"}
        assert client.post("/tests/move", data=move_form, auth=AUTH).status_code == 403
        headers = {**FORM_HEADERS, "Accept": "application/json"}
        moved = client.post("/tests/move", data=move_form, auth=AUTH, headers=headers)
        assert moved.json() == {"id": "from-orders", "group_id": "orders", "placement_revision": 1}
        stale = client.post("/tests/move", data=move_form, auth=AUTH, headers=headers)
        assert stale.status_code == 409 and stale.json()["error"] == "placement_conflict"
        missing = client.post("/tests/move", data={**move_form, "group_id": "missing", "revision": "1"},
                              auth=AUTH, headers=headers)
        assert missing.status_code == 404
        result = call(client, "gateway_test_run", {"id": "from-orders", "expected_revision": 1})["structuredContent"]
        assert result["passed"] and result["upstream_calls"] == 1
        assert result["fixture_id"] == "book"
        edited = call(client, "gateway_test_save", {"scenario": commands[1], "expected_revision": 1})
        assert edited["structuredContent"]["revision"] == 2
        assert edited["structuredContent"]["group_id"] == "orders"
        assert edited["structuredContent"]["placement_revision"] == 1
        run_form = {"csrf": csrf, "action": "run", "id": "from-orders", "revision": "2", "mode": "view"}
        run = client.post("/tests", data=run_form, auth=AUTH, headers=FORM_HEADERS, follow_redirects=False)
        assert run.status_code == 303 and "mode=view" in run.headers["location"]
        assert 'id="run-summary"' in client.get(run.headers["location"], auth=AUTH).text

        rename = {"csrf": csrf, "id": "orders", "title": "Order checks", "revision": "1", "current": "from-orders"}
        renamed = client.post("/tests/groups", data=rename, auth=AUTH, headers=FORM_HEADERS, follow_redirects=False)
        assert renamed.status_code == 303
        assert client.post("/tests/groups", data=rename, auth=AUTH, headers=FORM_HEADERS).status_code == 409
        assert call(client, "gateway_test_move", {"id": "legacy", "group_id": "mixed",
                                                   "expected_placement_revision": 0})["structuredContent"]["placement_revision"] == 1

    restored = MockTests(router, directory, executable())
    assert restored.scenario("from-orders")["group_id"] == "orders"
    assert restored.scenario("legacy")["revision"] == 1
    assert {entry["title"] for entry in asyncio.run(restored.list())["groups"]} == {"<Mixed MCP>", "Order checks"}


def test_mock_catalog_cli_round_trips_authored_files_and_preserves_local_state(lab, tmp_path, capsys):
    """Reapply Git edits with unchanged bytes, stable IDs and separate request/placement revisions."""
    _, router, _ = lab
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "mock-tests.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    directory = tmp_path / "state" / router.profile.state_path
    service = MockTests(router, directory / "mock-tests", executable())
    asyncio.run(service.save_group(SaveGroup(id="Books", title="Book checks", expected_revision=0)))
    scenario = Scenario.model_validate(scenario_data("Book", after=AFTER))
    asyncio.run(service.save(SaveScenario(scenario=scenario, group_id="Books", expected_revision=0)))
    asyncio.run(service.save(SaveScenario(scenario=Scenario.model_validate(scenario_data("book")), expected_revision=0)))
    run = asyncio.run(service.run(RunScenario(id="Book", expected_revision=1)))
    root = tmp_path / "repository" / "mock-catalog"
    args = ["--profiles", str(profiles), "--state", str(tmp_path / "state"), "--profile", "mock-tests",
            "mock-catalog"]
    assert main([*args, "export", str(root)]) == 0
    original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")}
    assert set(map(str, original)) == {
        "mock-catalog.json", "collections/id-~books.json", "requests/id-~book.json", "requests/id-book.json",
    }
    assert all(b'"revision"' not in data and b'"run_id"' not in data for data in original.values())
    assert main([*args, "export", str(root)]) == 0
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")} == original
    assert main(["mock-catalog", "check", str(root)]) == 0
    request_path = root / "requests" / "id-~book.json"
    request = json.loads(request_path.read_bytes())
    request["scenario"]["title"] = "Reviewed in Git"
    request["group_id"] = None
    request_path.write_text(json.dumps(request), encoding="utf-8")
    (root / "requests" / "id-book.json").unlink()
    with FileLock(str(directory / "gateway.lock"), timeout=0):
        assert main([*args, "import", str(root)]) == 1
    assert service.scenario("Book")["revision"] == 1
    assert main([*args, "import", str(root)]) == 0
    assert main([*args, "import", str(root)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["changed"] == 0
    reopened = MockTests(router, directory / "mock-tests", executable())
    restored = reopened.scenario("Book")
    assert restored["revision"] == 2 and restored["placement_revision"] == 1
    assert restored["scenario"]["title"] == "Reviewed in Git" and restored["group_id"] is None
    assert reopened.scenario("book")["revision"] == 1
    assert asyncio.run(reopened.get_run(GetRun(run_id=run["run_id"])))["passed"]
    assert asyncio.run(reopened.run(RunScenario(id="Book", expected_revision=2)))["passed"]


def test_mock_catalog_rejects_invalid_git_files_and_survives_failed_import(lab, tmp_path, monkeypatch):
    """Keep the active catalog consistent after invalid or partially written input."""
    from gateway import mock_tests

    service, router, directory = lab
    asyncio.run(service.save_group(SaveGroup(id="books", title="Local name", expected_revision=0)))
    scenario = Scenario.model_validate(scenario_data("book"))
    asyncio.run(service.save(SaveScenario(scenario=scenario, expected_revision=0)))
    root = tmp_path / "bundle"
    from gateway.mock_test_git import export_git_catalog

    export_git_catalog(service, root)
    group_file = root / "collections" / "id-books.json"
    group_file.write_text('{"id":"books","title":"Reviewed name"}', encoding="utf-8")
    request_file = root / "requests" / "id-book.json"
    original = request_file.read_text(encoding="utf-8")
    request_file.write_text('{"group_id":"books",' + original.lstrip()[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        read_git_catalog(root)
    request_file.write_text(original, encoding="utf-8")
    catalog = read_git_catalog(root)
    persist = mock_tests.write_state

    def fail_on_request(path, data):
        """Simulate a failed stage after a new collection file has been written."""
        if path.parent.name == "scenarios":
            raise OSError("Simulated storage failure")
        persist(path, data)

    monkeypatch.setattr(mock_tests, "write_state", fail_on_request)
    with pytest.raises(OSError, match="Simulated storage failure"):
        import_git_catalog(service, catalog)
    reopened = MockTests(router, directory, executable())
    assert asyncio.run(reopened.list())["groups"][0]["title"] == "Local name"
    assert not list(directory.glob("catalog-*"))


@pytest.mark.parametrize("changes", [
    {"environment": "production"}, {"environment": None},
    {"management_bridge": False}, {"access_token_env": None},
])
def test_mock_feature_requires_an_authenticated_test_profile(changes):
    """Reject activation in other environments or outside authenticated management."""
    with pytest.raises(ValidationError):
        Profile.model_validate({**PROFILE, **changes})


def test_disabled_profile_exposes_neither_browser_nor_mcp_mock_operations(lab, tmp_path):
    """A service object cannot expose mocks when the selected profile is disabled."""
    service, _, _ = lab
    command, *arguments = executable()
    profile = Profile.model_validate({
        **PROFILE, "mock_tests_enabled": False,
        "backends": {"demo": {"transport": "stdio", "command": command, "args": [*arguments, "demo-backend"]}},
    })
    router = Router(profile, Telemetry(Journal(tmp_path / "disabled-journal", 65_536)))
    settings = ToolPolicySettings(router, tmp_path / "disabled-policies.json")
    app = create_app(router, 18793, TOKEN, settings=settings, mock_tests=service)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/tests", auth=AUTH).status_code == 404
        result = rpc(client, "tools/list").json()["result"]
        assert not any(tool["name"].startswith("gateway_test_") for tool in result["tools"])


def test_catalog_seeds_from_the_discovered_schema_without_calling_it(tmp_path):
    """Link an enabled browser catalog to its tool's editable mock scenario."""
    command, *arguments = executable()
    profile = Profile.model_validate({
        **PROFILE, "backends": {
            "demo": {"transport": "stdio", "command": command, "args": [*arguments, "demo-backend"]},
        },
    })
    router = Router(profile, Telemetry(Journal(tmp_path / "catalog-journal", 65_536)))
    service = MockTests(router, tmp_path / "tests", executable())
    settings = ToolPolicySettings(router, tmp_path / "catalog-policies.json")
    app = create_app(router, 18793, TOKEN, settings=settings, mock_tests=service)
    with TestClient(app, base_url=ORIGIN) as client:
        catalog = client.get("/tools", auth=AUTH)
        assert 'href="/tests?tool=demo.echo"' in catalog.text
        seed = client.get("/tests", params={"tool": "demo.echo"}, auth=AUTH)
        assert seed.status_code == 200
        assert 'value="demo.echo"' in seed.text
        field = re.search(r'<textarea[^>]*name="input_schema"[^>]*>(.*?)</textarea>', seed.text, re.S)
        assert field is not None
        schema = json.loads(unescape(field.group(1)))
        assert schema["required"] == ["text"]
        assert router.telemetry.snapshot()["total"]["upstream"] == 0
