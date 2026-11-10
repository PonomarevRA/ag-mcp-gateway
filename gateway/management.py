"""Expose bounded dashboard settings through a separate local MCP surface."""

import sqlite3
from bisect import bisect_left
from html import escape

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connections import ReauthorizationError
from .dashboard import connection_status, last_call_line
from .mcp_server import catalog_page, make_server
from .module_data import ModuleError
from .module_runtime import ModuleService
from .policy_settings import PolicyChange
from .profile import HttpBackend, Identifier
from .router import tool_error
from .skill_store import SkillStore, SkillStoreError
from .skill_tools import SkillTools
from .tool_discovery import Discovery
from .tool_feedback import FeedbackError, ToolFeedback


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

    def __init__(self, router, settings, origin: str, credentials: bool, skill_store: SkillStore | None = None, feedback: ToolFeedback | None = None):
        """Bind this management surface to the selected profile and loopback origin."""
        self.router = router
        self.settings = settings
        self.origin = origin
        self.credentials = credentials
        self._policy_key: tuple[int, int] | None = None
        self._policy_names: tuple[str, ...] = ()
        self.skills = SkillTools(skill_store, router) if skill_store is not None else None
        self.discovery = Discovery(router, feedback, self.skills) if feedback is not None else None
        self.modules = ModuleService(router.modules, router) if router.modules is not None else None

    def policy_names(self) -> tuple[str, ...]:
        """Reuse the ordered policy union until route or override membership changes."""
        state = self.settings.state
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
            connections.append(entry)
        return {
            "profile": profile.endpoint_key,
            "revision": self.settings.state.revision,
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
        data_commands = {**skill_commands, **discovery_commands}
        tools = []
        for name, (model, description) in schemas.items():
            read_only = name == "gateway_inspect"
            tools.append(types.Tool(name=name, description=description, inputSchema=model.model_json_schema(),
                                    annotations=types.ToolAnnotations(readOnlyHint=read_only, openWorldHint=False)))

        for name, (model, description, read_only, _) in {**data_commands, **module_commands}.items():
            tools.append(types.Tool(name=name, description=description, inputSchema=model.model_json_schema(),
                                    annotations=types.ToolAnnotations(readOnlyHint=read_only, openWorldHint=False)))

        async def catalog(cursor):
            """List only the fixed management tools."""
            return catalog_page(tools, cursor, 0)

        async def call(params):
            """Validate and execute an explicitly supported operation."""
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
                scope = "discovery" if params.name in discovery_commands else "skill"
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
            except (ValueError, ReauthorizationError) as error:
                return tool_error(str(error))
            except OSError:
                return tool_error("Unable to save tool policy")
            return types.CallToolResult(content=[], structuredContent=result)

        return make_server(catalog, call)

    def page(self, offset: int) -> str:
        """Render saved tool policies and credential entry points for operators."""
        current = self.inspect(InspectQuery(offset=offset))
        rows = []
        sources = {"os": "Хранилище ОС", "os_then_env": "Хранилище ОС, затем окружение",
                   "env": "Окружение", "none": "Без учётных данных"}
        for connection in current["connections"]:
            name = escape(connection["backend"])
            preference = sources[connection["secret_preference"]]
            link = ""
            if "credentials_url" in connection:
                url = escape(connection["credentials_url"], quote=True)
                link = f'<p><a href="{url}">Настроить Basic</a></p>'
            status = connection_status(connection["status"], connection["secret_preference"] != "none")
            last_call = last_call_line(connection["last_call_at"])
            rows.append(f"<li><strong>{name}</strong> {status}<p>{preference}</p>{last_call}{link}</li>")
        policies = []
        labels = {"allow": "Разрешён", "deny": "Запрещён", "singleflight": "Singleflight"}
        for tool in current["tools"]:
            label = labels[tool["policy"]]
            if tool["policy"] == "singleflight" and tool["effective_policy"] != "singleflight":
                label += " · приостановлен"
            policies.append(f'<li><strong>{escape(tool["tool"])}</strong> · {label}</li>')
        if not policies:
            policies.append("<li>Инструменты не найдены.</li>")
        links = []
        if offset:
            links.append(f'<a href="/settings?offset={max(0, offset - 100)}">Назад</a>')
        if current["next_offset"] is not None:
            links.append(f'<a href="/settings?offset={current["next_offset"]}">Далее</a>')
        gateway = self.router.profile
        token_source = "Хранилище ОС" if gateway.access_token_keychain else "Окружение"
        if gateway.access_token_keychain and gateway.access_token_env:
            token_source = "Хранилище ОС, затем окружение"
        return f"""<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Настройки MCP</title><link rel="stylesheet" href="/dashboard.css">
<main><header><div><h1>Настройки MCP</h1><p>Профиль {escape(current["profile"])}</p></div>
<a href="/">К каталогу</a></header><section aria-labelledby="policy"><h2 id="policy">Управление</h2>
<p>MCP Bridge включён. Ревизия политики {current["revision"]}.</p>
<p>Источник токена шлюза — {token_source}.</p></section>
<section aria-labelledby="tool-policy"><h2 id="tool-policy">Политика инструментов</h2>
<ul class="tools">{"".join(policies)}</ul>
<nav class="backend-actions" aria-label="Страницы инструментов">{"".join(links)}</nav></section>
<section aria-labelledby="sources"><h2 id="sources">Источники учётных данных MCP</h2>
<ul class="tools">{"".join(rows)}</ul></section></main></html>"""
