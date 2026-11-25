"""Define bounded mock scenarios, fixture rules and test commands."""

import re
from typing import Annotated, Literal

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StringConstraints

from .module_data import ModuleError, bounded_json, pointer, same_scalar
from .module_registry import Module
from .profile import Identifier, ToolName

MAX_SCENARIO_BYTES = 65_536
MAX_SCENARIO_RECORD_BYTES = MAX_SCENARIO_BYTES + 512
MAX_COLLECTIONS = 64
MAX_REQUESTS = 64
POINTER = Annotated[str, StringConstraints(max_length=512, pattern=r"^(?:/(?:[^~]|~[01])*)?$")]
JSON_OBJECT = dict[str, JsonValue]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
_TOOL = re.compile(r"[A-Za-z0-9_-]{1,32}\.[A-Za-z0-9_.-]+")


class MockTestError(ValueError):
    """Identify a mock test failure without echoing caller-controlled data."""

    def __init__(self, code: str, message: str):
        """Keep the machine reason alongside the human-readable explanation."""
        super().__init__(message)
        self.code = code


class Contract(BaseModel):
    """Reject unknown fields and keep validation errors free of supplied inputs."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class ExactMatch(Contract):
    """Match all post-hook tool arguments as a JSON object."""

    op: Literal["exact"]
    value: JSON_OBJECT


class PointerEquals(Contract):
    """Match a value at one JSON Pointer, including explicit null."""

    op: Literal["pointer_equals"]
    path: POINTER
    value: JsonValue


class Fixture(Contract):
    """Choose an inert result, an MCP tool error or an intentional deadline."""

    id: Identifier
    match: Annotated[ExactMatch | PointerEquals, Field(discriminator="op")]
    outcome: Literal["result", "tool_error", "timeout"]
    structured_content: JSON_OBJECT | None = None
    text: str | None = Field(default=None, max_length=8192)


class EqualsCheck(Contract):
    """Compare a JSON Pointer in the client-visible structured result."""

    op: Literal["equals"]
    path: POINTER
    value: JsonValue


class PresenceCheck(Contract):
    """Distinguish a missing path from a present JSON null."""

    op: Literal["exists", "absent"]
    path: POINTER


class SchemaCheck(Contract):
    """Check the client-visible structured result against the scenario output schema."""

    op: Literal["schema"]


class ErrorCheck(Contract):
    """Expect a gateway reason code, with null denoting success."""

    op: Literal["error_code"]
    value: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None


Check = Annotated[EqualsCheck | PresenceCheck | SchemaCheck | ErrorCheck, Field(discriminator="op")]


class Scenario(Contract):
    """Capture one synthetic tool descriptor, call, fixtures and result expectations."""

    id: Identifier
    title: Title
    tool: ToolName
    input_schema: JSON_OBJECT
    output_schema: JSON_OBJECT | None = None
    arguments: JSON_OBJECT = Field(default_factory=dict)
    fixtures: list[Fixture] = Field(default_factory=list, max_length=16)
    checks: list[Check] = Field(default_factory=list, max_length=24)
    before: Module | None = None
    after: Module | None = None
    denied: bool = Field(default=False, strict=True)


class ListScenarios(Contract):
    """List scenarios in the current authenticated test profile."""

    group_id: Identifier | None = None
    backend: Identifier | None = None


class ValidateScenario(Contract):
    """Check the supplied draft without editing saved scenarios."""

    scenario: Scenario


class SaveScenario(ValidateScenario):
    """Save a validated scenario against its current revision, with zero for creation."""

    expected_revision: int = Field(ge=0, strict=True)
    group_id: Identifier | None = None


class GroupMetadata(Contract):
    """Identify a collection by a stable ID and a mutable display name."""

    id: Identifier
    title: Title


class SaveGroup(GroupMetadata):
    """Create or rename a collection at the observed revision."""

    expected_revision: int = Field(ge=0, strict=True)


class GroupRecord(GroupMetadata):
    """Persist one profile-local collection."""

    revision: int = Field(ge=1, strict=True)


class MoveScenario(Contract):
    """Move a request independently of revisions to its executable definition."""

    id: Identifier
    group_id: Identifier | None
    expected_placement_revision: int = Field(ge=0, strict=True)


class RunScenario(Contract):
    """Execute the exact saved scenario revision requested by the caller."""

    id: Identifier
    expected_revision: int = Field(ge=1, strict=True)


class GetRun(Contract):
    """Retrieve one retained report using its opaque profile-scoped identifier."""

    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ScenarioRecord(ValidateScenario):
    """Store an immutable definition with its revision in an atomic document."""

    revision: int = Field(ge=1, strict=True)
    group_id: Identifier | None = None
    placement_revision: int = Field(default=0, ge=0, strict=True)


class WorkerReceipt(Contract):
    """Accept a call count and match bits, or null when the arguments were invalid."""

    upstream_calls: int = Field(ge=1, strict=True)
    matches: Annotated[list[StrictBool], Field(max_length=16)] | None


def json_equal(left: JsonValue, right: JsonValue) -> bool:
    """Compare JSON values, keeping boolean types and array order significant."""
    pending = [(left, right)]
    while pending:
        actual, expected = pending.pop()
        if isinstance(actual, dict) and isinstance(expected, dict):
            if actual.keys() != expected.keys():
                return False
            pending.extend((actual[key], expected[key]) for key in actual)
        elif isinstance(actual, list) and isinstance(expected, list):
            if len(actual) != len(expected):
                return False
            pending.extend(zip(actual, expected))
        elif not same_scalar(actual, expected):
            return False
    return True


def fixture_matches(fixture: Fixture, arguments: JSON_OBJECT) -> bool:
    """Return whether every part of the fixture rule matches the received arguments."""
    rule = fixture.match
    if isinstance(rule, ExactMatch):
        return json_equal(arguments, rule.value)
    try:
        actual = pointer(arguments, rule.path)
    except ModuleError:
        return False
    return json_equal(actual, rule.value)


def select_fixture(fixtures: list[Fixture], matches: list[bool]) -> tuple[Fixture | None, str | None]:
    """Return the unique matching fixture or the corresponding match error code."""
    found = [fixture for fixture, matched in zip(fixtures, matches, strict=True) if matched]
    if len(found) == 1:
        return found[0], None
    return None, "mock_ambiguous_match" if found else "mock_no_match"


def encode_scenario_record(record: ScenarioRecord) -> bytes:
    """Bound both the worker input and the exact stored revision envelope."""
    document = record.model_dump()
    try:
        bounded_json(document["scenario"], MAX_SCENARIO_BYTES)
        return bounded_json(document, MAX_SCENARIO_RECORD_BYTES, node_limit=12_004)
    except ModuleError as error:
        raise MockTestError("scenario_limit", "Scenario exceeds the JSON size or depth limit.") from error


def validation_issues(scenario: Scenario, *, check_ambiguity: bool = True) -> list[dict[str, str]]:
    """Explain invalid schemas, unsafe hooks and ambiguous authored fixture rules."""
    issues = []
    try:
        encode_scenario_record(ScenarioRecord(scenario=scenario, revision=1))
    except MockTestError as error:
        return [{"code": error.code, "message": str(error)}]
    if _TOOL.fullmatch(scenario.tool) is None:
        issues.append({"code": "invalid_tool", "message": "Use a backend-qualified tool name, for example demo.find."})
    for name, schema in (("input_schema", scenario.input_schema), ("output_schema", scenario.output_schema)):
        if schema is None:
            continue
        try:
            if "$schema" in schema and not isinstance(schema["$schema"], str):
                raise ValueError("Invalid schema dialect")
            validator_for(schema).check_schema(schema)
        except (SchemaError, ValueError, RecursionError):
            issues.append({"code": name, "message": f"Invalid {name.replace('_', ' ')}."})
    if any(module is not None and not module.local for module in (scenario.before, scenario.after)):
        issues.append({"code": "non_local_hook", "message": "Scenario hooks must contain only local operations."})
    if len({fixture.id for fixture in scenario.fixtures}) != len(scenario.fixtures):
        issues.append({"code": "duplicate_fixture", "message": "Fixture identifiers must be unique."})
    expected_codes = {check.value for check in scenario.checks if isinstance(check, ErrorCheck)}
    if not scenario.fixtures and "mock_no_match" not in expected_codes and not scenario.denied:
        issues.append({"code": "fixture_required", "message": "Add a fixture or explicitly expect no match."})
    if check_ambiguity and scenario.before is None and "mock_ambiguous_match" not in expected_codes:
        matches = sum(fixture_matches(fixture, scenario.arguments) for fixture in scenario.fixtures)
        if matches > 1:
            issues.append({"code": "mock_ambiguous_match", "message": "Multiple fixtures match the scenario arguments."})
    if not scenario.checks:
        issues.append({"code": "check_required", "message": "Add at least one assertion."})
    if scenario.output_schema is None and any(isinstance(check, SchemaCheck) for check in scenario.checks):
        issues.append({"code": "schema_required", "message": "A schema check requires an output schema."})
    return issues
