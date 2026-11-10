"""Expose bounded dashboard settings through a separate local MCP surface."""

from html import escape

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connections import ReauthorizationError
from .mcp_server import catalog_page, make_server
from .policy_settings import PolicyChange
from .profile import HttpBackend, Identifier
from .router import tool_error


class InspectQuery(BaseModel):
    """Page through live and configured tool policies, optionally for one backend."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    backend: Identifier | None = None
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=100, strict=True)


class ReconnectRequest(BaseModel):
    """Reconnect exactly one configured Basic backend using its saved item."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    backend: Identifier


class Management:
    """Keep management commands separate from proxied tools and secret values."""

    def __init__(self, router, settings, origin: str, credentials: bool):
        """Bind this management surface to the selected profile and loopback origin."""
        self.router = router
        self.settings = settings
        self.origin = origin
        self.credentials = credentials

    def inspect(self, query: InspectQuery) -> dict:
        """Expose current policy revision, connection state and browser destinations."""
        profile = self.router.profile
        if query.backend is not None and query.backend not in profile.backends:
            raise ValueError("backend is not in this profile")
        views = {"catalog": f"{self.origin}/", "settings": f"{self.origin}/settings"}
        if self.credentials:
            views["credentials"] = f"{self.origin}/credentials"
        names = set(self.router.routes) | profile.denied | profile.singleflight
        names |= {tool for tool in self.settings.state.overrides if tool.partition(".")[0] in profile.backends}
        selected = sorted(name for name in names if query.backend is None or name.startswith(query.backend + "."))
        end = query.offset + query.limit
        tools = []
        for name in selected[query.offset:end]:
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
            entry = {"backend": name, "status": self.router.statuses[name], "secret_preference": source}
            if self.credentials and name in self.router.connections.accounts:
                entry["credentials_url"] = f"{views['credentials']}?backend={name}"
            connections.append(entry)
        return {
            "profile": profile.endpoint_key,
            "revision": self.settings.state.revision,
            "views": views,
            "connections": connections,
            "tools": tools,
            "total_tools": len(selected),
            "next_offset": end if end < len(selected) else None,
        }

    def server(self):
        """Create a tool-only MCP service with strict command input schemas."""
        schemas = {
            "gateway_inspect": (InspectQuery, "Inspect gateway views, connections and paged tool policies."),
            "gateway_set_tool_policy": (PolicyChange, "Set one tool to allow, deny or singleflight using the current revision."),
            "gateway_reconnect_basic": (ReconnectRequest, "Verify and reconnect one MCP with its stored Basic credential."),
        }
        tools = []
        for name, (model, description) in schemas.items():
            read_only = name == "gateway_inspect"
            tools.append(types.Tool(name=name, description=description, inputSchema=model.model_json_schema(),
                                    annotations=types.ToolAnnotations(readOnlyHint=read_only, openWorldHint=False)))

        async def catalog(cursor):
            """List only the fixed management tools."""
            return catalog_page(tools, cursor, 0)

        async def call(params):
            """Validate and execute an explicitly supported operation."""
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
                else:
                    result = {"backend": query.backend,
                              "connected": await self.router.connections.rotate(query.backend, None)}
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
            rows.append(f"<li><strong>{name}</strong><p>{preference}</p>{link}</li>")
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
