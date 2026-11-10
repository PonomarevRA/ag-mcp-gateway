"""Render the same bounded warnings and errors used by the management bridge."""

import sqlite3
from html import escape
from urllib.parse import urlencode

from starlette.responses import HTMLResponse, PlainTextResponse

from .agent_diagnostics import AgentDiagnostics, LogQuery
from .connection_diagnostics import FAILURE_LABELS
from .credential_ui import SECURE_HTML
from .telemetry_api import permitted_request, sequence_number
from .ui_locale import language, language_switch, tr
from .ui_messages import CALL_ERRORS_RU

_LEVELS = {"WARN": "Предупреждение", "ERROR": "Ошибка", "FATAL": "Критическая ошибка"}


def _event_row(event: dict) -> str:
    """Show known diagnostic metadata and keep journal text inert."""
    details = event["properties"]
    code = details.get("errorCode") or details.get("status")
    message = event.get("message") or ""
    if code == "connection-failed":
        message = tr("Подключение недоступно")
    elif isinstance(code, str):
        if code in FAILURE_LABELS:
            message = tr(FAILURE_LABELS[code][0])
        elif language() == "ru":
            message = CALL_ERRORS_RU.get(code, message)
    fields = []
    for label, value in (
        ("MCP", details.get("backend")), ("Инструмент", details.get("tool")),
        ("Код", code), ("Trace ID", event["traceId"]), ("Тег сессии", event["sessionId"]),
    ):
        if isinstance(value, str) and value:
            fields.append(f"<div><dt>{tr(label)}</dt><dd><code>{escape(value)}</code></dd></div>")
    timestamp = escape(str(event.get("occurredAt", "")))
    level = tr(_LEVELS[event["level"]])
    return (f'<li><strong>{level}</strong> · <time datetime="{timestamp}">{timestamp}</time>'
            f'<p>{escape(str(message))}</p><dl class="diagnostic-fields">{"".join(fields)}</dl></li>')


class DiagnosticPage:
    """Provide a browser path from a backend card to its recent failures."""

    def __init__(self, telemetry_api, profile):
        """Bind read-only diagnostics to the active profile and journal."""
        self.reader = AgentDiagnostics(telemetry_api)
        self.profile = profile

    async def overview(self, request):
        """Read one bounded page and report stale cursors without exposing inputs."""
        if not permitted_request(request):
            return PlainTextResponse(tr("Доступ запрещён"), status_code=403, headers=SECURE_HTML)
        parameters = request.query_params
        backend = parameters.get("backend", "")
        trace_id = parameters.get("trace_id", "").strip().lower()
        if backend and backend not in self.profile.backends:
            return self.render("", "", notice="Выберите MCP этого профиля.", status=400)
        try:
            value = parameters.get("before")
            before = sequence_number(value) if value is not None else None
            query = LogQuery(backend=backend or None, trace_id=trace_id or None, before_sequence=before,
                             journal_epoch=parameters.get("epoch"))
        except ValueError:
            return self.render(backend, "", notice="Проверьте trace ID или начните просмотр заново.", status=400)
        try:
            result = await self.reader.errors(query)
        except (OSError, sqlite3.Error):
            return self.render(backend, trace_id, notice="Журнал временно недоступен.", status=503)
        if "error" in result:
            return self.render(backend, trace_id, notice="Эта страница устарела. Откройте свежие события.", status=409)
        return self.render(backend, trace_id, result=result)

    def render(self, backend: str, trace_id: str, *, result=None, notice="", status=200):
        """Render safe filters, event rows and forward navigation to older evidence."""
        title = tr("Диагностика")
        options = [f'<option value="">{tr("Все MCP")}</option>']
        for name in sorted(self.profile.backends):
            selected = " selected" if name == backend else ""
            options.append(f'<option value="{escape(name)}"{selected}>{escape(name)}</option>')
        message = f'<p role="status">{tr(notice)}</p>' if notice else ""
        events = ""
        older = ""
        if result is not None:
            events = "".join(_event_row(event) for event in result["events"])
            message = f'<p role="status">{tr("Проверено событий")} {result["scanned"]}.</p>'
            if not events:
                events = f'<li>{tr("На этой странице нет ошибок и предупреждений.")}</li>'
            cursor = result["next_cursor"]
            if cursor:
                params = {"backend": backend, "trace_id": trace_id,
                          "epoch": cursor["journal_epoch"], "before": cursor["before_sequence"]}
                link = escape("/diagnostics?" + urlencode(params), quote=True)
                older = f'<a rel="next" href="{link}">{tr("Более ранние события")}</a>'
        recent = escape("/diagnostics?" + urlencode({"backend": backend, "trace_id": trace_id}), quote=True)
        html = f"""<!doctype html>
<html lang="{language()}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/dashboard.css">
<main><header><div><h1>{title}</h1><p>{tr("Профиль")} {escape(self.profile.endpoint_key)}</p></div>
<a href="/">{tr("К каталогу")}</a>{language_switch()}</header>
<section aria-labelledby="diagnostic-events"><h2 id="diagnostic-events">{tr("Ошибки и предупреждения")}</h2>
<form action="/diagnostics" method="get">
<label for="diagnostic-backend">MCP</label><select id="diagnostic-backend" name="backend">{"".join(options)}</select>
<label for="trace-id">Trace ID</label><input id="trace-id" name="trace_id" value="{escape(trace_id)}" maxlength="32">
<button>{tr("Найти")}</button><a href="/diagnostics">{tr("Сбросить фильтры")}</a>
</form>{message}<ul class="tools">{events}</ul>
<nav class="backend-actions" aria-label="{tr("Страницы журнала")}"><a href="{recent}">{tr("Свежие события")}</a>{older}</nav>
</section></main></html>"""
        return HTMLResponse(html, status_code=status, headers=SECURE_HTML)
