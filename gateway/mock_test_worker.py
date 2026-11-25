"""Serve one synthetic MCP tool and report only fixture-selection metadata."""

import asyncio
from pathlib import Path

from jsonschema.exceptions import SchemaError, ValidationError
from mcp import types
from referencing.exceptions import Unresolvable

from .mcp_server import catalog_page, make_server, serve_stdio
from .mock_test_models import MAX_SCENARIO_BYTES, Scenario, fixture_matches, select_fixture
from .module_data import bounded_json
from .policy_settings import write_state
from .router import tool_error
from .schema_checks import SchemaChecks


async def serve_mock_worker(path: Path) -> None:
    """Read one bounded local scenario and serve it over standard MCP stdio."""
    if path.name != "scenario.json" or not path.parent.name.startswith(".mock-run-"):
        raise ValueError("Invalid mock worker scenario path")
    with path.open("rb") as source:
        data = source.read(MAX_SCENARIO_BYTES + 1)
    if len(data) > MAX_SCENARIO_BYTES:
        raise ValueError("Mock worker scenario exceeds its byte limit")
    scenario = Scenario.model_validate_json(data)
    tool_name = scenario.tool.partition(".")[2]
    descriptor = types.Tool(name=tool_name, inputSchema=scenario.input_schema, outputSchema=scenario.output_schema)
    receipt_path = path.with_name("receipt.json")
    input_checks = SchemaChecks(limit=1)
    upstream_calls = 0

    def save_match_receipt(matches: list[bool] | None) -> None:
        """Store the call count and match bits without any argument or fixture values."""
        receipt = {"upstream_calls": upstream_calls, "matches": matches}
        write_state(receipt_path, bounded_json(receipt, 512))

    async def catalog(cursor: str | None) -> types.ListToolsResult:
        """Publish just this scenario descriptor under its original upstream name."""
        return catalog_page([descriptor], cursor, 0)

    async def call(params: types.CallToolRequestParams) -> types.CallToolResult:
        """Select one fixture against the dispatch arguments and return its MCP result."""
        nonlocal upstream_calls
        if params.name != tool_name:
            return tool_error("Unknown synthetic tool")
        upstream_calls += 1
        arguments = params.arguments or {}
        try:
            input_checks.validate(arguments, scenario.input_schema)
        except (SchemaError, ValidationError, Unresolvable, ValueError, RecursionError):
            save_match_receipt(None)
            return tool_error("Synthetic arguments do not match the input schema")
        matches = [fixture_matches(fixture, arguments) for fixture in scenario.fixtures]
        winner, error_code = select_fixture(scenario.fixtures, matches)
        save_match_receipt(matches)
        if winner is None:
            return tool_error("No synthetic fixture matched" if error_code == "mock_no_match"
                              else "Several synthetic fixtures matched")
        if winner.outcome == "timeout":
            await asyncio.Future()
        content = [types.TextContent(type="text", text=winner.text)] if winner.text is not None else []
        return types.CallToolResult(content=content, structuredContent=winner.structured_content,
                                    isError=winner.outcome == "tool_error")

    # Bypass only the fixture server's response validator. CatalogSession on the
    # gateway must receive the real wire result even when its schema is invalid.
    await serve_stdio(make_server(catalog, call))
