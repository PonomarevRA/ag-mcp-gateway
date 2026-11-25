"""Render the same bounded warnings and errors used by the management bridge."""

import sqlite3
from html import escape
from urllib.parse import urlencode

from pydantic import TypeAdapter, ValidationError
from starlette.responses import HTMLResponse, PlainTextResponse

from .agent_diagnostics import AgentDiagnostics, LogQuery, TraceId
from .connection_diagnostics import FAILURE_LABELS
from .credential_ui import SECURE_HTML
from .telemetry_api import permitted_request, sequence_number
from .ui_locale import language, tr
from .ui_shell import shell
from .ui_messages import CALL_ERRORS_RU

_TRACE_ID = TypeAdapter(TraceId)
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
            fields.append(f'<div data-ag-width="content" data-ag-width-min-em="6"><dt>{tr(label)}</dt>'
                          f"<dd><code>{escape(value)}</code></dd></div>")
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
        """Read one bounded page and distinguish a field error from a cursor error."""
        if not permitted_request(request):
            return PlainTextResponse(tr("Доступ запрещён"), status_code=403, headers=SECURE_HTML)
        parameters = request.query_params
        backend = parameters.get("backend", "")
        trace_input = parameters.get("trace_id", "").strip()
        trace_id = trace_input.lower()
        trace_display = trace_input[:32]
        if backend and backend not in self.profile.backends:
            return self.render("", trace_display, notice="Выберите MCP этого профиля.",
                               invalid_field="backend", status=400)
        # Check the visible field first so an expired or malformed cursor cannot mask its error.
        if trace_id:
            try:
                _TRACE_ID.validate_python(trace_id)
            except ValidationError:
                return self.render(backend, trace_display, notice="Trace ID должен содержать 32 шестнадцатеричных символа.",
                                   invalid_field="trace", status=400)
        try:
            value = parameters.get("before")
            before = sequence_number(value) if value is not None else None
            query = LogQuery(backend=backend or None, trace_id=trace_id or None, before_sequence=before,
                             journal_epoch=parameters.get("epoch"))
        except ValueError:
            return self.render(backend, trace_display, notice="Некорректная ссылка на страницу журнала.",
                               reset_cursor=True, status=400)
        try:
            result = await self.reader.errors(query)
        except (OSError, sqlite3.Error):
            return self.render(backend, trace_display, notice="Журнал временно недоступен.", status=503)
        if "error" in result:
            return self.render(backend, trace_display, notice="Эта страница устарела. Откройте свежие события.",
                               reset_cursor=True, status=409)
        return self.render(backend, trace_id, result=result)

    def render(self, backend: str, trace_id: str, *, result=None, notice="", status=200,
               invalid_field: str = "", reset_cursor: bool = False):
        """Render safe filters, event rows and forward navigation to older evidence."""
        title = tr("Диагностика")
        options = [f'<option value="">{tr("Все MCP")}</option>']
        for name in sorted(self.profile.backends):
            selected = " selected" if name == backend else ""
            options.append(f'<option value="{escape(name)}"{selected}>{escape(name)}</option>')
        recent_trace = "" if invalid_field == "trace" else trace_id
        recent = escape("/diagnostics?" + urlencode({"backend": backend, "trace_id": recent_trace}), quote=True)
        message = ""
        if notice:
            summary_focus = "" if invalid_field or reset_cursor else ' tabindex="-1" autofocus'
            message = f'<p id="diagnostic-feedback" role="alert"{summary_focus}>{tr(notice)}</p>'
        field_state = ' aria-invalid="true" aria-describedby="diagnostic-feedback" autofocus'
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
        recovery = ' aria-describedby="diagnostic-feedback" autofocus' if reset_cursor else ""
        content = f"""<section data-ag-width="fill" aria-labelledby="diagnostic-events"><h2 id="diagnostic-events">{tr("Ошибки и предупреждения")}</h2>
{message}
<form class="module-form" action="/diagnostics" method="get" data-ag-form-row="stack">
<div class="module-field" data-ag-width="fill" data-ag-form-field><label for="diagnostic-backend">MCP</label><select id="diagnostic-backend" name="backend"{field_state if invalid_field == "backend" else ""}>{"".join(options)}</select></div>
<div class="module-field" data-ag-width="fill" data-ag-form-field><label for="trace-id">Trace ID</label><input id="trace-id" name="trace_id" value="{escape(trace_id)}" maxlength="32"{field_state if invalid_field == "trace" else ""}></div>
<button>{tr("Найти")}</button><a href="/diagnostics">{tr("Сбросить фильтры")}</a>
</form><ul class="tools">{events}</ul>
<nav class="backend-actions" aria-label="{tr("Страницы журнала")}"><a href="{recent}"{recovery}>{tr("Свежие события")}</a>{older}</nav>
</section>"""
        html = shell("/diagnostics", self.profile.endpoint_key, title, content)
        return HTMLResponse(html, status_code=status, headers=SECURE_HTML)
