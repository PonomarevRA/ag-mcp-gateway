"""Accept credential updates from an authenticated same-origin dashboard form."""

from html import escape
from hmac import compare_digest
from secrets import token_urlsafe
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .connection_diagnostics import FAILURE_LABELS
from .connections import ReauthorizationError
from .credentials import basic_authorization, pasted_authorization

_LIMIT = 32_768
_MESSAGES = {
    "saved": "Подключение проверено. Учётные данные сохранены в Связке ключей.",
    "saved-offline": "Учётные данные сохранены. Новая сессия закрылась до переключения. Шлюз продолжит использовать прежнее подключение, если оно доступно.",
    "connected": "MCP подключён с сохранёнными учётными данными.",
    "saved-disconnected": "Учётные данные сохранены. Текущее состояние подключения указано ниже.",
}
_ERRORS = {
    "invalid": (400, "Проверьте логин и пароль или строку Basic/Base64."),
    "busy": (409, "Проверка этого MCP уже выполняется."),
    "credentials-missing": (409, "Для этого MCP ещё нет записи в Связке ключей."),
    "keychain-unavailable": (503, "Связка ключей macOS недоступна или не разрешила операцию."),
    "connection-failed": (502, "MCP не подтвердил подключение. Сохранённые данные не изменились."),
    "not-enabled": (404, "Управление учётными данными этого MCP недоступно."),
}
SECURE_HTML = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    # Keep the browser's same-origin POST Origin while suppressing cross-origin referrers.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


class CredentialsUI:
    """Expose the profile's opt-in Basic backends without displaying stored values."""

    def __init__(self, connections, origin: str, enabled: bool):
        """Bind forms and their CSRF token to one running loopback profile."""
        self.connections = connections
        self.origin = origin
        self.enabled = enabled and bool(connections.accounts)
        self.csrf = token_urlsafe(32)
        self.confirmations: dict[str, tuple[str, str]] = {}

    async def overview(self, request: Request) -> Response:
        """Render credential controls and fixed confirmation text."""
        if not self.enabled:
            return PlainTextResponse("Not found", status_code=404, headers=SECURE_HTML)
        selected = request.query_params.get("backend")
        if selected is not None and selected not in self.connections.accounts:
            return self._error("not-enabled")
        receipt = request.query_params.get("confirmation")
        return self._page(self._confirmation(selected, receipt), selected=selected)

    async def update(self, request: Request) -> Response:
        """Validate the protected form, then start one scoped credential transaction."""
        name = request.path_params["backend"]
        if not self.enabled or name not in self.connections.accounts:
            return PlainTextResponse("Not found", status_code=404, headers=SECURE_HTML)
        if request.headers.get("origin") != self.origin:
            return PlainTextResponse("Invalid form origin", status_code=403, headers=SECURE_HTML)
        try:
            fields = await read_form(request)
            csrf = fields.pop("csrf", "")
            if not compare_digest(csrf.encode(), self.csrf.encode()):
                return PlainTextResponse("Invalid form token", status_code=403, headers=SECURE_HTML)
            operation = fields.pop("operation", "")
            if operation == "save" and set(fields) == {"login", "password"}:
                authorization = basic_authorization(fields["login"], fields["password"])
            elif operation == "paste" and set(fields) == {"basic"}:
                authorization = pasted_authorization(fields["basic"])
            elif operation == "reconnect" and not fields:
                authorization = None
            else:
                raise ValueError("invalid form")
        except ValueError:
            return self._error("invalid", name)

        try:
            connected = await self.connections.rotate(name, authorization)
        except ReauthorizationError as error:
            return self._error(str(error), name)

        if operation in {"save", "paste"}:
            result = "saved" if connected else "saved-offline"
        elif connected:
            result = "connected"
        else:
            return self._error("connection-failed", name)
        receipt = token_urlsafe(24)
        self.confirmations[name] = (receipt, result)
        location = f"/credentials?backend={name}&confirmation={receipt}"
        return RedirectResponse(location, status_code=303, headers=SECURE_HTML)

    def _confirmation(self, selected: str | None, receipt: str | None) -> str:
        """Show the latest verified form result for this backend and its current state."""
        confirmation = self.confirmations.get(selected) if selected is not None else None
        if confirmation is None or receipt != confirmation[0]:
            return ""
        result = confirmation[1]
        worker = self.connections.active.get(selected)
        if worker is None or not worker.live:
            if result == "connected":
                return ""
            if result == "saved":
                result = "saved-disconnected"
        return _MESSAGES[result]

    def _error(self, code: str, selected: str | None = None) -> Response:
        """Translate known errors without exposing transport messages or form values."""
        if code in _ERRORS:
            status, message = _ERRORS[code]
        elif code in FAILURE_LABELS:
            message, _tone = FAILURE_LABELS[code]
            status = 502
            message += "."
            worker = self.connections.active.get(selected)
            if worker is not None and worker.live:
                message += " Действующее подключение сохранено."
        else:
            status, message = _ERRORS["connection-failed"]
        return self._page(message, status, selected=selected)

    def _page(self, message: str, status: int = 200, *, selected: str | None = None) -> Response:
        """Render the profile and one native form pair for each configured Basic backend."""
        names = [selected] if selected else sorted(self.connections.accounts)
        controls = "".join(self._controls(name) for name in names)
        notice = f'<p role="status">{escape(message)}</p>' if message else ""
        content = f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Учётные данные MCP</title><link rel="stylesheet" href="/dashboard.css">
<main><header><div><h1>Учётные данные MCP</h1><p>Профиль {escape(self.connections.profile.endpoint_key)}</p></div>
<a href="/">К обзору</a></header>
<section aria-label="Переавторизация">
<p>Шлюз проверит подключение перед сохранением данных в Связке ключей macOS.</p>
{notice}<ul class="tools">{controls}</ul></section></main></html>"""
        return HTMLResponse(content, status_code=status, headers=SECURE_HTML)

    def _controls(self, name: str) -> str:
        """Build accessible forms whose fields never echo secrets back to the browser."""
        label = escape(name)
        action = f"/credentials/{label}"
        csrf = f'<input type="hidden" name="csrf" value="{self.csrf}">'
        worker = self.connections.active.get(name)
        if worker is not None and worker.live:
            state = '<p class="status-badge status-ready"><span aria-hidden="true">✓</span> MCP подключён</p>'
        else:
            code = worker.status if worker is not None else "starting"
            title, tone = FAILURE_LABELS.get(code, ("Подключение недоступно", "error"))
            state = f'<p class="status-badge status-{tone}">{escape(title)}</p>'
        return f"""<li><h2>{label}</h2>{state}
<form class="credential-form" method="post" action="{action}">
{csrf}<input type="hidden" name="operation" value="save">
<div><label for="{label}-login">Логин</label>
<input id="{label}-login" name="login" required maxlength="256" autocomplete="off"></div>
<div><label for="{label}-password">Пароль</label>
<input id="{label}-password" type="password" name="password" required maxlength="1024" autocomplete="new-password"></div>
<button type="submit">Проверить и сохранить</button></form>
<details><summary>Ввести готовый Basic / Base64</summary>
<form class="credential-form" method="post" action="{action}">
{csrf}<input type="hidden" name="operation" value="paste">
<div><label for="{label}-basic">Basic / Base64</label>
<input id="{label}-basic" type="password" name="basic" required maxlength="8198" autocomplete="new-password"></div>
<button type="submit">Проверить и сохранить Basic</button></form></details>
<form class="credential-form" method="post" action="{action}">
{csrf}<input type="hidden" name="operation" value="reconnect">
<button type="submit">Подключиться с сохранёнными данными</button></form></li>"""


async def read_form(request: Request, *, limit: int = _LIMIT, max_fields: int = 4) -> dict[str, str]:
    """Read one bounded URL-encoded form and reject repeated or malformed fields."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("invalid content type")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ValueError("form exceeds size limit")
        body.extend(chunk)
    fields = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                      errors="strict", max_num_fields=max_fields)
    if any(len(values) != 1 for values in fields.values()):
        raise ValueError("duplicate form field")
    return {key: values[0] for key, values in fields.items()}
