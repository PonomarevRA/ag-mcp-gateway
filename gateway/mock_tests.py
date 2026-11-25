"""Persist profile-local mock scenarios and run them through an isolated MCP router."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from shutil import rmtree
from tempfile import TemporaryDirectory
from time import monotonic, time
from uuid import uuid4

from mcp import types
from pydantic import ValidationError

from .mock_test_models import (
    EXAMPLE_SCENARIO, MAX_COLLECTIONS, MAX_REQUESTS, MAX_SCENARIO_BYTES, MAX_SCENARIO_RECORD_BYTES,
    GetRun, GroupRecord, ListScenarios, MockTestError,
    MoveScenario, RunScenario, SaveGroup, SaveScenario, Scenario, ScenarioRecord, ScenarioTemplate,
    ValidateScenario, WorkerReceipt, encode_scenario_record,
    select_fixture, validation_issues,
)
from .mock_test_report import MAX_REPORT_BYTES, report_data
from .module_data import bounded_json
from .module_registry import Hooks, Module, ModuleRegistry, ModuleState
from .policy_settings import write_state
from .profile import Profile
from .router import Router, tool_error
from .telemetry import Journal, Telemetry

_MAX_REPORTS = 64
_MAX_ACTIVE = 4
_REPORT_TTL_SECONDS = 86_400
_WORKER_STARTUP_SECONDS = 10
_WORKER_CALL_SECONDS = 1


def _read(path: Path, limit: int) -> bytes:
    """Bound reads from the profile state and worker receipt files."""
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise MockTestError("state_limit", "Stored mock test data exceeds its byte limit.")
    return data


def _read_dict(path: Path, limit: int) -> dict:
    """Open a bounded local JSON object without including storage contents in errors."""
    data = _read(path, limit)
    try:
        value = json.loads(data)
    except (ValueError, RecursionError) as error:
        raise MockTestError("state_invalid", "Stored mock test data is invalid.") from error
    if not isinstance(value, dict):
        raise MockTestError("state_invalid", "Stored mock test data is invalid.")
    return value


def _match_trace(path: Path, scenario: Scenario) -> tuple[int, str | None, str | None, list[dict]]:
    """Construct diagnostics from the authored rules and the worker's ordered match bits."""
    try:
        receipt = WorkerReceipt.model_validate_json(_read(path, 512))
    except FileNotFoundError:
        return 0, None, None, []
    except ValidationError as error:
        raise MockTestError("state_invalid", "Stored mock worker receipt is invalid.") from error
    if receipt.matches is None:
        return receipt.upstream_calls, None, "invalid_mock_arguments", []
    if len(receipt.matches) != len(scenario.fixtures):
        raise MockTestError("state_invalid", "Mock worker receipt does not match its scenario.")
    fixture, error_code = select_fixture(scenario.fixtures, receipt.matches)
    rules = []
    for item, matched in zip(scenario.fixtures, receipt.matches, strict=True):
        rule = {"id": item.id, "op": item.match.op, "matched": matched}
        pointer_path = getattr(item.match, "path", None)
        if pointer_path is not None:
            rule["path"] = pointer_path
        rules.append(rule)
    return receipt.upstream_calls, fixture.id if fixture is not None else None, error_code, rules


def _effective_hook(scenario: Scenario, phase: str, state: ModuleState) -> tuple[Module | None, str]:
    """Resolve a phase exactly as the isolated router will resolve it for this scenario."""
    authored = getattr(scenario, phase)
    if authored is not None:
        return authored, "scenario"
    bindings = state.bindings.get(scenario.tool, Hooks())
    return state.modules.get(getattr(bindings, phase)), "profile"


class MockTests:
    """Share scenario state, execution and reports between browser and management MCP."""

    def __init__(self, router: Router, directory: Path, command: list[str] | tuple[str, ...]):
        """Enable the service only for a test profile and use a trusted gateway launcher."""
        if router.profile.environment != "test" or not router.profile.mock_tests_enabled:
            raise MockTestError("test_profile_required", "Mock tests require an opted-in test profile.")
        self.router = router
        self.directory = directory.resolve()
        self.command = tuple(command)
        self.catalog = self._active_catalog()
        self.scenarios = self.catalog / "scenarios"
        self.groups = self.catalog / "groups"
        self.runs = self.directory / "runs"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.scenarios.mkdir(mode=0o700, exist_ok=True)
        self.groups.mkdir(mode=0o700, exist_ok=True)
        self.runs.mkdir(mode=0o700, exist_ok=True)
        self.journal = Journal(self.directory / "journal", router.profile.log_max_bytes)
        self.active = 0
        self.commands = {
            "gateway_test_list": (ListScenarios, "List mock request collections and saved requests.", True, self.list),
            "gateway_test_template": (
                ScenarioTemplate,
                "Get a runnable mock scenario in the scenario field and the validate, save, run and report workflow. "
                "Call with {} before authoring a scenario.",
                True, self.template,
            ),
            "gateway_test_validate": (
                ValidateScenario,
                "Validate a draft in the scenario field. Call gateway_test_template for a complete example. "
                "The scenario requires id, title, tool and input_schema. Put synthetic responses in "
                "fixtures[].structured_content and assertions in checks, with JSON Pointer paths like /echo.",
                True, self.validate,
            ),
            "gateway_test_save": (
                SaveScenario,
                "Save the scenario from gateway_test_template or your validated draft. Pass scenario, "
                "expected_revision (0 to create), and optional group_id for a new scenario. "
                "Use the returned revision to run. Use gateway_test_move to move an existing scenario.",
                False, self.save,
            ),
            "gateway_test_group_save": (
                SaveGroup, "Create a collection with id, title and expected_revision=0, "
                "or rename it at the current revision.", False, self.save_group,
            ),
            "gateway_test_move": (MoveScenario, "Move a mock request to a collection or to ungrouped using its placement revision.", False, self.move),
            "gateway_test_run": (
                RunScenario, "Run a saved mock in a synthetic local MCP process. "
                "Pass the scenario id and expected_revision from save or list. Returns run_id and passed.",
                False, self.run,
            ),
            "gateway_test_run_get": (
                GetRun, "Read a retained mock report using run_id from gateway_test_run, "
                "including passed, checks and response.", True, self.get_run,
            ),
        }

    def _active_catalog(self) -> Path:
        """Open the selected generation or the original pre-import layout."""
        try:
            generation = _read(self.directory / "catalog-current", 32)
        except FileNotFoundError:
            return self.directory
        if len(generation) != 32 or any(byte not in b"0123456789abcdef" for byte in generation):
            raise MockTestError("state_invalid", "Stored mock catalog selector is invalid.")
        catalog = self.directory / ("catalog-" + generation.decode("ascii"))
        if not (catalog / "scenarios").is_dir() or not (catalog / "groups").is_dir():
            raise MockTestError("state_invalid", "Stored mock catalog generation is missing.")
        return catalog

    @staticmethod
    def _state_path(directory: Path, identity: str) -> Path:
        """Keep distinct IDs distinct on case-insensitive filesystems."""
        filename = sha256(identity.encode("utf-8")).hexdigest() + ".json"
        return directory / filename

    def _scenario_path(self, identity: str) -> Path:
        """Resolve a scenario filename within this profile."""
        return self._state_path(self.scenarios, identity)

    def _group_record(self, path: Path) -> GroupRecord:
        """Read bounded collection metadata from a single atomic document."""
        try:
            return GroupRecord.model_validate_json(_read(path, 2048))
        except ValidationError as error:
            raise MockTestError("state_invalid", "Stored collection is invalid.") from error

    def _require_group(self, group_id: str | None) -> None:
        """Ensure a destination collection exists in the current profile."""
        if group_id is None:
            return
        try:
            self._group_record(self._state_path(self.groups, group_id))
        except FileNotFoundError as error:
            raise MockTestError("group_not_found", "Collection does not exist.") from error

    def catalog_records(self) -> tuple[dict[str, GroupRecord], dict[str, ScenarioRecord]]:
        """Read all bounded definitions under the profile state lock."""
        groups = {}
        scenarios = {}
        for path in sorted(self.groups.glob("*.json"))[:MAX_COLLECTIONS]:
            group = self._group_record(path)
            groups[group.id] = group
        for path in sorted(self.scenarios.glob("*.json"))[:MAX_REQUESTS]:
            record = self._record(path)
            scenarios[record.scenario.id] = record
        return groups, scenarios

    def replace_catalog(self, groups: dict[str, GroupRecord], scenarios: dict[str, ScenarioRecord]) -> None:
        """Activate a complete staged generation while the caller holds the profile lock."""
        if len(groups) > MAX_COLLECTIONS or len(scenarios) > MAX_REQUESTS:
            raise MockTestError("catalog_capacity", "The test profile has reached its catalog limit.")
        generation = uuid4().hex
        catalog = self.directory / ("catalog-" + generation)
        scenario_dir, group_dir = catalog / "scenarios", catalog / "groups"
        catalog.mkdir(mode=0o700)
        try:
            scenario_dir.mkdir(mode=0o700)
            group_dir.mkdir(mode=0o700)
            for key, group in groups.items():
                write_state(self._state_path(group_dir, key), group.model_dump_json().encode("utf-8"))
            for key, record in scenarios.items():
                write_state(self._state_path(scenario_dir, key), encode_scenario_record(record))
            write_state(self.directory / "catalog-current", generation.encode("ascii"))
        except BaseException:
            # Leave the generation available if the atomic selector already switched.
            if self._active_catalog() != catalog:
                rmtree(catalog, ignore_errors=True)
            raise
        previous = self.catalog
        self.catalog, self.scenarios, self.groups = catalog, scenario_dir, group_dir
        if previous != self.directory:
            rmtree(previous, ignore_errors=True)

    async def save_group(self, request: SaveGroup) -> dict:
        """Create or rename a collection with optimistic concurrency."""
        path = self._state_path(self.groups, request.id)
        try:
            revision = self._group_record(path).revision
        except FileNotFoundError:
            revision = 0
        if revision != request.expected_revision:
            raise MockTestError("revision_conflict", "Collection revision changed. Refresh and retry.")
        if revision == 0 and sum(1 for _ in self.groups.glob("*.json")) >= MAX_COLLECTIONS:
            raise MockTestError("group_capacity", "The test profile has reached its collection limit.")
        record = GroupRecord(id=request.id, title=request.title, revision=revision + 1)
        write_state(path, record.model_dump_json().encode("utf-8"))
        return record.model_dump()

    async def move(self, request: MoveScenario) -> dict:
        """Update only catalog placement and keep the saved run revision valid."""
        path = self._scenario_path(request.id)
        try:
            record = self._record(path)
        except FileNotFoundError as error:
            raise MockTestError("scenario_not_found", "Mock scenario does not exist.") from error
        if record.placement_revision != request.expected_placement_revision:
            raise MockTestError("placement_conflict", "Request placement changed. Refresh and retry.")
        self._require_group(request.group_id)
        if record.group_id != request.group_id:
            record = record.model_copy(update={"group_id": request.group_id,
                                               "placement_revision": record.placement_revision + 1})
            write_state(path, encode_scenario_record(record))
        return {"id": request.id, "group_id": record.group_id, "placement_revision": record.placement_revision}

    def _record(self, path: Path) -> ScenarioRecord:
        """Load a bounded exact revision or report invalid profile state."""
        data = _read(path, MAX_SCENARIO_RECORD_BYTES)
        try:
            return ScenarioRecord.model_validate_json(data)
        except ValidationError as error:
            raise MockTestError("state_invalid", "Stored mock scenario is invalid.") from error

    def scenario(self, identity: str) -> dict:
        """Return the current scenario document for the operator to inspect or edit."""
        try:
            record = self._record(self._scenario_path(identity))
        except FileNotFoundError as error:
            raise MockTestError("scenario_not_found", "Mock scenario does not exist.") from error
        return record.model_dump()

    def seed(self, tool_name: str) -> dict | None:
        """Prepare a draft from current catalog schemas without calling the backend."""
        route = self.router.routes.get(tool_name)
        if route is None:
            return None
        tool = route.tool
        identity = tool_name.replace(".", "-")
        if len(identity) > 32:
            digest = sha256(tool_name.encode("utf-8")).hexdigest()[:8]
            identity = f"{identity[:23]}-{digest}"
        draft = Scenario(id=identity, title=(tool.title or tool_name)[:100], tool=tool_name,
                         input_schema=tool.inputSchema, output_schema=tool.outputSchema,
                         checks=[{"op": "error_code", "value": None}])
        return draft.model_dump()

    async def list(self, query: ListScenarios | None = None) -> dict:
        """List stable identities and revisions without including fixture payloads."""
        group_records, records = self.catalog_records()
        scenarios = []
        for record in records.values():
            source = record.scenario
            backend = source.tool.partition(".")[0]
            if query and "group_id" in query.model_fields_set and query.group_id != record.group_id:
                continue
            if query and query.backend is not None and query.backend != backend:
                continue
            scenarios.append({"id": source.id, "title": source.title, "tool": source.tool,
                              "revision": record.revision, "group_id": record.group_id,
                              "placement_revision": record.placement_revision, "backend": backend,
                              "fixture_count": len(source.fixtures)})
        scenarios.sort(key=lambda item: item["id"])
        groups = [group.model_dump() for group in group_records.values()]
        groups.sort(key=lambda item: (item["title"].casefold(), item["id"]))
        return {"scenarios": scenarios, "groups": groups}

    async def validate(self, request: ValidateScenario) -> dict:
        """Explain invalid drafts without storing them or starting a worker."""
        issues = self._validation_issues(request.scenario)
        return {"valid": not issues, "issues": issues}

    async def template(self, _request: ScenarioTemplate) -> dict:
        """Return an example and guide callers through the mock commands."""
        return {
            "scenario": Scenario.model_validate(EXAMPLE_SCENARIO).model_dump(exclude_unset=True),
            "workflow": [
                "Change the id, title and tool to suit your test. Set input_schema for your arguments.",
                "Use the arguments in the fixture match. Set the synthetic response and corresponding checks.",
                "Call gateway_test_validate with scenario. After valid is true, call gateway_test_save with scenario, "
                "expected_revision 0 and optional group_id. Use gateway_test_group_save to create a collection.",
                "Call gateway_test_run with id and the saved revision as expected_revision. "
                "Call gateway_test_run_get with run_id to read the report.",
            ],
        }

    def _validation_issues(self, scenario: Scenario) -> list[dict[str, str]]:
        """Skip matching raw arguments when an effective hook can transform them."""
        live = self.router.modules
        state = live.state if live is not None else ModuleState()
        before, _ = _effective_hook(scenario, "before", state)
        return validation_issues(scenario, check_ambiguity=before is None)

    def require_valid(self, scenario: Scenario) -> None:
        """Apply the profile's executable scenario rules before persisting a definition."""
        issues = self._validation_issues(scenario)
        if issues:
            raise MockTestError(issues[0]["code"], issues[0]["message"], issues=issues)

    async def save(self, request: SaveScenario) -> dict:
        """Atomically save a definition after checking its observed revision."""
        self.require_valid(request.scenario)
        path = self._scenario_path(request.scenario.id)
        try:
            current = self._record(path)
        except FileNotFoundError:
            current = None
        revision = current.revision if current else 0
        if revision != request.expected_revision:
            raise MockTestError("revision_conflict", "Scenario revision changed. Refresh and retry.")
        if revision == 0 and sum(1 for _ in self.scenarios.glob("*.json")) >= MAX_REQUESTS:
            raise MockTestError("scenario_capacity", "The test profile has reached its scenario limit.")
        group_id = current.group_id if current else request.group_id
        if current and "group_id" in request.model_fields_set and request.group_id != group_id:
            raise MockTestError("use_move", "Use gateway_test_move to change an existing request's collection.")
        if current is None:
            self._require_group(group_id)
        record = ScenarioRecord(scenario=request.scenario, revision=revision + 1, group_id=group_id,
                                placement_revision=current.placement_revision if current else 0)
        write_state(path, encode_scenario_record(record))
        return record.model_dump()

    async def run(self, request: RunScenario) -> dict:
        """Execute the observed revision and return its retained report identifier and outcome."""
        try:
            record = self._record(self._scenario_path(request.id))
        except FileNotFoundError as error:
            raise MockTestError("scenario_not_found", "Mock scenario does not exist.") from error
        if record.revision != request.expected_revision:
            raise MockTestError("revision_conflict", "Scenario revision changed. Refresh and retry.")
        if self.active >= _MAX_ACTIVE:
            raise MockTestError("run_busy", "The test profile has reached its concurrent run limit.")
        self.active += 1
        try:
            report = await self._execute(record)
        finally:
            self.active -= 1
        self._store_report(report)
        keys = ("run_id", "scenario_id", "revision", "profile", "duration_ms", "passed", "error_code",
                "fixture_id", "upstream_calls", "truncated")
        return {key: report[key] for key in keys}

    async def get_run(self, request: GetRun) -> dict:
        """Return a report for this profile while its retention window is open."""
        path = self.runs / f"{request.run_id}.json"
        try:
            if path.stat().st_mtime > time() - _REPORT_TTL_SECONDS:
                return _read_dict(path, MAX_REPORT_BYTES)
        except FileNotFoundError:
            pass
        raise MockTestError("run_not_found", "Mock test run does not exist or has expired.")

    def _store_report(self, report: dict) -> None:
        """Persist the new report atomically and trim older results around it."""
        path = self.runs / f'{report["run_id"]}.json'
        write_state(path, bounded_json(report, MAX_REPORT_BYTES, node_limit=80_000))
        self.prune_reports(newest_run_id=report["run_id"])

    def prune_reports(self, *, newest_run_id: str | None = None) -> None:
        """Expire old reports and journal entries, optionally preserving a just-written run."""
        cutoff = time() - _REPORT_TTL_SECONDS
        retained = []
        for other in self.runs.glob("*.json"):
            if other.stem == newest_run_id:
                continue
            try:
                updated = other.stat().st_mtime
            except FileNotFoundError:
                continue
            if updated <= cutoff:
                other.unlink(missing_ok=True)
            else:
                retained.append((updated, other))
        retained.sort(reverse=True)
        limit = _MAX_REPORTS if newest_run_id is None else _MAX_REPORTS - 1
        for _, other in retained[limit:]:
            other.unlink(missing_ok=True)
        self.journal.prune_expired(1)

    def _isolated_router(self, scenario: Scenario, directory: Path, trace: dict) -> Router:
        """Create a router whose only upstream is the fixed local mock worker."""
        namespace = scenario.tool.partition(".")[0]
        profile = self.router.profile
        denied = scenario.denied or scenario.tool in profile.denied
        command, *arguments = self.command
        isolated = Profile.model_validate({
            "name": profile.name, "environment": "test", "timeout_seconds": _WORKER_STARTUP_SECONDS,
            "backends": {namespace: {"transport": "stdio", "command": command,
                                     "args": [*arguments, "mock-test-worker", str(directory / "scenario.json")]}},
            "denied": [scenario.tool] if denied else [],
        })
        router = Router(isolated, Telemetry(self.journal))
        self._install_hooks(router, scenario, directory, trace)
        return router

    def _install_hooks(self, router: Router, scenario: Scenario, directory: Path, trace: dict) -> None:
        """Copy effective local hooks once and keep their definitions private to the run."""
        live = self.router.modules
        state = live.state if live is not None else ModuleState()
        effective = {}
        modules = {}
        for phase in ("before", "after"):
            module, source = _effective_hook(scenario, phase, state)
            if module is not None:
                if not module.local:
                    raise MockTestError("non_local_hook", "Scenario hooks must contain only local operations.")
                key = f"mock-{phase}"
                modules[key] = module.model_copy(update={"id": key}, deep=True)
                effective[phase] = {"id": module.id, "source": source}
        trace["effective_hooks"] = effective
        if live is not None:
            trace["module_revision"] = state.revision
        if modules:
            registry = ModuleRegistry(router, directory / "modules.json")
            keys = Hooks(before="mock-before" if "before" in effective else None,
                         after="mock-after" if "after" in effective else None)
            registry.state = ModuleState(modules=modules, bindings={scenario.tool: keys})
            router.modules = registry

    async def _execute(self, record: ScenarioRecord) -> dict:
        """Dispatch through the standard router and reconcile the mock-side match receipt."""
        scenario = record.scenario
        run_id = uuid4().hex
        started = monotonic()
        trace = {"trace_id": run_id, "before_hook": "skipped", "arguments_changed": False,
                 "after_hook": "skipped", "output_schema": "skipped", "dispatched": False}
        with TemporaryDirectory(prefix=".mock-run-", dir=self.directory) as name:
            directory = Path(name)
            worker_fields = {"id", "title", "tool", "input_schema", "output_schema", "fixtures"}
            payload = bounded_json(scenario.model_dump(include=worker_fields), MAX_SCENARIO_BYTES)
            write_state(directory / "scenario.json", payload)
            router = self._isolated_router(scenario, directory, trace)
            async with router.running():
                if scenario.tool not in router.routes:
                    result = tool_error("The local mock worker could not start")
                    trace["error_code"] = "mock_worker_unavailable"
                else:
                    # Keep startup discovery independent of the fixture's short call deadline.
                    router.profile.timeout_seconds = _WORKER_CALL_SECONDS
                    metadata = {"traceparent": f"00-{run_id}-{token_hex(8)}-00"}
                    result = await router.call(scenario.tool, scenario.arguments, metadata=metadata, mock_trace=trace)
            upstream_calls, fixture_id, match_error, rules = _match_trace(directory / "receipt.json", scenario)
        error_code = trace.get("error_code")
        if match_error and error_code == "upstream_tool_error":
            error_code = match_error
        if not upstream_calls and trace["dispatched"] and not result.isError:
            result = tool_error("The mock worker did not record its fixture selection")
            error_code = "mock_receipt_missing"
        trace.update(error_code=error_code, match_error=match_error, fixture_rules=rules)
        sections = report_data(scenario, result, error_code)
        return {"run_id": run_id, "scenario_id": scenario.id, "revision": record.revision,
                "profile": self.router.profile.endpoint_key, "duration_ms": round((monotonic() - started) * 1000, 2),
                "error_code": error_code, "fixture_id": fixture_id, "upstream_calls": upstream_calls, "trace": trace,
                **sections}
