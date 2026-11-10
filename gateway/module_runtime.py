"""Run approved data modules and adapt their values to MCP calls."""

import asyncio

from mcp import types

from .module_data import (ModuleError, array_required, bounded_json, contains, difference,
                          formula, number, object_required, parse_json, pointer)
from .schema_checks import SchemaChecks
from .module_registry import (ActivateHook, Aggregate, Bind, Call, Compare, Containment, Delete,
                              HookCapability, ListModules, Math, Module, Object, PinHook, Run, Save,
                              Select, SetGenerations)


class Pipeline:
    """Keep the initial input, previous value and named step outputs per invocation."""

    def __init__(self, data):
        """Validate the initial JSON before resolving any operation."""
        bounded_json(data)
        self.context = {"input": data, "value": data, "steps": {}}

    def get(self, path: str):
        """Resolve a pointer against this invocation's context."""
        snapshot = {**self.context, "steps": self.context["steps"].copy()}
        return pointer(snapshot, path)

    def record(self, step, value):
        """Bound each operation's output and retain it for subsequent steps."""
        bounded_json(value)
        self.context["value"] = value
        if step.save_as is not None:
            self.context["steps"][step.save_as] = value
        return value

    def apply(self, step):
        """Execute a local operation with no external side effects."""
        if isinstance(step, Select):
            return self.get(step.source)
        if isinstance(step, Object):
            return {key: self.get(path) for key, path in step.fields.items()}
        if isinstance(step, Compare):
            changes = difference(self.get(step.left), self.get(step.right))
            return {"equal": not changes, "changes": changes}
        if isinstance(step, Containment):
            value = self.get(step.source)
            budget = [50_000]
            if step.op == "contains":
                return contains(value, step.pattern, budget)
            return [item for item in array_required(value) if contains(item, step.pattern, budget)]
        if isinstance(step, Aggregate):
            return self.aggregate(step)
        if isinstance(step, Math):
            variables = {key: number(self.get(path)) for key, path in step.variables.items()}
            return formula(step.expression, variables)
        raise ModuleError("Automatic hooks require local operations")

    def aggregate(self, step: Aggregate):
        """Reduce an array of numeric values, or count its elements."""
        items = array_required(self.get(step.source))
        if step.method == "count":
            return len(items)
        values = [number(pointer(item, step.item)) for item in items]
        if step.method == "sum":
            return number(sum(values))
        if not values:
            raise ModuleError("Aggregate requires a nonempty array")
        if step.method == "mean":
            return number(sum(values) / len(values))
        return min(values) if step.method == "min" else max(values)


def run_local(module: Module, data):
    """Run a pure pipeline synchronously before or after an upstream call."""
    pipeline = Pipeline(data)
    for step in module.steps:
        pipeline.record(step, pipeline.apply(step))
    return pipeline.context["value"]


def check_schema(value, schema: dict | None, checks: SchemaChecks) -> None:
    """Validate a transformed JSON value without resolving remote schema references."""
    if schema is None:
        return
    try:
        checks.validate(value, schema)
    except Exception as error:
        raise ModuleError("Module output does not match the tool schema") from error


def result_data(result: types.CallToolResult, *, allow_text: bool = False):
    """Read structured MCP data or one text block containing JSON."""
    if result.structuredContent is not None:
        return result.structuredContent
    if len(result.content) == 1 and isinstance(result.content[0], types.TextContent):
        text = result.content[0].text
        try:
            return parse_json(text)
        except ModuleError:
            if allow_text:
                return text
            raise
    raise ModuleError("The tool did not return a single JSON result")


def transform_result(module: Module, result: types.CallToolResult,
                     output_schema: dict | None, checks: SchemaChecks) -> types.CallToolResult:
    """Replace the JSON payload without forwarding a duplicate of the raw response."""
    value = run_local(module, result_data(result))
    text = bounded_json(value).decode()
    structured = None
    if result.structuredContent is not None:
        structured = object_required(value)
        check_schema(structured, output_schema, checks)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                structuredContent=structured, _meta=result.meta)


class ModuleService:
    """Expose the same registry and explicit runner to operators and management MCP."""

    def __init__(self, registry, router):
        """Describe the fixed module commands and their permission surface."""
        self.registry = registry
        self.router = router
        self.commands = {
            "gateway_modules_list": (ListModules, "List data modules, their hooks and revision.", True, self.list),
            "gateway_module_save": (Save, "Save a declarative data module at the observed revision.", False, self.save),
            "gateway_module_delete": (Delete, "Delete an unbound data module.", False, self.delete),
            "gateway_module_bind": (Bind, "Bind or detach a local module before or after one MCP tool.", False, self.bind),
            "gateway_hook_session_open": (ListModules, "Open an isolated one-hour hook session. Keep its capability private.", False, self.open_session),
            "gateway_hook_session_close": (HookCapability, "Revoke a hook session using its capability.", False, self.close_session),
            "gateway_hook_activate": (ActivateHook, "Activate or detach a local hook only in the supplied session.", False, self.activate),
            "gateway_hook_pin": (PinHook, "Pin or unpin one local hook template.", False, self.pin),
            "gateway_hook_generations_set": (SetGenerations, "Change the successful-session thresholds for Gen2 and Gen1.", False, self.set_generations),
            "gateway_module_run": (Run, "Run a module with explicit JSON. Chains may call configured read-only MCP tools.", False, self.run),
        }

    async def list(self, _query, _metadata=None) -> dict:
        """Return definitions and bindings from the current revision."""
        return self.registry.catalog()

    async def save(self, change: Save, _metadata=None) -> dict:
        """Persist a module definition using the common edit revision."""
        return self.registry.save(change)

    async def delete(self, change: Delete, _metadata=None) -> dict:
        """Remove an unbound module."""
        return self.registry.delete(change)

    async def bind(self, change: Bind, _metadata=None) -> dict:
        """Persist or detach a hook binding."""
        return self.registry.bind(change)

    async def open_session(self, _query=None, _metadata=None) -> dict:
        """Create an isolated server-issued scope without persisting its capability."""
        return self.registry.sessions.open()

    async def close_session(self, request: HookCapability, _metadata=None) -> dict:
        """Revoke one hook capability without editing the profile."""
        return self.registry.sessions.close(request.capability)

    async def activate(self, request: ActivateHook, _metadata=None) -> dict:
        """Attach or detach a local template inside an existing scope."""
        return self.registry.activate(request)

    async def pin(self, request: PinHook, _metadata=None) -> dict:
        """Update explicit template retention."""
        return self.registry.pin(request)

    async def set_generations(self, request: SetGenerations, _metadata=None) -> dict:
        """Update both generation thresholds at the observed revision."""
        return self.registry.set_generations(request)

    async def run(self, request: Run, metadata=None) -> dict:
        """Run a saved definition with a shared time limit for all of its MCP steps."""
        state = self.registry.state
        if request.expected_revision != state.revision:
            raise ModuleError("Module revision changed. Refresh and retry.")
        module = state.modules.get(request.id)
        if module is None:
            raise ModuleError("Module not found")
        pipeline = Pipeline(request.data)
        try:
            async with asyncio.timeout(min(60, self.router.profile.timeout_seconds)):
                for step in module.steps:
                    if isinstance(step, Call):
                        value = await self.call(step, pipeline, metadata)
                    else:
                        value = pipeline.apply(step)
                    pipeline.record(step, value)
        except TimeoutError as error:
            raise ModuleError("Module execution timed out") from error
        return {"id": module.id, "result": pipeline.context["value"]}

    async def call(self, step: Call, pipeline: Pipeline, metadata):
        """Apply the regular gateway policies and require a current upstream read-only hint."""
        arguments = object_required(pipeline.get(step.arguments))
        result = await self.router.call(step.tool, arguments, metadata=metadata, require_read_only=True)
        if result.isError:
            raise ModuleError("A read-only MCP step failed or is unavailable")
        return result_data(result, allow_text=True)
