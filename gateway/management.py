"""Expose bounded dashboard settings through a separate local MCP surface."""

import sqlite3
from bisect import bisect_left
from hmac import compare_digest
from html import escape
from secrets import token_urlsafe
from urllib.parse import urlencode

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .agent_diagnostics import AgentDiagnostics
from .chain_suggestions import ChainSuggestions
from .connection_diagnostics import docker_recovery
from .connections import ReauthorizationError
from .credential_ui import LIVE_SETTINGS_HTML, SECURE_HTML, read_form
from .dashboard import backend_filter, connection_status, last_call_line
from .mcp_server import catalog_page, make_server
from .mock_test_models import argument_issues, describe_issues
from .mock_tests import MockTestError
from .module_data import ModuleError
from .module_runtime import ModuleService
from .policy_settings import PolicyChange, PolicyState
from .profile import HttpBackend, Identifier
from .router import tool_error
from .skill_store import SkillStore, SkillStoreError
from .skill_tools import SkillTools
from .tool_discovery import Discovery
from .tool_feedback import FeedbackError, ToolFeedback
from .ui_locale import tr
from .ui_shell import revision_notice, shell


_POLICY_LABELS = {"allow": "Разрешён", "deny": "Запрещён", "singleflight": "Singleflight"}
_PAGE_SIZE = 100


def _settings_url(offset: int, backend: str | None, receipt: str | None = None) -> str:
    """Build a settings URL that preserves the current browser scope."""
    params = {}
    if backend:
        params["backend"] = backend
    if offset:
        params["offset"] = offset
    if receipt:
        params["saved"] = receipt
    return "/settings" + ("?" + urlencode(params) if params else "")


class InspectQuery(BaseModel):
    """Page through live and configured tool policies, optionally for one backend."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    backend: Identifier | None = None
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=100, strict=True)


class ReconnectRequest(BaseModel):
    """Select exactly one configured MCP by name."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    backend: Identifier


class Management:
    """Keep management commands separate from proxied tools and secret values."""

    def __init__(self, router, settings, origin: str, credentials: bool, skill_store: SkillStore | None = None,
                 feedback: ToolFeedback | None = None, telemetry_api=None, *, csrf: str | None = None,
                 bridge_enabled: bool = False, mock_tests=None):
        """Bind this management surface to the selected profile and loopback origin."""
        self.router = router
        self.settings = settings
        self.origin = origin
        self.credentials = credentials
        self.csrf = csrf if settings is not None else None
        self.bridge_enabled = bridge_enabled
        self.mock_tests = mock_tests
        self.default_policy = PolicyState()
        self.confirmation: tuple[str, int, str] | None = None
        self._policy_key: tuple[int, int] | None = None
        self._policy_names: tuple[str, ...] = ()
        self.skills = SkillTools(skill_store, router) if skill_store is not None else None
        self.discovery = Discovery(router, feedback, self.skills) if feedback is not None else None
        self.diagnostics = AgentDiagnostics(telemetry_api) if telemetry_api is not None else None
        self.modules = ModuleService(router.modules, router) if router.modules is not None else None
        self.chains = ChainSuggestions(router) if self.modules is not None and router.profile.chain_suggestions else None

    @property
    def policy_state(self) -> PolicyState:
        """Expose the active policy state even without a writable settings store."""
        return self.settings.state if self.settings is not None else self.default_policy

    def browser_revision(self) -> str:
        """Identify the exact policy and tool catalog shown by the browser."""
        return f"{self.policy_state.revision}:{self.router.catalog_revision}"

    def policy_names(self) -> tuple[str, ...]:
        """Reuse the ordered policy union until route or override membership changes."""
        state = self.policy_state
        key = self.router.catalog_revision, state.revision
        if key != self._policy_key:
            profile = self.router.profile
            names = set(self.router.routes) | profile.denied | profile.singleflight
            names.update(tool for tool in state.overrides if tool.partition(".")[0] in profile.backends)
            self._policy_names = tuple(sorted(names))
            self._policy_key = key
        return self._policy_names

    def inspect(self, query: InspectQuery) -> dict:
        """Expose current policy revision, connection state and browser destinations."""
        profile = self.router.profile
        if query.backend is not None and query.backend not in profile.backends:
            raise ValueError("backend is not in this profile")
        views = {"catalog": f"{self.origin}/", "settings": f"{self.origin}/settings"}
        if self.modules is not None:
            views["modules"] = f"{self.origin}/modules"
        if self.mock_tests is not None:
            views["tests"] = f"{self.origin}/tests"
        if self.credentials:
            views["credentials"] = f"{self.origin}/credentials"
        names = self.policy_names()
        start, stop = 0, len(names)
        if query.backend is not None:
            # A slash sorts immediately after the period that delimits the namespace.
            start = bisect_left(names, query.backend + ".")
            stop = bisect_left(names, query.backend + "/")
        total = stop - start
        end = query.offset + query.limit
        tools = []
        for name in names[start + query.offset:min(start + end, stop)]:
            policy = "allow"
            if name in profile.denied:
                policy = "deny"
            elif name in profile.singleflight:
                policy = "singleflight"
            route = self.router.routes.get(name)
            effective = policy if route is not None else None
            if route is not None and policy == "singleflight" and not self.router.singleflight_active(name, route):
                effective = "allow"
            tools.append({"tool": name, "policy": policy, "effective_policy": effective,
                          "discovered": route is not None})
        connections = []
        for name, config in profile.backends.items():
            if query.backend is not None and name != query.backend:
                continue
            source = "none"
            if isinstance(config, HttpBackend):
                if config.basic_keychain:
                    source = "os"
                elif config.bearer_token_keychain:
                    source = "os_then_env" if config.bearer_token_env else "os"
                elif config.bearer_token_env:
                    source = "env"
            entry = {
                "backend": name,
                "status": self.router.statuses[name],
                "last_call_at": self.router.telemetry.last_upstream_at(name),
                "secret_preference": source,
            }
            if self.credentials and name in self.router.connections.accounts:
                entry["credentials_url"] = f"{views['credentials']}?backend={name}"
            recovery = docker_recovery(name, entry["status"])
            if recovery is not None:
                entry["recovery"] = recovery
            connections.append(entry)
        return {
            "profile": profile.endpoint_key,
            "revision": self.policy_state.revision,
            "views": views,
            "connections": connections,
            "tools": tools,
            "total_tools": total,
            "next_offset": end if end < total else None,
        }

    def server(self):
        """Create a tool-only MCP service with strict command input schemas."""
        schemas = {
            "gateway_inspect": (InspectQuery, "Inspect gateway views, connections and paged tool policies."),
            "gateway_set_tool_policy": (PolicyChange, "Set one tool to allow, deny or singleflight using the current revision."),
            "gateway_reconnect_basic": (ReconnectRequest, "Verify and reconnect one MCP with its stored Basic credential."),
            "gateway_reconnect_mcp": (ReconnectRequest, "Reconnect one configured MCP and refresh its tool catalog."),
        }
        skill_commands = self.skills.commands if self.skills is not None else {}
        discovery_commands = self.discovery.commands if self.discovery is not None else {}
        module_commands = self.modules.commands if self.modules is not None else {}
        diagnostic_commands = self.diagnostics.commands if self.diagnostics is not None else {}
        chain_commands = self.chains.commands if self.chains is not None else {}
        test_commands = self.mock_tests.commands if self.mock_tests is not None else {}
        data_commands = {**skill_commands, **discovery_commands, **diagnostic_commands, **chain_commands}
        tools = []
        for name, (model, description) in schemas.items():
            read_only = name == "gateway_inspect"
            tools.append(types.Tool(name=name, description=description, inputSchema=model.model_json_schema(),
                                    annotations=types.ToolAnnotations(readOnlyHint=read_only, openWorldHint=False)))

        for name, (model, description, read_only, _) in {**data_commands, **module_commands, **test_commands}.items():
            tools.append(types.Tool(name=name, description=description, inputSchema=model.model_json_schema(),
                                    annotations=types.ToolAnnotations(readOnlyHint=read_only, openWorldHint=False)))

        async def catalog(cursor):
            """List only the fixed management tools."""
            return catalog_page(tools, cursor, 0)

        async def call(params):
            """Validate and execute an explicitly supported operation."""
            if params.name in test_commands:
                return await self.call_mock_test(test_commands[params.name], params.arguments or {})
            if params.name in module_commands:
                model, _, _, execute = module_commands[params.name]
                try:
                    query = model.model_validate(params.arguments or {})
                    meta = params.meta.model_dump(by_alias=True, exclude_none=True) if params.meta else None
                    result = await execute(query, meta)
                except ValidationError:
                    return tool_error("Invalid module arguments")
                except ModuleError as error:
                    return tool_error(str(error))
                except OSError:
                    return tool_error("Unable to save module settings")
                return types.CallToolResult(content=[], structuredContent=result)
            if params.name in data_commands:
                model, _, _, execute = data_commands[params.name]
                scope = "skill"
                if params.name in diagnostic_commands:
                    scope = "diagnostic"
                elif params.name in discovery_commands:
                    scope = "discovery"
                elif params.name in chain_commands:
                    scope = "chain suggestion"
                try:
                    query = model.model_validate(params.arguments or {})
                    result = await execute(query)
                except ValidationError:
                    return tool_error(f"Invalid {scope} arguments")
                except (FeedbackError, SkillStoreError) as error:
                    return tool_error(str(error))
                except (OSError, sqlite3.Error):
                    return tool_error(f"{scope.capitalize()} storage is unavailable")
                return types.CallToolResult(content=[], structuredContent=result)
            if params.name not in schemas:
                return tool_error("Unknown management tool")
            model, _ = schemas[params.name]
            try:
                query = model.model_validate(params.arguments or {})
                if isinstance(query, InspectQuery):
                    result = self.inspect(query)
                elif isinstance(query, PolicyChange):
                    revision = self.settings.change(query)
                    result = {"revision": revision, "tool": query.tool, "policy": query.policy}
                elif params.name == "gateway_reconnect_basic":
                    result = {"backend": query.backend,
                              "connected": await self.router.connections.rotate(query.backend, None)}
                else:
                    connected = await self.router.connections.reconnect(query.backend)
                    result = {"backend": query.backend, "connected": connected,
                              "catalog_revision": self.router.catalog_revision}
            except ValidationError:
                return tool_error("Invalid management arguments")
            except ReauthorizationError as error:
                recovery = docker_recovery(query.backend, str(error))
                if recovery is not None:
                    return tool_error(recovery["message"], details=recovery)
                return tool_error(str(error))
            except ValueError as error:
                return tool_error(str(error))
            except OSError:
                return tool_error("Unable to save tool policy")
            return types.CallToolResult(content=[], structuredContent=result)

        return make_server(catalog, call)

    async def call_mock_test(self, command, arguments) -> types.CallToolResult:
        """Dispatch one test command and preserve its safe machine-readable failure code."""
        model, _, _, execute = command
        try:
            result = await execute(model.model_validate(arguments))
        except ValidationError as error:
            issues = argument_issues(error)
            return tool_error(describe_issues("Invalid mock test arguments", issues), details={
                "code": "invalid_test_arguments", "issues": issues, "issue_count": error.error_count(),
            })
        except MockTestError as error:
            details = {"code": error.code}
            message = str(error)
            if error.issues:
                details["issues"] = error.issues
                message = describe_issues("Scenario validation failed", error.issues)
            return tool_error(message, details=details)
        except (OSError, sqlite3.Error):
            return tool_error("Mock test storage is unavailable", details={"code": "test_storage_unavailable"})
        return types.CallToolResult(content=[], structuredContent=result)

    def _browser_query(self, request: Request) -> tuple[int, str | None]:
        """Align browser offsets and use only a backend configured on this profile."""
        value = request.query_params.get("offset", "")
        offset = int(value) if value.isascii() and value.isdecimal() and len(value) <= 6 else 0
        backend = request.query_params.get("backend")
        return offset - offset % _PAGE_SIZE, backend if backend in self.router.profile.backends else None

    async def overview(self, request: Request) -> Response:
        """Render one policy page and report a current verified save, when present."""
        offset, backend = self._browser_query(request)
        notice = ""
        if self.confirmation is not None:
            receipt, revision, message = self.confirmation
            if request.query_params.get("saved") == receipt and self.policy_state.revision == revision:
                notice = message
        return HTMLResponse(self.page(offset, backend, notice=notice), headers=LIVE_SETTINGS_HTML)

    async def submit(self, request: Request) -> Response:
        """Apply one same-origin browser policy edit through the Bridge's CAS path."""
        if self.csrf is None:
            return PlainTextResponse(tr("Не найдено"), status_code=404, headers=SECURE_HTML)
        if request.headers.get("origin") != self.origin:
            return PlainTextResponse(tr("Недопустимый источник формы"), status_code=403, headers=SECURE_HTML)
        offset, backend = self._browser_query(request)
        try:
            fields = await read_form(request, limit=4096, max_fields=4)
        except ValueError:
            fields = {}
        if not compare_digest(fields.pop("csrf", "").encode(), self.csrf.encode()):
            return PlainTextResponse(tr("Недопустимый токен формы"), status_code=403, headers=SECURE_HTML)
        try:
            if set(fields) != {"tool", "policy", "expected_revision"}:
                raise ValueError("invalid policy form")
            raw_revision = fields["expected_revision"]
            if not raw_revision.isascii() or not raw_revision.isdecimal() or len(raw_revision) > 19:
                raise ValueError("invalid policy revision")
            change = PolicyChange(tool=fields["tool"], policy=fields["policy"],
                                  expected_revision=int(raw_revision))
        except ValueError:
            return self._policy_error(offset, backend, "Проверьте инструмент и политику.", 400)
        if change.expected_revision != self.settings.state.revision:
            return self._policy_error(offset, backend, "Политика уже изменилась. Проверьте текущую версию и повторите.", 409)
        try:
            revision = self.settings.change(change)
        except ValueError:
            return self._policy_error(offset, backend, "Эта политика сейчас недоступна для инструмента.", 400)
        except OSError:
            return self._policy_error(offset, backend, "Не удалось сохранить политику. Повторите позже.", 503)
        message = "Политика уже актуальна." if revision == change.expected_revision else "Политика сохранена."
        receipt = token_urlsafe(24)
        self.confirmation = (receipt, revision, message)
        return RedirectResponse(_settings_url(offset, backend, receipt) + "#tool-policy",
                                status_code=303, headers=SECURE_HTML)

    def _policy_error(self, offset: int, backend: str | None, message: str, status: int) -> Response:
        """Show current policy data after a rejected edit without reflecting form text."""
        return HTMLResponse(self.page(offset, backend, notice=message, error=True),
                            status_code=status, headers=LIVE_SETTINGS_HTML)

    def _connection_rows(self, connections: list[dict]) -> str:
        """Render credential entry points and status for the selected MCPs."""
        rows = []
        sources = {"os": "Хранилище ОС", "os_then_env": "Хранилище ОС, затем окружение",
                   "env": "Окружение", "none": "Без учётных данных"}
        for connection in connections:
            name = escape(connection["backend"])
            preference = sources[connection["secret_preference"]]
            link = ""
            if "credentials_url" in connection:
                url = escape(connection["credentials_url"], quote=True)
                link = f'<p><a href="{url}">{tr("Настроить Basic")}</a></p>'
            status = connection_status(connection["status"], connection["secret_preference"] != "none")
            last_call = last_call_line(connection["last_call_at"])
            rows.append(f"<li><strong>{name}</strong> {status}<p>{tr(preference)}</p>{last_call}{link}</li>")
        return "".join(rows)

    def _policy_form(self, tool: dict, revision: int, action: str) -> str:
        """Render only choices allowed by the current tool or its configured baseline."""
        if self.csrf is None:
            return ""
        name = tool["tool"]
        route = self.router.routes.get(name)
        annotations = route.tool.annotations if route is not None else None
        singleflight_allowed = name in self.settings.baseline_singleflight or bool(
            annotations and annotations.readOnlyHint is True
        )
        options = []
        for policy, label in _POLICY_LABELS.items():
            current = policy == tool["policy"]
            if policy == "singleflight" and not singleflight_allowed and not current:
                continue
            selected = " selected" if current else ""
            disabled = " disabled" if policy == "singleflight" and not singleflight_allowed else ""
            options.append(f'<option value="{policy}"{selected}{disabled}>{tr(label)}</option>')
        escaped_name = escape(name, quote=True)
        select_label = escape(tr("Политика для {tool}").format(tool=name), quote=True)
        save_label = escape(tr("Сохранить политику для {tool}").format(tool=name), quote=True)
        return f"""<form class="gateway-filter" method="post" action="{action}" data-ag-form-row="stack">
<input type="hidden" name="csrf" value="{escape(self.csrf, quote=True)}">
<input type="hidden" name="tool" value="{escaped_name}">
<input type="hidden" name="expected_revision" value="{revision}">
<div class="ag-select" data-ag-form-field><label for="policy-{escaped_name}">{tr("Политика")}</label>
<div class="gateway-control-line">
<select class="ag-select__control" id="policy-{escaped_name}" name="policy" aria-label="{select_label}">{"".join(options)}</select>
<button class="ag-button ag-button--secondary" type="submit" aria-label="{save_label}">{tr("Сохранить политику")}</button>
</div></div></form>"""

    def _policy_rows(self, tools: list[dict], revision: int, action: str) -> str:
        """Show configured policy and distinguish missing tools from paused coalescing."""
        rows = []
        for tool in tools:
            label = tr(_POLICY_LABELS[tool["policy"]])
            if not tool["discovered"]:
                label += " · " + tr("Нет в каталоге")
            elif tool["policy"] == "singleflight" and tool["effective_policy"] != "singleflight":
                label += " · " + tr("приостановлен")
            route = self.router.routes.get(tool["tool"])
            summary = route.tool.description if route is not None else None
            description = f'<p class="policy-description muted">{escape(summary[:300])}</p>' if summary else ""
            form = self._policy_form(tool, revision, action)
            rows.append(f'<li><strong>{escape(tool["tool"])}</strong> · {label}{description}{form}</li>')
        return "".join(rows) or f"<li>{tr('Инструменты не найдены.')}</li>"

    def _backend_filter(self, backend: str | None) -> str:
        """Render a native GET filter so pagination never spans unrelated MCPs."""
        return backend_filter(self.router.profile.backends, backend, path="/settings", identity="policy-backend",
                              label="Фильтр по MCP", action="Применить фильтр")

    def page(self, offset: int, backend: str | None = None, *, notice: str = "", error: bool = False) -> str:
        """Render the scoped, revisioned policy view without exposing secret values."""
        current = self.inspect(InspectQuery(offset=offset, backend=backend))
        total = current["total_tools"]
        last_offset = max(0, (total - 1) // _PAGE_SIZE * _PAGE_SIZE)
        if offset > last_offset:
            offset = last_offset
            current = self.inspect(InspectQuery(offset=offset, backend=backend))
        action = escape(_settings_url(offset, backend), quote=True)
        policies = self._policy_rows(current["tools"], current["revision"], action)
        rows = self._connection_rows(current["connections"])
        links = []
        if offset:
            previous = escape(_settings_url(max(0, offset - _PAGE_SIZE), backend), quote=True)
            links.append(f'<a rel="prev" href="{previous}#tool-policy">{tr("Назад")}</a>')
        if current["next_offset"] is not None:
            following = escape(_settings_url(current["next_offset"], backend), quote=True)
            links.append(f'<a rel="next" href="{following}#tool-policy">{tr("Далее")}</a>')
        count = tr("Показано {start}–{stop} из {total}").format(
            start=offset + 1, stop=offset + len(current["tools"]), total=total,
        ) if total else ""
        mode = (
            "Изменения действуют для следующих вызовов в текущей сессии."
            if self.csrf is not None else "Режим просмотра. Для изменения политики откройте защищённый профиль."
        )
        refresh = revision_notice("policy", self.browser_revision(), _settings_url(offset, backend) + "#tool-policy")
        bridge = (
            f'<p>{tr("Через MCP Bridge также доступны")} <code>gateway_<wbr>inspect</code> · '
            '<code>gateway_<wbr>set_<wbr>tool_<wbr>policy</code>.</p>'
            if self.bridge_enabled else ""
        )
        profile = self.router.profile
        sources = []
        if profile.access_token_keychain:
            sources.append(tr("Хранилище ОС"))
        if profile.access_token_env:
            sources.append(tr("Окружение"))
        token_source = f'<p>{tr("Источник токена шлюза")} · {" → ".join(sources)}.</p>' if sources else ""
        feedback = ""
        if notice:
            role = "alert" if error else "status"
            feedback = f'<p role="{role}">{escape(tr(notice))}</p>'
        content = f"""<section id="tool-policy" data-ag-width="fill" aria-labelledby="section-title">
<p class="muted">{tr(mode)}</p>
{refresh}{feedback}{self._backend_filter(backend)}
<p class="muted">{count}</p>
<ul class="tools">{policies}</ul>
<nav class="backend-actions" aria-label="{tr("Страницы инструментов")}">{"".join(links)}</nav></section>
<section data-ag-width="fill" aria-labelledby="sources"><h2 id="sources">{tr("Источники учётных данных MCP")}</h2>
<ul class="tools">{rows}</ul>
<details><summary>{tr("Управление")}</summary>
<p>{tr("Ревизия политики")} {current["revision"]}.</p>{token_source}{bridge}
</details></section>"""
        return shell("/settings", current["profile"], tr("Политика инструментов"), content)
