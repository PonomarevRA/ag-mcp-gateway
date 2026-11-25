"""Define bounded mock scenarios, fixture rules and test commands."""

import re
from typing import Annotated, Literal

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StringConstraints, ValidationError

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

EXAMPLE_SCENARIO = {
    "id": "example-echo",
    "title": "Echo example",
    "tool": "demo.echo",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    "arguments": {"text": "hello"},
    "fixtures": [{
        "id": "echo-result", "match": {"op": "exact", "value": {"text": "hello"}},
        "outcome": "result", "structured_content": {"echo": "hello"},
    }],
    "checks": [{"op": "equals", "path": "/echo", "value": "hello"}],
}


class MockTestError(ValueError):
    """Identify a mock test failure without echoing caller-controlled data."""

    def __init__(self, code: str, message: str, *, issues: list[dict[str, str]] | None = None):
        """Keep the machine reason alongside the human-readable explanation."""
        super().__init__(message)
        self.code = code
        self.issues = issues


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
    path: POINTER = Field(description="JSON Pointer into the call arguments, for example /text.")
    value: JsonValue


class Fixture(Contract):
    """Choose an inert result, an MCP tool error or an intentional deadline."""

    id: Identifier
    match: Annotated[ExactMatch | PointerEquals, Field(discriminator="op")] = Field(
        description='Use {"op":"exact","value":{...}} to match the call arguments.')
    outcome: Literal["result", "tool_error", "timeout"] = Field(description="Use result for a successful fixture.")
    structured_content: JSON_OBJECT | None = Field(default=None, description="Synthetic JSON response object.")
    text: str | None = Field(default=None, max_length=8192)


class EqualsCheck(Contract):
    """Compare a JSON Pointer in the client-visible structured result."""

    op: Literal["equals"]
    path: POINTER = Field(description="JSON Pointer into the response, for example /items/0/id.")
    value: JsonValue


class PresenceCheck(Contract):
    """Distinguish a missing path from a present JSON null."""

    op: Literal["exists", "absent"]
    path: POINTER = Field(description="JSON Pointer into the response, for example /items/0/id.")


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
    tool: ToolName = Field(description="Backend-qualified MCP tool name, for example demo.echo.")
    input_schema: JSON_OBJECT = Field(description='JSON Schema for arguments, for example {"type":"object"}.')
    output_schema: JSON_OBJECT | None = Field(default=None, description='Required for an {"op":"schema"} check.')
    arguments: JSON_OBJECT = Field(default_factory=dict, description="Call arguments sent to the synthetic tool.")
    fixtures: list[Fixture] = Field(default_factory=list, max_length=16, description="Synthetic response variants.")
    checks: list[Check] = Field(default_factory=list, max_length=24, description="Response and error assertions.")
    before: Module | None = None
    after: Module | None = None
    denied: bool = Field(default=False, strict=True)


class ListScenarios(Contract):
    """List scenarios in the current authenticated test profile."""

    group_id: Identifier | None = None
    backend: Identifier | None = None


class ScenarioTemplate(Contract):
    """Request a runnable example without writing to the mock catalog."""


class ValidateScenario(Contract):
    """Check the supplied draft without editing saved scenarios."""

    scenario: Scenario = Field(description="Complete mock scenario object. See gateway_test_template.",
                               examples=[EXAMPLE_SCENARIO])


class SaveScenario(ValidateScenario):
    """Save a validated scenario against its current revision, with zero for creation."""

    expected_revision: int = Field(ge=0, strict=True, description="Use 0 to create, or the current revision to update.")
    group_id: Identifier | None = Field(default=None, description="Optional collection ID when creating a scenario.")


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
        return [{"code": error.code, "path": "/scenario", "message": str(error)}]
    if _TOOL.fullmatch(scenario.tool) is None:
        issues.append({"code": "invalid_tool", "path": "/scenario/tool",
                       "message": "Use a backend-qualified tool name, for example demo.echo."})
    for name, schema in (("input_schema", scenario.input_schema), ("output_schema", scenario.output_schema)):
        if schema is None:
            continue
        try:
            if "$schema" in schema and not isinstance(schema["$schema"], str):
                raise ValueError("Invalid schema dialect")
            validator_for(schema).check_schema(schema)
        except (SchemaError, ValueError, RecursionError):
            issues.append({"code": name, "path": f"/scenario/{name}",
                           "message": f"Invalid {name.replace('_', ' ')}.",
                           "hint": 'Use a valid JSON Schema such as {"type":"object"}.'})
    for name, module in (("before", scenario.before), ("after", scenario.after)):
        if module is not None and not module.local:
            issues.append({"code": "non_local_hook", "path": f"/scenario/{name}",
                           "message": "Scenario hooks must contain only local operations."})
    if len({fixture.id for fixture in scenario.fixtures}) != len(scenario.fixtures):
        issues.append({"code": "duplicate_fixture", "path": "/scenario/fixtures",
                       "message": "Fixture identifiers must be unique."})
    expected_codes = {check.value for check in scenario.checks if isinstance(check, ErrorCheck)}
    if not scenario.fixtures and "mock_no_match" not in expected_codes and not scenario.denied:
        issues.append({"code": "fixture_required", "path": "/scenario/fixtures",
                       "message": "Add a fixture or explicitly expect no match.",
                       "hint": 'For an intentional miss use {"op":"error_code","value":"mock_no_match"} in checks.'})
    if check_ambiguity and scenario.before is None and "mock_ambiguous_match" not in expected_codes:
        matches = sum(fixture_matches(fixture, scenario.arguments) for fixture in scenario.fixtures)
        if matches > 1:
            issues.append({"code": "mock_ambiguous_match", "path": "/scenario/fixtures",
                           "message": "Multiple fixtures match the scenario arguments.",
                           "hint": "Adjust the match rules to select one fixture."})
    if not scenario.checks:
        issues.append({"code": "check_required", "path": "/scenario/checks", "message": "Add at least one assertion.",
                       "hint": 'For equality use {"op":"equals","path":"/field","value":...}.'})
    if scenario.output_schema is None and any(isinstance(check, SchemaCheck) for check in scenario.checks):
        issues.append({"code": "schema_required", "path": "/scenario/output_schema",
                       "message": "A schema check requires an output schema."})
    return issues


_MAX_ISSUES = 8
_FIELDS = frozenset({
    "scenario", "id", "title", "tool", "input_schema", "output_schema", "arguments",
    "fixtures", "checks", "before", "after", "denied", "match", "outcome",
    "structured_content", "text", "op", "path", "value", "expected_revision",
    "group_id", "expected_placement_revision", "backend", "run_id", "steps",
    "save_as", "source", "fields", "field", "left", "right", "pattern", "method",
    "item", "expression", "variables", "response", "assertions",
})
# Pydantic adds discriminators to locations, although they do not exist in the input JSON.
_UNION_TAGS = frozenset({
    "exact", "pointer_equals", "equals", "exists", "absent", "schema", "error_code",
    "select", "object", "find", "compare", "contains", "filter", "aggregate", "math", "call",
})
_MESSAGES = {
    "missing": "Required field is missing.",
    "extra_forbidden": "This field is not supported.",
    "dict_type": "Expected a JSON object.",
    "list_type": "Expected an array.",
    "string_type": "Expected a string.",
    "bool_type": "Expected a boolean.",
    "int_type": "Expected an integer.",
    "greater_than_equal": "Integer is below the allowed minimum.",
    "less_than_equal": "Integer is above the allowed maximum.",
    "string_too_short": "String is too short.",
    "string_too_long": "String is too long.",
    "string_pattern_mismatch": "Value does not match the expected format.",
    "literal_error": "Choose a supported value.",
    "union_tag_not_found": "Required discriminator op is missing.",
    "union_tag_invalid": "Choose a supported op.",
    "too_short": "Array has too few entries.",
    "too_long": "Array has too many entries.",
}
_HINTS = {
    "input_schema": 'Provide a JSON Schema object, for example {"type":"object"}.',
    "output_schema": 'Provide a JSON Schema object when using a schema check.',
    "fixtures": 'Use id, match, outcome and structured_content for each synthetic response.',
    "match": 'For exact arguments, use {"op":"exact","value":{...}}.',
    "outcome": 'Use "result", "tool_error" or "timeout".',
    "structured_content": "Put the synthetic response fields here as a JSON object.",
    "response": 'Use "structured_content" for the fixture response object.',
    "assertions": 'Use "checks" with op, path and value.',
    "checks": 'For an equality check, use {"op":"equals","path":"/field","value":...}.',
    "path": "Use a JSON Pointer such as /items/0/id.",
    "expected_revision": "Use the revision from save or list. To create an entry, pass 0 to its save command.",
    "expected_placement_revision": "Use the request's placement_revision from gateway_test_list.",
    "id": "Use 1 to 32 ASCII letters, digits, hyphens or underscores.",
    "group_id": "Use an existing collection ID from gateway_test_list.",
    "run_id": "Use the run_id returned by gateway_test_run.",
    "tool": "Use a backend-qualified name such as demo.echo.",
    "scenario": "Use gateway_test_template to get a complete example.",
}


def _location(parts: tuple) -> list[str]:
    """Keep known field names and array indexes, and mask arbitrary keys."""
    path = []
    for part in parts:
        if isinstance(part, int):
            path.append(str(part))
        elif part in _FIELDS:
            path.append(part)
        elif part not in _UNION_TAGS:
            path.append("*")
            break
    return path


def argument_issues(error: ValidationError) -> list[dict[str, str]]:
    """Return useful field paths without copying input values or validator text."""
    issues = []
    for item in error.errors(include_input=False, include_context=False, include_url=False)[:_MAX_ISSUES]:
        parts = _location(item["loc"])
        code = item["type"]
        issue = {"code": code, "path": "/" + "/".join(parts),
                 "message": _MESSAGES.get(code, "Check the field against the input schema.")}
        hint = next((_HINTS[part] for part in reversed(parts) if part in _HINTS), None)
        if hint:
            issue["hint"] = hint
        issues.append(issue)
    return issues


def describe_issues(heading: str, issues: list[dict[str, str]]) -> str:
    """Render the same diagnostics for MCP clients that consume only text content."""
    lines = [heading]
    for issue in issues:
        line = f"{issue['path']}: {issue['message']}"
        if hint := issue.get("hint"):
            line += " " + hint
        lines.append(line)
    return "\n".join(lines)
