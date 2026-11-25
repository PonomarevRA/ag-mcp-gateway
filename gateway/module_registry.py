"""Validate and store revisioned data modules and their per-tool hooks."""

import logging
import time
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from .hook_lifecycle import (MAX_SESSION_USES, Activation, GenerationRules, HookLife, HookSession,
                             HookSessions)
from .module_data import ModuleError, bounded_json, formula_tree, json_signature
from .policy_settings import write_state
from .profile import Identifier, ToolName

Pointer = Annotated[str, StringConstraints(max_length=256, pattern=r"^(?:/(?:[^~]|~[01])*)?$")]
JsonKey = Annotated[str, StringConstraints(max_length=128)]
Phase = Literal["before", "after"]
MAX_STATE = 393_216
MAX_DEFINITIONS = 131_072
_LOG = logging.getLogger("gateway")


class Contract(BaseModel):
    """Validate module definitions and management requests without echoing input."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class Step(Contract):
    """One named result in a declarative pipeline."""

    save_as: Identifier | None = None


class Select(Step):
    op: Literal["select"]
    source: Pointer


class Object(Step):
    op: Literal["object"]
    fields: dict[JsonKey, Pointer] = Field(max_length=32)


class Find(Step):
    """Select one object by an exact string match in a previous result."""

    op: Literal["find"]
    source: Pointer
    field: Pointer
    match: Pointer


class Compare(Step):
    op: Literal["compare"]
    left: Pointer
    right: Pointer


class Containment(Step):
    op: Literal["contains", "filter"]
    source: Pointer
    pattern: JsonValue


class Aggregate(Step):
    op: Literal["aggregate"]
    source: Pointer
    method: Literal["count", "sum", "mean", "min", "max"]
    item: Pointer = ""


class Math(Step):
    op: Literal["math"]
    expression: str = Field(min_length=1, max_length=160)
    variables: dict[Identifier, Pointer] = Field(max_length=12)

    @model_validator(mode="after")
    def check_expression(self):
        """Check the grammar and variable names before a definition can run."""
        formula_tree(self.expression, self.variables)
        return self


class Call(Step):
    op: Literal["call"]
    tool: ToolName
    arguments: Pointer = "/value"


Operation = Annotated[Select | Object | Find | Compare | Containment | Aggregate | Math | Call, Field(discriminator="op")]


class Module(Contract):
    """A reusable ordered list of bounded operations with unique named results."""

    id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    steps: list[Operation] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def check_steps(self):
        """Reject overwritten results and overly long MCP call sequences."""
        names = [step.save_as for step in self.steps if step.save_as is not None]
        if len(names) != len(set(names)):
            raise ValueError("duplicate step name")
        if sum(isinstance(step, Call) for step in self.steps) > 4:
            raise ValueError("too many MCP calls")
        return self

    def step_signature(self) -> bytes:
        """Compare operational steps as JSON, independent of the title."""
        return json_signature([step.model_dump() for step in self.steps])

    @property
    def local(self) -> bool:
        """Return whether this module is eligible for automatic hooks."""
        return not any(isinstance(step, Call) for step in self.steps)


class Hooks(Contract):
    """Allow one module in each phase, composed of any number of local steps."""

    before: Identifier | None = None
    after: Identifier | None = None


class Publication(Contract):
    """Define required JSON object fields for one published tool."""

    fields: dict[Identifier, Literal["string", "boolean", "integer", "number", "object", "array"]] = Field(
        default_factory=dict, max_length=16,
    )

    @cached_property
    def input_schema(self) -> dict:
        """Generate an MCP schema with no external references or executable constraints."""
        return {"type": "object", "properties": {name: {"type": kind} for name, kind in self.fields.items()},
                "required": list(self.fields), "additionalProperties": False}


class ModuleState(Contract):
    """Capture all definitions and bindings in one atomically replaced snapshot."""

    revision: int = Field(default=0, ge=0, strict=True)
    modules: dict[Identifier, Module] = Field(default_factory=dict, max_length=64)
    publications: dict[Identifier, Publication] = Field(default_factory=dict, max_length=64)
    bindings: dict[ToolName, Hooks] = Field(default_factory=dict, max_length=256)
    lifecycle: dict[Identifier, HookLife] = Field(default_factory=dict, max_length=64)
    generation_rules: GenerationRules = Field(default_factory=GenerationRules)

    def document(self) -> dict:
        """Omit absent bindings while preserving required JSON nulls inside modules."""
        result = self.model_dump(exclude_none=True)
        result["modules"] = {key: module.model_dump() for key, module in self.modules.items()}
        return result

    @model_validator(mode="after")
    def check_references(self):
        """Require existing local modules for all stored hook references."""
        for key, module in self.modules.items():
            if key != module.id:
                raise ValueError("module identity mismatch")
        if not self.publications.keys() <= self.modules.keys():
            raise ValueError("publication requires a saved module")
        if any(key not in self.modules or not self.modules[key].local for key in self.lifecycle):
            raise ValueError("hook history requires a local module")
        for hook in self.bindings.values():
            for name in (hook.before, hook.after):
                if name is not None and (name not in self.modules or not self.modules[name].local):
                    raise ValueError("hook requires a local module")
        return self


class Change(Contract):
    """Fence an edit with the revision last observed by its author."""

    expected_revision: int = Field(ge=0, strict=True)


class Save(Change):
    module: Module


class Publish(Change):
    """Publish a saved module or remove its existing public entry."""

    id: Identifier
    publication: Publication | None


class Delete(Change):
    id: Identifier


class Bind(Change):
    tool: ToolName
    phase: Phase
    module: Identifier | None


class Run(Change):
    id: Identifier
    data: JsonValue


class ListModules(Contract):
    """List the catalog without call data."""


class SetGenerations(Change):
    """Change both survival thresholds in one revisioned update."""

    rules: GenerationRules


class PinHook(Change):
    """Protect or unprotect a local hook template from automatic expiry."""

    id: Identifier
    pinned: bool = Field(strict=True)


class HookCapability(Contract):
    """Present a short-lived server-issued capability for one profile session."""

    capability: str = Field(min_length=43, max_length=43, repr=False)


class ActivateHook(HookCapability, Change):
    """Attach an exact local hook to a session or detach the previous attachment."""

    tool: ToolName
    phase: Phase
    module: Identifier | None


@dataclass(frozen=True)
class HookUsage:
    """Hold the exact definitions applied by one in-flight call."""

    session: HookSession
    activations: tuple[Activation, ...]


class ModuleRegistry:
    """Own a private profile file and publish edits only after a durable write."""

    def __init__(self, router, path: Path):
        """Load a bounded state snapshot for a running profile."""
        self.router = router
        self.path = path
        self.sessions = HookSessions()
        self.inflight: Counter[str] = Counter()
        try:
            with path.open("rb") as source:
                data = source.read(MAX_STATE + 1)
            if len(data) > MAX_STATE:
                raise ModuleError("Module registry exceeds its byte limit")
            self.state = ModuleState.model_validate_json(data)
        except FileNotFoundError:
            self.state = ModuleState()
        history = self.state.lifecycle.copy()
        for key, module in self.state.modules.items():
            if module.local and key not in history:
                history[key] = HookLife(created_at=int(time.time()), pinned=True)
        if history != self.state.lifecycle:
            self._write(self.state.model_copy(update={"lifecycle": history}))

    def _write(self, state: ModuleState) -> None:
        """Persist a bounded snapshot before publishing it to the event loop."""
        data = bounded_json(state.document(), MAX_STATE, node_limit=32_000)
        write_state(self.path, data)
        if state.publications != self.state.publications:
            self.router.catalog_revision += 1
        self.state = state

    def _check_revision(self, expected: int):
        """Reject changes and activations prepared against an older configuration."""
        if self.state.revision != expected:
            raise ModuleError("Module revision changed. Refresh and retry.")

    def _edit(self, change: Change, **updates) -> dict:
        """Validate references and commit one configuration revision."""
        self._check_revision(change.expected_revision)
        try:
            data = {**dict(self.state), **updates, "revision": self.state.revision + 1}
            state = ModuleState.model_validate(data)
        except ValueError as error:
            raise ModuleError("Invalid module state or active hook") from error
        self._write(state)
        return {"revision": state.revision}

    def save(self, change: Save) -> dict:
        """Add or replace a definition, resetting usage after a change in steps."""
        module = change.module
        self._check_revision(change.expected_revision)
        before = self.state.modules.get(module.id)
        if module.id in self.state.publications and module != before:
            raise ModuleError("Unpublish the tool before editing its module")
        history = self.state.lifecycle.copy()
        if not module.local:
            history.pop(module.id, None)
        elif module.id not in history or (before is not None and before.step_signature() != module.step_signature()):
            prior = history.get(module.id)
            history[module.id] = HookLife(created_at=int(time.time()), definition_revision=self.state.revision + 1,
                                         pinned=bool(prior and prior.pinned))
        modules = {**self.state.modules, module.id: module}
        definitions = {key: value.model_dump() for key, value in modules.items()}
        # Reserve file capacity for bounded session histories and profile bindings.
        bounded_json(definitions, MAX_DEFINITIONS, node_limit=24_000)
        return self._edit(change, modules=modules, lifecycle=history)

    def check_publication_targets(self, module: Module) -> None:
        """Require current policy and metadata to permit every upstream read."""
        for step in module.steps:
            if isinstance(step, Call) and not self.router.allows_module_read(step.tool):
                raise ModuleError("Published modules require allowed read-only MCP tools")

    def publish(self, change: Publish) -> dict:
        """Atomically expose a validated saved module, or remove its catalog entry."""
        self._check_revision(change.expected_revision)
        module = self.state.modules.get(change.id)
        if module is None:
            raise ModuleError("Module not found")
        publications = self.state.publications.copy()
        if change.publication is None:
            publications.pop(change.id, None)
        else:
            self.check_publication_targets(module)
            publications[change.id] = change.publication
        if publications == self.state.publications:
            return {"revision": self.state.revision}
        return self._edit(change, publications=publications)

    def delete(self, change: Delete) -> dict:
        """Remove a definition only after all profile and session references end."""
        self._check_revision(change.expected_revision)
        if change.id in self.state.publications:
            raise ModuleError("Unpublish the tool before deleting its module")
        modules = self.state.modules.copy()
        if change.id in self.protected(int(time.time())) or self.inflight[change.id]:
            raise ModuleError("Detach or close active hook bindings before deleting the module")
        if modules.pop(change.id, None) is None:
            raise ModuleError("Module not found")
        history = self.state.lifecycle.copy()
        history.pop(change.id, None)
        return self._edit(change, modules=modules, lifecycle=history)

    def bind(self, change: Bind) -> dict:
        """Attach a local module to one phase or detach the previous binding."""
        current = self.state.bindings.get(change.tool)
        if change.tool not in self.router.routes and current is None:
            raise ModuleError("Tool is not available in this profile's catalog")
        if change.module is not None and change.tool not in self.router.routes:
            raise ModuleError("Tool is not available in this profile's catalog")
        hooks = (current or Hooks()).model_copy(update={change.phase: change.module})
        bindings = self.state.bindings.copy()
        if hooks.before or hooks.after:
            bindings[change.tool] = hooks
        else:
            bindings.pop(change.tool, None)
        return self._edit(change, bindings=bindings)

    def pin(self, change: PinHook) -> dict:
        """Persist an explicit operator choice about a local template's retention."""
        life = self.state.lifecycle.get(change.id)
        if life is None:
            raise ModuleError("Local hook not found")
        history = {**self.state.lifecycle, change.id: life.model_copy(update={"pinned": change.pinned})}
        return self._edit(change, lifecycle=history)

    def set_generations(self, change: SetGenerations) -> dict:
        """Persist both thresholds together so promotion is recalculated consistently."""
        return self._edit(change, generation_rules=change.rules)

    def protected(self, now: int) -> set[str]:
        """Collect modules held by current profile bindings or unexpired activations."""
        active = set(self.inflight) | self.state.publications.keys()
        for hooks in self.state.bindings.values():
            active.update(name for name in (hooks.before, hooks.after) if name is not None)
        self.sessions.reap(now)
        for session in self.sessions.sessions.values():
            active.update(activation.module for activation in session.bindings.values())
        return active

    def sweep(self, now: int | None = None):
        """Collect expired unpinned templates once they have no active binding or call."""
        now = int(time.time()) if now is None else now
        active = self.protected(now)
        expired = {key for key, life in self.state.lifecycle.items()
                   if not life.pinned and key not in active and life.deadline(self.state.generation_rules) <= now}
        if expired:
            modules = {key: value for key, value in self.state.modules.items() if key not in expired}
            history = {key: value for key, value in self.state.lifecycle.items() if key not in expired}
            self._edit(Change(expected_revision=self.state.revision), modules=modules, lifecycle=history)

    def catalog(self) -> dict:
        """Expose templates, their generation and pseudonymous session history."""
        state = self.state
        result = state.document()
        rules = state.generation_rules
        result["hooks"] = {
            key: {**life.model_dump(exclude_none=True), "generation": life.generation(rules),
                  "retention_deadline": None if life.pinned else life.deadline(rules)}
            for key, life in state.lifecycle.items()
        }
        result.pop("lifecycle")
        result["active_sessions"] = self.sessions.catalog()
        return result

    def activate(self, change: ActivateHook) -> dict:
        """Authorize a local hook for the requested session, tool and phase only."""
        self._check_revision(change.expected_revision)
        session = self.sessions.get(change.capability)
        activation = None
        if change.module is not None:
            profile_hooks = self.state.bindings.get(change.tool, Hooks())
            if getattr(profile_hooks, change.phase) is not None:
                raise ModuleError("A session hook cannot replace a profile hook")
            route = self.router.routes.get(change.tool)
            module = self.state.modules.get(change.module)
            if route is None or change.tool in self.router.profile.denied or module is None or not module.local:
                raise ModuleError("Activation requires an allowed tool and local module")
            life = self.state.lifecycle[module.id]
            now = int(time.time())
            if not life.pinned and life.deadline(self.state.generation_rules) <= now and module.id not in self.protected(now):
                raise ModuleError("Hook template expired")
            activation = Activation(module.id, life.definition_revision, route.schema_generation)
            if (activation.module, activation.definition_revision) not in session.used and len(session.used) >= MAX_SESSION_USES:
                raise ModuleError("Hook session usage limit reached")
        self.sessions.bind(session, change.tool, change.phase, activation)
        return {"session_id": session.id, "tool": change.tool, "phase": change.phase, "module": change.module}

    def for_tool(self, name: str, route, token=None) -> tuple[Module | None, Module | None, HookUsage | None]:
        """Resolve both phases from one revision and verify any requested session override."""
        state = self.state
        hooks = state.bindings.get(name, Hooks())
        before, after = state.modules.get(hooks.before), state.modules.get(hooks.after)
        if token is None:
            return before, after, None
        session = self.sessions.get(token)
        active = []
        resolved = []
        for phase, permanent in (("before", before), ("after", after)):
            activation = session.bindings.get((name, phase))
            if activation is None:
                resolved.append(permanent)
                continue
            module = state.modules.get(activation.module)
            life = state.lifecycle.get(activation.module)
            if (permanent is not None or module is None or life is None or
                    life.definition_revision != activation.definition_revision or
                    route.schema_generation != activation.schema_generation):
                raise ModuleError("Session hook changed. Detach or activate the current definition.")
            active.append(activation)
            resolved.append(module)
        if not active:
            return resolved[0], resolved[1], None
        for activation in active:
            self.inflight[activation.module] += 1
        return resolved[0], resolved[1], HookUsage(session, tuple(active))

    def succeeded(self, usage: HookUsage):
        """Count a completed scoped hook once per session without saving its data."""
        history = self.state.lifecycle.copy()
        now = int(time.time())
        seen = set()
        for activation in usage.activations:
            if len(usage.session.used) + len(seen) >= MAX_SESSION_USES:
                break
            key = (activation.module, activation.definition_revision)
            life = history.get(activation.module)
            if key in usage.session.used or life is None or life.definition_revision != activation.definition_revision:
                continue
            history[activation.module] = life.used(usage.session.id, now)
            seen.add(key)
        if seen:
            try:
                self._write(self.state.model_copy(update={"lifecycle": history}))
            except (OSError, ModuleError):
                _LOG.warning("Could not persist successful hook usage")
            else:
                usage.session.used.update(seen)

    def release(self, usage: HookUsage):
        """Drop the protection held by a completed or cancelled invocation."""
        for activation in usage.activations:
            self.inflight[activation.module] -= 1
            if self.inflight[activation.module] == 0:
                del self.inflight[activation.module]
