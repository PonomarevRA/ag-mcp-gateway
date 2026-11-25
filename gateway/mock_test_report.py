"""Evaluate assertions and bound the synthetic data shown in a run report."""

import json
import re

from jsonschema.exceptions import SchemaError, ValidationError
from mcp import types
from referencing.exceptions import Unresolvable

from .mock_test_models import ErrorCheck, EqualsCheck, PresenceCheck, Scenario, json_equal
from .module_data import ModuleError, bounded_json, pointer
from .schema_checks import SchemaChecks

MAX_REPORT_BYTES = 262_144
_MASKED = "[redacted]"
_CREDENTIAL_FIELDS = frozenset({
    "authorization", "proxyauthorization", "password", "passwd", "apikey", "secret", "clientsecret",
    "accesskey", "secretaccesskey", "sessionkey", "token", "accesstoken", "refreshtoken", "bearertoken",
    "authtoken", "sessiontoken", "cookie", "setcookie", "privatekey",
})
_AUTH_VALUE = re.compile(r"\b(Bearer|Basic)\s+[\w.~+/=-]+", re.IGNORECASE)


def _credential_key(key: str) -> bool:
    """Recognize common credential names independent of casing and separators."""
    return re.sub(r"[^a-z0-9]", "", key.lower()) in _CREDENTIAL_FIELDS


def _sensitive_path(path: str) -> bool:
    """Recognize a pointer beneath a credential field even when displayed as a scalar."""
    return any(_credential_key(segment.replace("~1", "/").replace("~0", "~"))
               for segment in path.split("/"))


def _redact(value):
    """Mask conventional credential values recursively inside synthetic JSON."""
    if isinstance(value, dict):
        return {key: _MASKED if _credential_key(key) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _AUTH_VALUE.sub(lambda match: f"{match[1]} {_MASKED}", value)
    return value


def _bounded_preview(value, limit: int) -> tuple[object, bool]:
    """Preserve valid JSON and mark oversized values without returning partial content."""
    try:
        bounded_json(value, limit)
    except ModuleError:
        return None, True
    return value, False


def _mask_text(text: str) -> str:
    """Mask structured credentials when a fixture or result encodes JSON as text."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return _redact(text)
    if isinstance(value, (dict, list)):
        try:
            bounded_json(value)
        except ModuleError:
            return _MASKED
        return json.dumps(_redact(value), ensure_ascii=False, separators=(",", ":"))
    return _redact(text)


def _response(result: types.CallToolResult) -> tuple[dict | None, bool]:
    """Retain the MCP result envelope, masking secrets in structured and JSON text blocks."""
    payload = result.model_dump(by_alias=True, exclude_none=True, mode="json")
    for block in payload["content"]:
        if block["type"] == "text":
            block["text"] = _mask_text(block["text"])
    return _bounded_preview(_redact(payload), 32_768)


def _scenario_snapshot(scenario: Scenario) -> tuple[dict, bool]:
    """Mask fixture and check values addressed directly through secret pointers."""
    document = scenario.model_dump()
    for fixture in document["fixtures"]:
        if fixture["text"] is not None:
            fixture["text"] = _mask_text(fixture["text"])
        match = fixture["match"]
        if match["op"] == "pointer_equals" and _sensitive_path(match["path"]):
            match["value"] = _MASKED
    for check in document["checks"]:
        if check["op"] == "equals" and _sensitive_path(check["path"]):
            check["value"] = _MASKED
    snapshot, trimmed = _bounded_preview(_redact(document), 65_536)
    if trimmed:
        snapshot = {"id": scenario.id, "title": scenario.title, "tool": scenario.tool}
    return snapshot, trimmed


def _path_check(check: EqualsCheck | PresenceCheck, structured: dict | None) -> tuple[dict, bool]:
    """Compare a path against structured data and return a masked preview of any difference."""
    try:
        actual = pointer(structured, check.path) if structured is not None else None
        present = structured is not None
    except ModuleError:
        actual, present = None, False
    row = {"op": check.op, "path": check.path}
    if isinstance(check, PresenceCheck):
        expected = check.op == "exists"
        row.update(passed=present == expected, actual=present, expected=expected)
        return row, False
    row["passed"] = present and json_equal(actual, check.value)
    if _sensitive_path(check.path):
        row.update(expected=_MASKED, actual=_MASKED if present else None, reason="redacted")
        return row, False
    row["expected"], expected_trimmed = _bounded_preview(_redact(check.value), 1536)
    row["actual"], actual_trimmed = _bounded_preview(_redact(actual), 1536)
    if expected_trimmed or actual_trimmed:
        row["reason"] = "value_limit"
    elif not present:
        row["reason"] = "path_missing"
    return row, expected_trimmed or actual_trimmed


def _schema_check(structured: dict | None, schema: dict | None) -> dict:
    """Report a schema mismatch with its field path and a safe diagnostic code."""
    row = {"op": "schema", "passed": False}
    if structured is None or schema is None:
        row["reason"] = "structured_result_required"
        return row
    try:
        SchemaChecks(limit=1).validate(structured, schema)
    except ValidationError as error:
        segments = [str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path]
        row.update(reason="schema_mismatch", path="/" + "/".join(segments) if segments else "")
    except (SchemaError, Unresolvable, ValueError, RecursionError):
        row["reason"] = "invalid_schema"
    else:
        row["passed"] = True
    return row


def report_data(scenario: Scenario, result: types.CallToolResult, error_code: str | None) -> dict:
    """Evaluate authored expectations and return bounded, sanitized report sections."""
    response, truncated = _response(result)
    snapshot, snapshot_trimmed = _scenario_snapshot(scenario)
    checks = []
    expects_error = False
    for check in scenario.checks:
        if isinstance(check, ErrorCheck):
            expects_error = True
            checks.append({"op": "error_code", "passed": check.value == error_code,
                           "expected": check.value, "actual": error_code})
        elif isinstance(check, (EqualsCheck, PresenceCheck)):
            row, trimmed = _path_check(check, result.structuredContent)
            checks.append(row)
            truncated |= trimmed
        else:
            checks.append(_schema_check(result.structuredContent, scenario.output_schema))
    if error_code is not None and not expects_error:
        checks.insert(0, {"op": "error_code", "passed": False, "expected": None, "actual": error_code})
    return {"passed": all(check["passed"] for check in checks), "checks": checks,
            "response": response, "scenario": snapshot, "truncated": truncated or snapshot_trimmed}
