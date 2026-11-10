"""Accept credential updates from an authenticated same-origin dashboard form."""

from html import escape
from hmac import compare_digest
from secrets import token_urlsafe
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .connections import ReauthorizationError
from .credentials import basic_authorization

_LIMIT = 16_384
_MESSAGES = {
    "saved": "Подключение проверено. Учётные данные сохранены в Связке ключей.",
    "saved-offline": "Учётные данные сохранены. Новая сессия закрылась до переключения. Шлюз продолжит использовать прежнее подключение, если оно доступно.",
    "connected": "MCP подключён с сохранёнными учётными данными.",
}
_ERRORS = {
    "invalid": (400, "Проверьте логин и пароль и повторите отправку формы."),
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

    async def overview(self, request: Request) -> Response:
        """Render credential controls and fixed confirmation text."""
        if not self.enabled:
            return PlainTextResponse("Not found", status_code=404, headers=SECURE_HTML)
        return self._page(_MESSAGES.get(request.query_params.get("result", ""), ""))

    async def update(self, request: Request) -> Response:
        """Validate the protected form, then start one scoped credential transaction."""
        name = request.path_params["backend"]
        if not self.enabled or name not in self.connections.accounts:
            return PlainTextResponse("Not found", status_code=404, headers=SECURE_HTML)
        if request.headers.get("origin") != self.origin:
            return PlainTextResponse("Invalid form origin", status_code=403, headers=SECURE_HTML)
        try:
            fields = await _form(request)
            csrf = fields.pop("csrf", "")
            if not compare_digest(csrf.encode(), self.csrf.encode()):
                return PlainTextResponse("Invalid form token", status_code=403, headers=SECURE_HTML)
            operation = fields.pop("operation", "")
            if operation == "save" and set(fields) == {"login", "password"}:
                authorization = basic_authorization(fields["login"], fields["password"])
            elif operation == "reconnect" and not fields:
                authorization = None
            else:
                raise ValueError("invalid form")
        except ValueError:
            return self._error("invalid")

        try:
            connected = await self.connections.rotate(name, authorization)
        except ReauthorizationError as error:
            return self._error(str(error))

        if operation == "save":
            result = "saved" if connected else "saved-offline"
        elif connected:
            result = "connected"
        else:
            return self._error("connection-failed")
        return RedirectResponse(f"/credentials?result={result}", status_code=303, headers=SECURE_HTML)

    def _error(self, code: str) -> Response:
        """Translate known errors without exposing transport messages or form values."""
        status, message = _ERRORS.get(code, _ERRORS["connection-failed"])
        return self._page(message, status)

    def _page(self, message: str, status: int = 200) -> Response:
        """Render the profile and one native form pair for each configured Basic backend."""
        controls = "".join(self._controls(name) for name in sorted(self.connections.accounts))
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
        return f"""<li><h2>{label}</h2>
<form class="credential-form" method="post" action="{action}">
{csrf}<input type="hidden" name="operation" value="save">
<div><label for="{label}-login">Логин</label>
<input id="{label}-login" name="login" required maxlength="256" autocomplete="off"></div>
<div><label for="{label}-password">Пароль</label>
<input id="{label}-password" type="password" name="password" required maxlength="1024" autocomplete="new-password"></div>
<button type="submit">Проверить и сохранить</button></form>
<form class="credential-form" method="post" action="{action}">
{csrf}<input type="hidden" name="operation" value="reconnect">
<button type="submit">Подключиться с сохранёнными данными</button></form></li>"""


async def _form(request: Request) -> dict[str, str]:
    """Read one bounded URL-encoded form and reject repeated or malformed fields."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("invalid content type")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _LIMIT:
            raise ValueError("form exceeds size limit")
        body.extend(chunk)
    fields = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                      errors="strict", max_num_fields=4)
    if any(len(values) != 1 for values in fields.values()):
        raise ValueError("duplicate form field")
    return {key: values[0] for key, values in fields.items()}
