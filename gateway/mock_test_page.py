"""Present synthetic MCP scenarios and run reports in the gateway workspace."""

import json
from dataclasses import dataclass
from hmac import compare_digest
from html import escape
from urllib.parse import quote
from uuid import uuid4

from pydantic import ValidationError
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .credential_ui import LIVE_SETTINGS_HTML, SECURE_HTML, read_form
from .mock_test_catalog import group_options, hidden, icon_link, label, new_request_url, render_catalog, scenario_url
from .mock_test_models import GetRun, MockTestError, MoveScenario, RunScenario, SaveGroup, SaveScenario, json_equal
from .module_data import MAX_DATA, ModuleError, parse_json
from .ui_shell import icon, shell

_EDITOR_SECTIONS = {
    "arguments": {"id", "title", "tool", "arguments"},
    "fixtures": {"outcome", "structured_content", "text", "fixture_id", "follow_arguments", "match",
                 "additional_fixtures"},
    "checks": {"checks"},
    "options": {"input_schema", "output_schema", "denied"},
    "hooks": {"before", "after"},
}


def json_text(value) -> str:
    """Format JSON for a textarea or an inert text preview."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def json_view(value) -> str:
    """Show report data as text, including markup supplied by a fixture."""
    return f'<div class="module-preview"><pre>{escape(json_text(value))}</pre></div>'


def run_url(identity: str) -> str:
    """Build a report URL from one escaped path segment."""
    return "/tests/runs/" + quote(identity, safe="")


def example_scenario() -> dict:
    """Create an editable example which works without a connected server."""
    return {
        "id": "find-book", "title": label("Поиск книги", "Find a book"), "tool": "demo.find",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "output_schema": {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
        "arguments": {"query": "book"},
        "fixtures": [{
            "id": "found", "match": {"op": "exact", "value": {"query": "book"}},
            "outcome": "result", "structured_content": {"items": [{"id": "book"}]},
        }],
        "checks": [{"op": "equals", "path": "/items/0/id", "value": "book"}],
    }


@dataclass(frozen=True)
class FormNotice:
    """Identify an editable field and a safe error message for its recovery."""

    message: str
    field: str = ""


class FormValueError(ValueError):
    """Mark a text field whose value could not be decoded."""

    def __init__(self, field: str):
        """Keep only the field name for rendering and focus recovery."""
        super().__init__(field)
        self.field = field


def draft_fields(scenario: dict, revision: int) -> dict:
    """Split the common fixture editor from optional rules, schemas and hooks."""
    arguments = scenario.get("arguments", {})
    fixtures = scenario.get("fixtures", [])
    primary = fixtures[0] if fixtures else {}
    match = primary.get("match") or {"op": "exact", "value": arguments}
    fields = {
        "id": scenario.get("id", ""), "title": scenario.get("title", ""), "tool": scenario.get("tool", ""),
        "revision": str(revision), "group_id": "", "fixture_id": primary.get("id", "response"),
        "outcome": primary.get("outcome", "result"), "text": primary.get("text") or "",
        "follow_arguments": "exact" if json_equal(match, {"op": "exact", "value": arguments}) else "custom",
        "denied": "true" if scenario.get("denied") else "false",
    }
    for key, value in (
        ("arguments", arguments), ("match", match), ("input_schema", scenario.get("input_schema", {})),
        ("output_schema", scenario.get("output_schema")), ("before", scenario.get("before")),
        ("after", scenario.get("after")), ("structured_content", primary.get("structured_content")),
        ("additional_fixtures", fixtures[1:]), ("checks", scenario.get("checks", [])),
    ):
        fields[key] = "" if value is None else json_text(value)
    if revision and not fixtures:
        fields["fixture_id"] = ""
    return fields


def form_json(fields: dict, name: str, blank=None):
    """Decode one bounded editor field without retaining decoder error text."""
    raw = fields.get(name, "")
    if not raw.strip():
        return blank
    try:
        return parse_json(raw)
    except ModuleError as error:
        raise FormValueError(name) from error


def form_revision(raw: str) -> int:
    """Decode a small nonnegative form revision for the typed command."""
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        raise FormValueError("")
    return int(raw)


def editor_request(fields: dict, revision: int) -> SaveScenario:
    """Rebuild the same typed revisioned contract used by management MCP."""
    arguments = form_json(fields, "arguments", {})
    fixture = None
    if fields.get("fixture_id"):
        match = {"op": "exact", "value": arguments}
        if fields.get("follow_arguments") != "exact":
            match = form_json(fields, "match")
        fixture = {
            "id": fields["fixture_id"], "match": match,
            "outcome": fields.get("outcome"), "structured_content": form_json(fields, "structured_content"),
            "text": fields.get("text") or None,
        }
    additional = form_json(fields, "additional_fixtures", [])
    if not isinstance(additional, list):
        raise FormValueError("additional_fixtures")
    fixtures = [fixture, *additional] if fixture is not None else additional
    request = {
        "expected_revision": revision, "scenario": {
            "id": fields.get("id"), "title": fields.get("title"), "tool": fields.get("tool"),
            "arguments": arguments, "input_schema": form_json(fields, "input_schema", {}),
            "output_schema": form_json(fields, "output_schema"), "fixtures": fixtures,
            "checks": form_json(fields, "checks", []), "before": form_json(fields, "before"),
            "after": form_json(fields, "after"), "denied": fields.get("denied") == "true",
        },
    }
    if revision == 0 and fields.get("group_id"):
        request["group_id"] = fields["group_id"]
    return SaveScenario.model_validate(request)


def control(name: str, title: str, fields: dict, notice: FormNotice | None, *,
            rows: int = 0, options: tuple = (), required: bool = False) -> str:
    """Render one labelled native form control using the gateway’s shared field layout."""
    value = escape(fields.get(name, ""))
    identity = f"mock-{name}"
    state = " required" if required else ""
    if notice and notice.field == name:
        state += ' aria-invalid="true" aria-describedby="mock-feedback" autofocus'
    if options:
        choices = []
        for option, caption in options:
            selected = " selected" if option == fields.get(name) else ""
            choices.append(f'<option value="{option}"{selected}>{caption}</option>')
        html = f'<select id="{identity}" name="{name}" class="ag-select__control"{state}>{"".join(choices)}</select>'
    elif rows:
        html = f'<textarea id="{identity}" name="{name}" rows="{rows}" maxlength="{MAX_DATA}"{state}>{value}</textarea>'
    else:
        maxlength = {"id": 32, "title": 100, "tool": 128, "fixture_id": 32}[name]
        if name == "id" and fields.get("revision") != "0":
            state += " readonly"
        html = f'<input id="{identity}" name="{name}" value="{value}" maxlength="{maxlength}"{state}>'
    return (f'<div class="module-field" data-ag-form-field data-ag-width="fill">'
            f'<label for="{identity}">{title}</label>{html}</div>')


def icon_submit(action: str, glyph: str, name: str, *, primary: bool = False) -> str:
    """Keep native form submission and expose the named action on pointer and keyboard."""
    caption = escape(name, quote=True)
    style = " mock-primary" if primary else ""
    keys = ("Meta+Enter Control+Enter", "⌘/Ctrl + ↵") if primary else ("Meta+S Control+S", "⌘/Ctrl + S")
    hint = caption + " · " + keys[1]
    return (f'<span class="mock-icon-slot" data-hint="{hint}"><button class="mock-icon{style}" type="submit" '
            f'name="action" value="{action}" aria-label="{caption}" aria-keyshortcuts="{keys[0]}">'
            f'{icon(glyph)}</button></span>')


def scenario_properties(fields: dict, notice: FormNotice | None, groups: list[dict]) -> str:
    """Keep identity close at hand, expanding it for new scenarios and validation errors."""
    fresh = fields["revision"] == "0"
    editing_identity = notice is not None and notice.field in {"title", "id"}
    open_state = " open" if fresh or editing_identity else ""
    caption = label("Свойства сценария", "Scenario properties")
    identity = control("id", label("ID сценария", "Scenario ID"), fields, notice, required=True)
    title = control("title", label("Название", "Title"), fields, notice, required=True)
    name = fields["title"] or label("Новый сценарий", "New scenario")
    revision = label("Черновик", "Draft") if fresh else label("Ревизия", "Revision") + " " + fields["revision"]
    rerun = "" if fresh else (
        f'<button type="submit" form="mock-saved-run" name="action" value="run">'
        f'{label("Запустить сохранённую версию", "Run saved revision")}</button>'
    )
    placement = ""
    if fresh and groups:
        placement = (f'<label class="mock-draft-collection">{label("Коллекция", "Collection")}'
                     f'<select name="group_id">{group_options(groups, fields["group_id"])}</select></label>')
    return f"""<details class="mock-properties" data-ag-width="fill"{open_state}>
<summary aria-label="{escape(caption, quote=True)}" data-hint="{escape(caption, quote=True)}">
<span class="mock-identity-name">{escape(name)}</span><small>{escape(revision)}</small>
<span class="mock-summary-icon">{icon("sliders")}</span></summary>
<div class="mock-property-fields" data-ag-width="fill">
<div class="mock-editor-identity" data-ag-width="fill"><div class="mock-slot">{title}</div>
<div class="mock-slot">{identity}</div></div>{placement}{rerun}</div></details>"""


def editor(fields: dict, csrf: str, notice: FormNotice | None, groups: list[dict],
           *, reviewing_failure: bool = False) -> str:
    """Keep one native form for all panes and open the pane with the rejected field."""
    tool = control("tool", label("Инструмент MCP", "MCP tool"), fields, notice, required=True)
    arguments = control("arguments", label("Аргументы JSON", "JSON arguments"), fields, notice, rows=7)
    checks = control("checks", label("Проверки JSON", "JSON checks"), fields, notice, rows=9)
    panes = (
        ("arguments", label("Аргументы", "Arguments"), arguments),
        ("fixtures", label("Фикстуры", "Fixtures"), fixture_editor(fields, notice)),
        ("checks", label("Проверки", "Checks"), checks),
        ("options", label("Схемы", "Schemas"), schema_editor(fields, notice)),
        ("hooks", label("Хуки", "Hooks"), hook_editor(fields, notice)),
    )
    default = "checks" if reviewing_failure else "arguments"
    active = next((key for key, names in _EDITOR_SECTIONS.items() if notice and notice.field in names), default)
    request_tabs = tab_set("mock-request", label("Настройка вызова", "Request settings"), panes, active)
    run = icon_submit("save-run", "play", label("Сохранить и запустить", "Save and run"), primary=True)
    save = icon_submit("save", "save", label("Сохранить", "Save"))
    return f"""<section id="scenario-editor" class="mock-panel" aria-labelledby="mock-editor-title" data-ag-width="fill">
<header class="mock-panel-heading"><h2 id="mock-editor-title">{label("Запрос", "Request")}</h2></header>
<form id="mock-call-form" class="module-form mock-editor" action="/tests" method="post" data-ag-width="fill">
<input type="hidden" name="csrf" value="{escape(csrf)}">
<input type="hidden" name="revision" value="{escape(fields["revision"])}">
<div class="mock-callbar" data-ag-width="fill">
<span class="mock-protocol" aria-hidden="true">MCP</span><div class="mock-slot">{tool}</div>
<div class="mock-actions">{run}{save}</div></div>
{scenario_properties(fields, notice, groups)}{request_tabs}</form>{saved_run_form(fields, csrf)}</section>"""


def tab_set(prefix: str, name: str, panes: tuple, active: str) -> str:
    """Emit labelled sections that the local script enhances into keyboard tabs."""
    buttons = []
    sections = []
    panel_prefix = f"{prefix}-" if prefix == "mock-request" else ""
    for key, title, content in panes:
        panel = f"{panel_prefix}{key}"
        buttons.append(f'<button type="button" id="{prefix}-tab-{key}" data-mock-tab="{panel}">{title}</button>')
        sections.append(f'<section id="{panel}" data-mock-pane data-ag-width="fill" '
                        f'aria-labelledby="{panel}-title"><h3 id="{panel}-title">{title}</h3>{content}</section>')
    selected = f"{panel_prefix}{active}"
    return (f'<div class="mock-tabs" data-mock-tabs data-selected="{selected}" data-ag-width="fill">'
            f'<div class="mock-tablist" data-mock-tablist aria-label="{name}" hidden>{"".join(buttons)}</div>'
            f'{"".join(sections)}</div>')


def saved_run_form(fields: dict, csrf: str, *, viewing: bool = False) -> str:
    """Expose an explicit saved-revision run independent of the draft form fields."""
    if fields["revision"] == "0":
        return ""
    return f"""<form id="mock-saved-run" action="/tests" method="post" hidden>
<input type="hidden" name="csrf" value="{escape(csrf)}">
<input type="hidden" name="id" value="{escape(fields["id"])}">
<input type="hidden" name="revision" value="{escape(fields["revision"])}">
{hidden("mode", "view") if viewing else ""}
</form>"""


def saved_request(saved: dict, csrf: str) -> str:
    """Display the exact saved request, mock responses, checks and local hooks."""
    scenario = saved["scenario"]
    panes = (
        ("arguments", label("Аргументы", "Arguments"), json_view(scenario["arguments"])),
        ("fixtures", label("Фикстуры", "Fixtures"), json_view(scenario["fixtures"])),
        ("checks", label("Проверки", "Checks"), json_view(scenario["checks"])),
        ("options", label("Схемы", "Schemas"), json_view({
            key: scenario[key] for key in ("input_schema", "output_schema", "denied")
        })),
        ("hooks", label("Хуки", "Hooks"), json_view({"before": scenario["before"], "after": scenario["after"]})),
    )
    run_label = label("Запустить сохранённую версию", "Run saved revision")
    fields = draft_fields(scenario, saved["revision"])
    request_tabs = tab_set("mock-request", label("Сохранённый запрос", "Saved request"), panes, "arguments")
    return f"""<section class="mock-panel" id="scenario-editor" aria-labelledby="mock-editor-title" data-ag-width="fill">
<header class="mock-panel-heading"><h2 id="mock-editor-title">{label("Запрос", "Request")}</h2>
<span class="mock-icon-slot" data-hint="{run_label}"><button class="mock-icon mock-primary" type="submit"
 form="mock-saved-run" name="action" value="run" aria-label="{run_label}">{icon("play")}</button></span></header>
<div class="mock-panel-body mock-request-meta"><strong>{escape(scenario["title"])}</strong><code>{escape(scenario["tool"])}</code>
<small>{escape(scenario["id"])} · {label("Ревизия", "Revision")} {saved["revision"]}</small></div>
{request_tabs}{saved_run_form(fields, csrf, viewing=True)}</section>"""


def fixture_editor(fields: dict, notice: FormNotice | None) -> str:
    """Group response variants with the arguments used to match them."""
    outcomes = (("result", label("Успех", "Success")), ("tool_error", label("Ошибка инструмента", "Tool error")),
                ("timeout", label("Тайм-аут", "Timeout")))
    outcome = control("outcome", label("Ответ мока", "Mock outcome"), fields, notice, options=outcomes)
    structured = control("structured_content", "structuredContent", fields, notice, rows=5)
    fixture_text = control("text", label("Текстовый блок ответа", "Response text block"), fields, notice, rows=2)
    fixture_id = control("fixture_id", label("ID ответа мока", "Mock response ID"), fields, notice)
    modes = (("exact", label("Точные аргументы сценария", "Exact scenario arguments")),
             ("custom", label("Правило JSON", "JSON rule")))
    mode = control("follow_arguments", label("Выбор ответа", "Response matching"), fields, notice, options=modes)
    match = control("match", label("Правило JSON для первого ответа", "JSON rule for the first response"),
                    fields, notice, rows=4)
    extra = control("additional_fixtures", label("Дополнительные ответы JSON", "Additional mock responses in JSON"),
                    fields, notice, rows=4)
    return f'<div class="mock-fields" data-ag-width="fill">{outcome}{structured}{fixture_text}{fixture_id}{mode}{match}{extra}</div>'


def schema_editor(fields: dict, notice: FormNotice | None) -> str:
    """Group input and output validation with the tool policy."""
    denied = control("denied", label("Политика инструмента", "Tool policy"), fields, notice,
                     options=(("false", label("Разрешить", "Allow")), ("true", label("Запретить", "Deny"))))
    input_schema = control("input_schema", "inputSchema", fields, notice, rows=4)
    output_schema = control("output_schema", "outputSchema", fields, notice, rows=4)
    return f'<div class="mock-fields" data-ag-width="fill">{denied}{input_schema}{output_schema}</div>'


def hook_editor(fields: dict, notice: FormNotice | None) -> str:
    """Keep before and after hooks together without nesting another editor form."""
    before = control("before", label("Хук до запроса", "Before-request hook"), fields, notice, rows=4)
    after = control("after", label("Хук после ответа", "After-response hook"), fields, notice, rows=4)
    return f'<div class="mock-fields" data-ag-width="fill">{before}{after}</div>'


def check_markup(check: dict) -> str:
    """Show a check’s path and the values available to the browser."""
    title = label("Пройдена", "Passed") if check["passed"] else label("Не пройдена", "Failed")
    status = "ready" if check["passed"] else "error"
    code = escape(check["op"])
    if check.get("path"):
        code += " " + escape(check["path"])
    values = []
    for key, caption in (("expected", label("Ожидалось", "Expected")), ("actual", label("Получено", "Actual"))):
        if key in check:
            values.append(f'<div data-ag-width="content" data-ag-width-min-em="3.5">'
                          f'<dt>{caption}</dt><dd><code>{escape(json_text(check[key]))}</code></dd></div>')
    detail = f'<dl class="diagnostic-fields">{"".join(values)}</dl>' if values else ""
    reasons = {
        "redacted": label("Значение скрыто.", "Value redacted."),
        "value_limit": label("Значение превышает лимит отчёта.", "Value exceeds the report size limit."),
        "path_missing": label("Путь отсутствует в ответе.", "Path missing in the response."),
        "structured_result_required": label("Требуется структурированный ответ.", "A structured response is required."),
        "schema_mismatch": label("Ответ не соответствует схеме.", "The response does not match the schema."),
    }
    code_reason = check.get("reason")
    reason = f'<p class="muted">{escape(reasons.get(code_reason, code_reason))}</p>' if code_reason else ""
    return f'<span class="status-badge status-{status}">{title}</span> <code>{code}</code>{detail}{reason}'


def trace_markup(run: dict) -> str:
    """Explain the mock selection and gateway stages before showing raw trace data."""
    trace = run["trace"]
    stages = (
        ("before_hook", label("Хук до запроса", "Before-request hook")),
        ("arguments_changed", label("Аргументы изменились", "Arguments changed")),
        ("dispatched", label("Передача в мок", "Dispatch to mock")),
        ("output_schema", label("Проверка схемы ответа", "Response schema validation")),
        ("after_hook", label("Хук после ответа", "After-response hook")),
    )
    states = {
        "skipped": label("Пропущен", "Skipped"),
        "applied": label("Применён", "Applied"),
        "failed": label("Ошибка", "Failed"),
        "passed": label("Пройдена", "Passed"),
        True: label("Да", "Yes"),
        False: label("Нет", "No"),
    }
    steps = []
    for key, caption in stages:
        if key in trace:
            steps.append(f'<li><strong>{caption}</strong> · {states.get(trace[key], "—")}</li>')
    fixture = run["fixture_id"]
    code = run["error_code"]
    if fixture:
        selected = escape(fixture)
    elif code == "mock_no_match":
        selected = label("Нет совпадения", "No match")
    elif code == "mock_ambiguous_match":
        selected = label("Несколько совпадений", "Multiple matches")
    elif code == "policy_denied":
        selected = label("Вызов запрещён политикой", "Call blocked by policy")
    else:
        selected = label("Ответ не выбран", "No response selected")
    error = f'<p><code>{escape(code)}</code></p>' if code else ""
    return f"""<p>{label("Выбранный ответ", "Selected response")} · <code>{selected}</code></p>
<p>{label("Вызовы мока", "Mock calls")} · {run["upstream_calls"]}</p>{error}
{match_markup(trace.get("fixture_rules", []))}
<ul class="tools">{"".join(steps)}</ul>
<details><summary>{label("Данные трассы", "Trace data")}</summary>{json_view(trace)}</details>"""


def match_markup(rules: list[dict]) -> str:
    """Show how the mock evaluated each fixture against the dispatched arguments."""
    if not rules:
        return ""
    rows = []
    for rule in rules:
        status = label("Совпало", "Matched") if rule["matched"] else label("Не совпало", "Did not match")
        criterion = escape(rule["op"])
        if rule.get("path"):
            criterion += " " + escape(rule["path"])
        rows.append(f'<li><code>{escape(rule["id"])}</code> · <code>{criterion}</code> · {status}</li>')
    title = label("Условия выбора ответа", "Response matching rules")
    return f'<h3>{title}</h3><ul class="tools">{"".join(rows)}</ul>'


def report_panel(run: dict, *, editable: bool = False) -> str:
    """Show one retained result, with failed checks selected when the call is not accepted."""
    passed = run["passed"]
    outcome = label("Тест пройден", "Test passed") if passed else label("Тест не пройден", "Test failed")
    badges = "ready" if passed else "error"
    failed = next((check for check in run["checks"] if not check["passed"]), None)
    first_failure = ""
    if failed is not None:
        first_failure = (f'<section class="mock-failure" aria-labelledby="mock-first-failure" data-ag-width="fill">'
                         f'<h4 id="mock-first-failure">{label("Первая ошибка", "First failed check")}</h4>'
                         f'{check_markup(failed)}</section>')
    checks = "".join(f"<li>{check_markup(check)}</li>" for check in run["checks"] if check is not failed)
    remaining_checks = f'<ul class="tools">{checks}</ul>' if checks else ""
    response = json_view(run["response"])
    report_tabs = tab_set("mock-result", label("Просмотр результата", "Result view"), (
        ("response", label("Ответ", "Response"), response),
        ("checks", label("Проверки", "Checks"), first_failure + remaining_checks),
        ("trace", label("Трасса", "Trace"), trace_markup(run)),
    ), "response" if passed else "checks")
    warnings = ""
    if run["truncated"]:
        warnings = f'<p role="status">{label("Часть данных скрыта или усечена.", "Some data was hidden or truncated.")}</p>'
    location = run_url(run["run_id"])
    edit_link = "" if editable else (
        icon_link(scenario_url(run["scenario_id"], run["run_id"]), "edit", label("Открыть сценарий", "Open scenario"))
    )
    permalink = icon_link(location, "links", label("Ссылка на прогон", "Run link"))
    download = icon_link(location + "/json", "download", label("Скачать JSON", "Download JSON"))
    focus = "" if editable else " autofocus"
    return f"""<section id="run-summary" class="mock-panel" aria-labelledby="mock-run-title" data-ag-width="fill">
<header class="mock-panel-heading">
<h2 id="mock-run-title" tabindex="-1"{focus}>{label("Результат", "Result")}</h2>
<span class="status-badge status-{badges}">{outcome}</span></header>
<div class="mock-result-meta">
<p><span class="mock-kicker">{label("Мок", "Mock")}</span>
<code>{escape(run["profile"])}</code> · {label("Ревизия", "Revision")} {run["revision"]} ·
{run["duration_ms"]:.1f} {label("мс", "ms")}</p>
<nav class="mock-actions" aria-label="{label("Навигация по отчёту", "Report navigation")}">
{edit_link}{permalink}{download}</nav></div>{warnings}{report_tabs}</section>"""


def report_request(run: dict) -> str:
    """Show what was submitted using the run's masked immutable scenario snapshot."""
    scenario = run.get("scenario") or {}
    identity = escape(run["scenario_id"])
    title = escape(scenario.get("title", run["scenario_id"]))
    tool = f'<p><code>{escape(scenario["tool"])}</code></p>' if scenario.get("tool") else ""
    arguments = (json_view(scenario["arguments"]) if "arguments" in scenario else
                 f'<p class="muted">{label("Аргументы скрыты в отчёте.", "Arguments are unavailable in the report.")}</p>')
    return f"""<section class="mock-panel" aria-labelledby="mock-snapshot-title" data-ag-width="fill">
<header class="mock-panel-heading"><h2 id="mock-snapshot-title">{label("Запрос", "Request")}</h2></header>
<div class="mock-panel-body">
<p class="mock-snapshot-name">{title}</p>{tool}
<p class="muted">{identity} · {label("Ревизия", "Revision")} {run["revision"]}</p>
<h3>{label("Аргументы JSON", "JSON arguments")}</h3>{arguments}
<details><summary>{label("Сценарий на момент прогона", "Scenario at run time")}</summary>
{json_view(scenario)}</details></div></section>"""


def empty_result() -> str:
    """Give the result pane a clear initial state before the first run."""
    return f"""<section class="mock-panel" aria-labelledby="mock-empty-title" data-ag-width="fill">
<header class="mock-panel-heading"><h2 id="mock-empty-title">{label("Результат", "Result")}</h2></header>
<div class="mock-empty"><h3>{label("Прогон не выбран", "No run selected")}</h3>
<p>{label("Запустите сценарий, чтобы увидеть ответ, проверки и трассу.",
          "Run the scenario to see its response, checks and trace.")}</p></div></section>"""


def workspace(library: str, request: str, result: str, feedback: str = "") -> str:
    """Compose the library and the request/result panels in their reading order."""
    return (f'<div class="mock-workspace" data-ag-width="fill">'
            f'<div class="mock-slot" data-ag-width="content" data-ag-width-min-em="7">{library}</div>'
            f'<div class="mock-slot">{feedback}<div class="mock-panes" data-ag-width="fill">'
            f'<div class="mock-slot">{request}</div><div class="mock-slot">{result}</div></div></div></div>')


class MockTestPage:
    """Keep test execution and validation on the shared management service."""

    def __init__(self, service, origin: str, csrf: str):
        """Attach browser forms to one authenticated gateway profile."""
        self.service = service
        self.origin = origin
        self.csrf = csrf

    def page(self, content: str, status: int = 200, *, report: bool = False) -> HTMLResponse:
        """Wrap test content in the current profile’s shared navigation."""
        title = label("Отчёт о мок-тесте", "Mock test report") if report else label("Мок-тесты", "Mock tests")
        html = shell("/tests", self.service.router.profile.endpoint_key, title, content)
        return HTMLResponse(html, status_code=status, headers=LIVE_SETTINGS_HTML)

    async def render(self, fields: dict, notice: FormNotice | None = None, status: int = 200,
                     *, run: dict | None = None) -> HTMLResponse:
        """Keep the draft beside its selected prior result and retain failed form input."""
        data = await self.service.list()
        identity = fields["id"] if fields["revision"] != "0" else ""
        library = render_catalog(data, identity, True, self.csrf, run["run_id"] if run else "",
                                 profile=self.service.router.profile.endpoint_key)
        feedback = ""
        if notice:
            focus = "" if notice.field else ' tabindex="-1" autofocus'
            feedback = f'<p id="mock-feedback" class="module-message" role="alert"{focus}>{escape(notice.message)}</p>'
        result = report_panel(run, editable=True) if run is not None else empty_result()
        draft = editor(fields, self.csrf, notice, data["groups"],
                       reviewing_failure=run is not None and not run["passed"])
        body = workspace(library, draft, result, feedback)
        return self.page(body, status)

    async def overview(self, request):
        """Open an existing scenario or prepare an example from the catalogue schema."""
        identity = request.query_params.get("edit", "")[:32]
        tool = request.query_params.get("tool", "")[:128]
        mode = request.query_params.get("mode")
        if identity:
            try:
                saved = self.service.scenario(identity)
            except MockTestError:
                notice = FormNotice(label("Сценарий не найден.", "Scenario not found."))
                if mode == "view":
                    return await self.view_request(notice=notice, status=404)
                return await self.render(draft_fields(example_scenario(), 0), notice, 404)
            run_id = request.query_params.get("run", "")
            run = await self.previous_result(identity, run_id)
            if mode == "view":
                return await self.view_request(saved, run)
            return await self.render(draft_fields(saved["scenario"], saved["revision"]), run=run)
        if not tool:
            if mode != "edit":
                return await self.view_request()
            fields = draft_fields(example_scenario(), 0)
            fields["group_id"] = request.query_params.get("group", "")[:32]
            if "group" in request.query_params:
                fields.update(id="", title="")
            return await self.render(fields)
        seed = self.service.seed(tool)
        if seed is None:
            notice = FormNotice(label("Инструмент отсутствует в текущем каталоге.",
                                      "The tool is unavailable in the current catalog."))
            return await self.render(draft_fields(example_scenario(), 0), notice, 404)
        return await self.render(draft_fields(seed, 0))

    async def view_request(self, saved: dict | None = None, run: dict | None = None, *,
                           notice: FormNotice | None = None, status: int = 200) -> HTMLResponse:
        """Open the catalog and saved request without draft controls."""
        identity = saved["scenario"]["id"] if saved else ""
        library = render_catalog(await self.service.list(), identity, False, self.csrf,
                                 run["run_id"] if run else "", profile=self.service.router.profile.endpoint_key)
        if saved:
            detail = saved_request(saved, self.csrf)
        else:
            detail = (f'<section class="mock-panel mock-empty"><h2>{label("Запрос", "Request")}</h2>'
                      f'<p>{label("Выберите запрос из каталога.", "Select a request from the catalog.")}</p>'
                      f'{icon_link(new_request_url(), "plus", label("Новый запрос", "New request"))}</section>')
        result = report_panel(run) if run is not None else empty_result()
        feedback = (f'<p class="module-message" role="alert">{escape(notice.message)}</p>' if notice else "")
        return self.page(workspace(library, detail, result, feedback), status)

    async def previous_result(self, identity: str, run_id: str) -> dict | None:
        """Attach a retained report only to the scenario it actually belongs to."""
        if len(run_id) != 32:
            return None
        try:
            run = await self.service.get_run(GetRun(run_id=run_id))
        except (MockTestError, ValidationError, OSError):
            return None
        return run if run["scenario_id"] == identity else None

    async def submit(self, request):
        """Apply only same-origin, CSRF-bound actions to the current scenario revision."""
        fields = await self.authorized_form(request, limit=4 * MAX_DATA, max_fields=21)
        if not isinstance(fields, dict):
            return fields
        action = fields.get("action", "")
        if action not in {"save", "save-run", "run"}:
            return PlainTextResponse(label("Неизвестное действие.", "Unknown action."), status_code=400, headers=SECURE_HTML)
        return await self.perform(action, fields)

    async def authorized_form(self, request, *, limit: int, max_fields: int):
        """Apply the same origin, size and token checks to every catalog write."""
        if request.headers.get("origin") != self.origin:
            return self.forbidden()
        try:
            fields = await read_form(request, limit=limit, max_fields=max_fields)
        except ValueError:
            return PlainTextResponse(label("Некорректная форма.", "Invalid form."),
                                     status_code=400, headers=SECURE_HTML)
        if not compare_digest(fields.pop("csrf", "").encode(), self.csrf.encode()):
            return self.forbidden()
        return fields

    async def save_group(self, request):
        """Create or rename a collection through the management contract."""
        return await self.catalog_action(request, moving=False)

    async def move_request(self, request):
        """Move a request through the same contract used by agents."""
        return await self.catalog_action(request, moving=True)

    async def catalog_action(self, request, *, moving: bool):
        """Accept a native form or return changed placement to the drag client."""
        fields = await self.authorized_form(request, limit=4096, max_fields=5)
        if not isinstance(fields, dict):
            return fields
        wants_json = moving and request.headers.get("accept") == "application/json"
        try:
            revision = form_revision(fields.get("revision", ""))
            if moving:
                result = await self.service.move(MoveScenario(id=fields.get("id"), group_id=fields.get("group_id") or None,
                                                              expected_placement_revision=revision))
            else:
                result = await self.service.save_group(SaveGroup(id=fields.get("id") or uuid4().hex,
                                                                 title=fields.get("title"), expected_revision=revision))
        except (ValidationError, FormValueError):
            return self.catalog_error(fields, wants_json, "invalid_form", 400)
        except MockTestError as error:
            status = {"revision_conflict": 409, "placement_conflict": 409,
                      "scenario_not_found": 404, "group_not_found": 404}.get(error.code, 400)
            return self.catalog_error(fields, wants_json, error.code, status)
        except OSError:
            return self.catalog_error(fields, wants_json, "store_unavailable", 503)
        if wants_json:
            return JSONResponse(result, headers=SECURE_HTML)
        return RedirectResponse(scenario_url(fields.get("current", "")[:32]), status_code=303, headers=SECURE_HTML)

    def catalog_error(self, fields: dict, wants_json: bool, code: str, status: int):
        """Return a safe error or a direct route back to the catalog."""
        if wants_json:
            return JSONResponse({"error": code}, status_code=status, headers=SECURE_HTML)
        message = self.service_notice(code).message
        back = escape(scenario_url(fields.get("current", "")[:32]))
        text = f'<p role="alert">{escape(message)}</p><a href="{back}">{label("К каталогу", "Back to catalog")}</a>'
        return self.page(text, status)

    @staticmethod
    def forbidden() -> PlainTextResponse:
        """Reject cross-origin submissions and stale or missing browser tokens."""
        return PlainTextResponse(label("Форма не авторизована.", "Form authorization failed."),
                                 status_code=403, headers=SECURE_HTML)

    async def perform(self, action: str, fields: dict):
        """Save the submitted revision or execute it, then open the resulting resource."""
        identity = fields.get("id", "")
        revision = fields.get("revision", "")
        try:
            version = form_revision(revision)
            if action != "run":
                command = editor_request(fields, version)
                saved = await self.service.save(command)
                identity = command.scenario.id
                version = saved["revision"]
                fields["revision"] = str(version)
            if action == "save":
                return RedirectResponse(scenario_url(identity), status_code=303, headers=SECURE_HTML)
            result = await self.service.run(RunScenario(id=identity, expected_revision=version))
            run_id = result["run_id"]
            location = scenario_url(identity, run_id) if action == "save-run" else run_url(run_id)
            if action == "run" and fields.get("mode") == "view":
                location = scenario_url(identity, run_id, editing=False)
            return RedirectResponse(location, status_code=303, headers=SECURE_HTML)
        except FormValueError as error:
            notice = FormNotice(label("Проверьте значение поля.", "Check the field value."), error.field)
        except ValidationError as error:
            notice = self.validation_notice(error)
        except MockTestError as error:
            notice = self.service_notice(error.code)
            status = {"revision_conflict": 409, "scenario_not_found": 404,
                      "group_not_found": 404, "run_busy": 503}.get(error.code, 400)
            return await self.recover(action, fields, notice, status)
        except OSError:
            notice = FormNotice(label("Не удалось прочитать или сохранить тест.", "Could not read or save the test."))
            return await self.recover(action, fields, notice, 503)
        return await self.recover(action, fields, notice, 400)

    @staticmethod
    def validation_notice(error: ValidationError) -> FormNotice:
        """Locate the first visible field without echoing submitted values."""
        issue = error.errors(include_input=False)[0]["loc"]
        field = issue[1] if len(issue) > 1 and issue[0] == "scenario" else ""
        if field == "fixtures":
            field = issue[3] if len(issue) > 3 and issue[2] == 0 else "additional_fixtures"
            if field == "id":
                field = "fixture_id"
        return FormNotice(label("Проверьте определение сценария.", "Check the scenario definition."), field)

    @staticmethod
    def service_notice(code: str) -> FormNotice:
        """Describe safe recovery actions without propagating arbitrary error text."""
        if code == "revision_conflict":
            return FormNotice(label("Ревизия изменилась. Скопируйте черновик и откройте сценарий заново.",
                                    "The revision changed. Copy your draft and reopen the scenario."))
        if code == "scenario_not_found":
            return FormNotice(label("Сценарий не найден.", "Scenario not found."))
        if code == "run_busy":
            return FormNotice(label("Достигнут лимит одновременных прогонов. Повторите запуск позже.",
                                    "The concurrent run limit was reached. Try again shortly."))
        problems = {
            "invalid_form": FormNotice(label("Проверьте значение поля.", "Check the field value.")),
            "placement_conflict": FormNotice(label("Запрос уже перемещён. Обновите каталог.",
                                                    "The request moved. Refresh the catalog.")),
            "group_not_found": FormNotice(label("Коллекция не найдена.", "Collection not found.")),
            "group_capacity": FormNotice(label("Достигнут лимит коллекций.", "The collection limit was reached.")),
            "store_unavailable": FormNotice(label("Не удалось сохранить изменения.", "Could not save changes.")),
            "fixture_required": FormNotice(label("Добавьте ответ мока или проверку отсутствия совпадений.",
                                                 "Add a fixture or an explicit no-match check."), "fixture_id"),
            "check_required": FormNotice(label("Добавьте хотя бы одну проверку.", "Add at least one check."), "checks"),
            "schema_required": FormNotice(label("Для проверки укажите схему ответа.",
                                                "Provide an output schema for the check."), "output_schema"),
            "duplicate_fixture": FormNotice(label("Укажите разные ID ответов мока.",
                                                   "Use distinct fixture IDs."), "additional_fixtures"),
            "mock_ambiguous_match": FormNotice(label("Аргументы подходят нескольким ответам.",
                                                       "The arguments match multiple fixtures."), "additional_fixtures"),
            "non_local_hook": FormNotice(label("Хук может выполнять только локальные операции.",
                                                 "A hook can perform local operations only."), "before"),
            "input_schema": FormNotice(label("Проверьте схему входа.", "Check the input schema."), "input_schema"),
            "output_schema": FormNotice(label("Проверьте схему ответа.", "Check the output schema."), "output_schema"),
        }
        if code in problems:
            return problems[code]
        return FormNotice(f'{label("Не удалось выполнить операцию", "Test operation failed")} ({code}).')

    async def recover(self, action: str, fields: dict, notice: FormNotice, status: int) -> HTMLResponse:
        """Keep the submitted draft or reload the scenario after a rejected run."""
        default = draft_fields(example_scenario(), 0)
        if action == "run":
            try:
                saved = self.service.scenario(fields.get("id", ""))
            except MockTestError:
                if fields.get("mode") == "view":
                    return await self.view_request(notice=notice, status=status)
                return await self.render(default, notice, status)
            if fields.get("mode") == "view":
                return await self.view_request(saved, notice=notice, status=status)
            return await self.render(draft_fields(saved["scenario"], saved["revision"]), notice, status)
        retained = {key: fields[key][:MAX_DATA] for key in default if key in fields}
        revision = retained.get("revision", "")
        if not revision.isascii() or not revision.isdecimal() or len(revision) > 20:
            retained.pop("revision", None)
        return await self.render({**default, **retained}, notice, status)

    async def report(self, request):
        """Render the run’s saved revision and the same details returned to agents."""
        return await self.open_report(request, export=False)

    async def report_json(self, request):
        """Download the exact synthetic report as a non-cacheable JSON attachment."""
        return await self.open_report(request, export=True)

    async def open_report(self, request, *, export: bool):
        """Read one run from the profile store and choose its browser representation."""
        try:
            run = await self.service.get_run(GetRun(run_id=request.path_params["run_id"]))
        except (MockTestError, ValidationError):
            return PlainTextResponse(label("Прогон не найден.", "Run not found."),
                                     status_code=404, headers=SECURE_HTML)
        except OSError:
            return PlainTextResponse(label("Не удалось прочитать прогон.", "Could not read the run."),
                                     status_code=503, headers=SECURE_HTML)
        if export:
            headers = {**SECURE_HTML, "Content-Disposition": 'attachment; filename="mock-test-run.json"'}
            return JSONResponse(run, headers=headers)
        library = render_catalog(await self.service.list(), run["scenario_id"], False, self.csrf, run["run_id"],
                                 profile=self.service.router.profile.endpoint_key)
        return self.page(workspace(library, report_request(run), report_panel(run)), report=True)
