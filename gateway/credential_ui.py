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
from .ui_locale import tr
from .ui_shell import shell

_LIMIT = 32_768
_MESSAGES = {
    "saved": "Подключение проверено. Учётные данные сохранены в Связке ключей.",
    "saved-offline": "Учётные данные сохранены. Новая сессия закрылась до переключения. Шлюз продолжит использовать прежнее подключение, если оно доступно.",
    "verified": "Сохранённые данные прошли проверку на отдельном подключении.",
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
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    # Keep the browser's same-origin POST Origin while suppressing cross-origin referrers.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}

LIVE_SETTINGS_HTML = {
    **SECURE_HTML,
    "Content-Security-Policy": SECURE_HTML["Content-Security-Policy"] + "; connect-src 'self'",
}


class CredentialsUI:
    """Expose the profile's opt-in Basic backends without displaying stored values."""

    def __init__(self, connections, origin: str, enabled: bool):
        """Bind forms and their CSRF token to one running loopback profile."""
        self.connections = connections
        self.origin = origin
        self.protected = enabled
        self.enabled = enabled and bool(connections.accounts)
        self.csrf = token_urlsafe(32)
        self.confirmations: dict[str, tuple[str, str]] = {}

    async def overview(self, request: Request) -> Response:
        """Render credential controls and fixed confirmation text."""
        if not self.enabled:
            return self._unavailable()
        selected = request.query_params.get("backend")
        if selected is not None and selected not in self.connections.accounts:
            return self._error("not-enabled")
        receipt = request.query_params.get("confirmation")
        return self._page(self._confirmation(selected, receipt), selected=selected)

    def _unavailable(self) -> Response:
        """Explain which profile prerequisite is missing and link to the profile controls."""
        if not self.protected:
            reason = "В профиле без токена шлюза управление учётными данными отключено."
        else:
            reason = "В этом профиле нет MCP с Basic через Связку ключей macOS."
        content = f"""<section class="credential-unavailable" aria-labelledby="section-title">
<p>{tr(reason)}</p>
<p class="muted">{tr("Выберите защищённый профиль с Basic MCP или настройте такой профиль.")}</p>
<div class="backend-actions">
<a class="primary-action" href="agm-gateway://profiles">{tr("Выбрать профиль")}</a>
<a href="agm-gateway://add-mcp/{escape(self.connections.profile.endpoint_key, quote=True)}">{tr("Настроить Basic MCP")}</a>
</div>
</section>"""
        page = shell("/credentials", self.connections.profile.endpoint_key, tr("Учётные данные MCP"), content)
        return HTMLResponse(page, headers=SECURE_HTML)

    async def update(self, request: Request) -> Response:
        """Validate the protected form, then start one scoped credential transaction."""
        name = request.path_params["backend"]
        if not self.enabled or name not in self.connections.accounts:
            return PlainTextResponse(tr("Не найдено"), status_code=404, headers=SECURE_HTML)
        if request.headers.get("origin") != self.origin:
            return PlainTextResponse(tr("Недопустимый источник формы"), status_code=403, headers=SECURE_HTML)
        operation = ""
        try:
            fields = await read_form(request)
            csrf = fields.pop("csrf", "")
            if not compare_digest(csrf.encode(), self.csrf.encode()):
                return PlainTextResponse(tr("Недопустимый токен формы"), status_code=403, headers=SECURE_HTML)
            operation = fields.pop("operation", "")
            if operation == "save" and set(fields) == {"login", "password"}:
                authorization = basic_authorization(fields["login"], fields["password"])
            elif operation == "paste" and set(fields) == {"basic"}:
                authorization = pasted_authorization(fields["basic"])
            elif operation in {"verify", "reconnect"} and not fields:
                authorization = None
            else:
                raise ValueError("invalid form")
        except ValueError:
            return self._error("invalid", name, operation=operation)

        try:
            if operation == "verify":
                connected = await self.connections.verify_saved(name)
            else:
                connected = await self.connections.rotate(name, authorization)
        except ReauthorizationError as error:
            return self._error(str(error), name, operation=operation)

        if operation in {"save", "paste"}:
            result = "saved" if connected else "saved-offline"
        elif connected:
            result = "verified" if operation == "verify" else "connected"
        else:
            return self._error("connection-failed", name, operation=operation)
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
        return tr(_MESSAGES[result])

    def _error(self, code: str, selected: str | None = None, *, operation: str = "") -> Response:
        """Translate known errors without exposing transport messages or form values."""
        if code in _ERRORS:
            status, message = _ERRORS[code]
            message = tr(message)
        elif code in FAILURE_LABELS:
            message, _tone = FAILURE_LABELS[code]
            status = 502
            message = tr(message) + "."
        else:
            status, message = _ERRORS["connection-failed"]
            message = tr(message)
        worker = self.connections.active.get(selected)
        if status in {502, 503} and worker is not None and worker.live:
            message += " " + tr("Действующее подключение сохранено.")
        return self._page(message, status, selected=selected, failed_operation=operation,
                          correct_input=code in {"invalid", "auth-rejected"})

    def _page(self, message: str, status: int = 200, *, selected: str | None = None,
              failed_operation: str = "", correct_input: bool = False) -> Response:
        """Render Basic forms and direct error recovery to the failed form."""
        names = [selected] if selected else sorted(self.connections.accounts)
        controls = "".join(self._controls(name, failed_operation=failed_operation, correct_input=correct_input)
                           for name in names)
        role = "alert" if status >= 400 else "status"
        notice = f'<p id="credential-feedback" role="{role}">{escape(message)}</p>' if message else ""
        content = f"""<section data-ag-width="fill" aria-label="{tr("Переавторизация")}">
<p>{tr("Можно проверить или сменить данные при работающем шлюзе. Новые данные сохраняются в Связке ключей macOS после успешной проверки.")}</p>
{notice}<ul class="tools">{controls}</ul></section>"""
        content = shell("/credentials", self.connections.profile.endpoint_key, tr("Учётные данные MCP"), content, language_backend=selected)
        return HTMLResponse(content, status_code=status, headers=SECURE_HTML)

    def _controls(self, name: str, *, failed_operation: str = "", correct_input: bool = False) -> str:
        """Build accessible forms whose fields never echo secrets back to the browser."""
        label = escape(name)
        action = f"/credentials/{label}"
        csrf = f'<input type="hidden" name="csrf" value="{self.csrf}">'
        worker = self.connections.active.get(name)
        if worker is not None and worker.live:
            state = f'<p class="status-badge status-ready"><span aria-hidden="true">✓</span> {tr("MCP подключён")}</p>'
        else:
            code = worker.status if worker is not None else "starting"
            title, tone = FAILURE_LABELS.get(code, ("Подключение недоступно", "error"))
            state = f'<p class="status-badge status-{tone}">{escape(tr(title))}</p>'
        # Keep the chosen form visible without reflecting any submitted value.
        feedback = ' aria-describedby="credential-feedback"'
        save_help = feedback if failed_operation == "save" else ""
        save_focus = " autofocus" if failed_operation == "save" and correct_input else ""
        paste_help = feedback if failed_operation == "paste" else ""
        paste_open = " open" if failed_operation == "paste" else ""
        paste_focus = ' aria-invalid="true" autofocus' if failed_operation == "paste" and correct_input else ""
        verify_help = feedback if failed_operation == "verify" else ""
        reconnect_help = feedback if failed_operation == "reconnect" else ""
        return f"""<li><div class="backend-heading"><h2>{label}</h2>{state}</div>
<form class="credential-actions" method="post" action="{action}">
{csrf}<button type="submit" name="operation" value="verify"{verify_help}>{tr("Проверить сохранённые данные")}</button>
<button type="submit" name="operation" value="reconnect"{reconnect_help}>{tr("Подключиться с сохранёнными данными")}</button></form>
<details{" open" if failed_operation in {"save", "paste"} else ""}><summary>{tr("Сменить учётные данные")}</summary>
<form class="credential-form" method="post" action="{action}" data-ag-form-row="stack">
{csrf}<input type="hidden" name="operation" value="save">
<div data-ag-width="fill" data-ag-form-field><label for="{label}-login">{tr("Логин")}</label>
<input id="{label}-login" name="login" required maxlength="256" autocomplete="off"{save_help}{save_focus}></div>
<div data-ag-width="fill" data-ag-form-field><label for="{label}-password">{tr("Пароль")}</label>
<input id="{label}-password" type="password" name="password" required maxlength="1024" autocomplete="new-password"></div>
<button type="submit">{tr("Проверить и сохранить")}</button></form>
<details{paste_open}><summary>{tr("Ввести готовый Basic / Base64")}</summary>
<form class="credential-form" method="post" action="{action}" data-ag-form-row="stack">
{csrf}<input type="hidden" name="operation" value="paste">
<div data-ag-width="fill" data-ag-form-field><label for="{label}-basic">Basic / Base64</label>
<input id="{label}-basic" type="password" name="basic" required maxlength="8198" autocomplete="new-password"{paste_help}{paste_focus}></div>
<button type="submit">{tr("Проверить и сохранить Basic")}</button></form></details>
</details></li>"""


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
