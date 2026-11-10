"""Render the read-only catalog and metrics as escaped, responsive HTML."""

from datetime import datetime
from html import escape
from urllib.parse import urlencode

from .connection_diagnostics import FAILURE_LABELS
from .ui_locale import language, language_switch, tr

CATALOG_PAGE_SIZE = 100

STYLE = """
:root { color-scheme: light dark; font: 100% system-ui, sans-serif; }
body { margin: 0; color: #172535; background: #f3f6fa; line-height: 1.5; }
main { max-width: 76rem; margin: auto; padding: clamp(.75rem, 3vw, 2.5rem); }
h1, h2, p { margin: 0; }
h1 { font-size: clamp(1.4rem, 4vw, 2.15rem); line-height: 1.18; }
h2 { margin-bottom: .75rem; font-size: 1.2rem; }
section { margin-top: 1.75rem; }
header, form { display: flex; align-items: end; justify-content: space-between;
  gap: .75rem; flex-wrap: wrap; }
.module-layout { display: grid; grid-template-columns: minmax(9rem, 12rem) minmax(0, 1fr); gap: 2rem; }
.module-nav { display: grid; align-content: start; gap: .2rem; min-width: 0; }
.module-nav a { padding: .5rem .65rem; overflow-wrap: anywhere; }
.module-nav [aria-current] { font-weight: 680; border-inline-start: 3px solid currentColor; }
.module-main, .module-form { min-width: 0; }
.module-form { display: grid; align-items: start; grid-template-columns: minmax(0, 1fr); gap: .45rem; margin-block: .75rem; }
.module-field { display: grid; gap: .25rem; min-width: 0; }
.module-main code { overflow-wrap: anywhere; }
.module-form button { justify-self: start; min-width: 0; overflow-wrap: anywhere; }
.module-form :is(textarea, select, input:not([type=hidden])) { box-sizing: border-box; width: 100%; min-width: 0; color: inherit;
  font: inherit; border: 1px solid #64768b; background: transparent; border-radius: .45rem; padding: .5rem; }
.module-form textarea { font-family: ui-monospace, monospace; resize: vertical; }
.module-form :is(textarea, select):focus-visible { outline: 3px solid #3174c2; }
.module-preview pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.module-message { margin-block: 1rem; padding: .6rem; border-inline-start: 3px solid currentColor; }
@media (max-width: 52rem) {
  .module-layout { grid-template-columns: minmax(0, 1fr); gap: 1rem; }
  .module-nav { display: flex; flex-wrap: wrap; gap: .3rem; }
}
header nav { display: flex; flex-wrap: wrap; gap: .75rem; }
.language-switch { margin-inline-start: auto; }
.language-switch [aria-current] { font-weight: 700; text-decoration: underline; }
a, input, select, button { color: inherit; }
a:focus-visible, input:focus-visible, select:focus-visible, button:focus-visible { outline: 3px solid #3174c2; }
a { padding: .4rem 0; }
form { justify-content: start; align-items: center; }
.credential-form { align-items: end; margin-top: .8rem; min-width: 0; }
.credential-form div { display: grid; grid-template-columns: minmax(0, 1fr);
  flex: 1 1 min(100%, 12rem); gap: .25rem; min-width: 0; max-width: 100%; }
.credential-form input { width: 100%; min-width: 0; }
.credential-form button { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
input, select, button { font: inherit; border: 1px solid #64768b; border-radius: .45rem;
  padding: .4rem .65rem; background: transparent; max-width: 100%; box-sizing: border-box; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit,minmax(min(100%,9rem),1fr));
  gap: .65rem; margin: .8rem 0; }
.metrics div, .tools li, .backends li { background: #fff; border: 1px solid #d2dce7;
  border-radius: .55rem; padding: .65rem .8rem; }
dt { font-size: .88rem; }
dd { margin: 0; font-weight: 680; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
ul { padding: 0; margin: .7rem 0; list-style: none; display: grid; gap: .45rem; }
li, h1, h2, header p { overflow-wrap: anywhere; }
.tools li p { white-space: pre-wrap; margin-top: .2rem; }
small, .muted { color: #445770; }
.backends { grid-template-columns: repeat(auto-fit,minmax(min(100%,17rem),1fr)); }
.backend-heading { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem .65rem; }
.backend-heading h3 { margin: 0; }
.diagnostic-fields { margin: .35rem 0 0; display: flex; flex-wrap: wrap; gap: .25rem 1rem; }
.diagnostic-fields > div { min-width: 0; }
.diagnostic-fields dd { font-weight: normal; }
.backend-actions { display: flex; flex-wrap: wrap; gap: .35rem .8rem; }
.status-badge { display: inline-block; max-width: 100%; box-sizing: border-box;
  padding: .1rem .5rem; border-radius: .35rem; font-size: .9em; font-weight: 650;
  overflow-wrap: anywhere; }
.status-ready { background: #def3e7; color: #16563a; }
.status-attention { background: #fff0c4; color: #624600; }
.status-error { background: #fbe4e5; color: #7d2030; }
.status-pending { background: #e2edf8; color: #284d72; }
details { margin-top: .65rem; }
summary { cursor: pointer; overflow-wrap: anywhere; }
@media (prefers-color-scheme: dark) {
  body { color: #e7eef9; background: #151e2a; }
  .metrics div, .tools li, .backends li { background: #202d3c; border-color: #52667e; }
  small, .muted { color: #bacbe1; }
  .status-ready { background: #214936; color: #caf4dc; }
  .status-attention { background: #59451e; color: #fff0c4; }
  .status-error { background: #572734; color: #ffe2e7; }
  .status-pending { background: #294767; color: #e2edf8; }
}
"""

_ENVIRONMENTS = {"test": "Тестовая среда", "production": "Рабочая среда"}

_METRICS = {
    "received": "Запросы",
    "upstream": "Вызовы MCP",
    "merged": "Объединено",
    "denied": "Заблокировано",
    "errors": "Ошибки",
    "latency_mean_ms": "Средняя задержка, мс",
    "latency_max_ms": "Макс. задержка, мс",
    "input_tokens_estimate": "Вход, токены ≈",
    "output_tokens_estimate": "Выход, токены ≈",
    "upstream_output_bytes_avoided_estimate": "Сэкономлено, байты ответа ≈",
}


def display(value) -> str:
    """Render missing observations separately from an observed zero."""
    return "—" if value is None else escape(str(value))


def tool_row(tool: dict, metrics: dict) -> str:
    """Show one tool policy, its upstream description and observed counters."""
    name = tool["name"]
    counts = metrics.get(name, {})
    policy = tr("Запрещён" if tool["denied"] else "Доступен")
    if tool["singleflight"]:
        policy += " · singleflight"
    return (
        f"<li><strong>{escape(name)}</strong> <small>{policy}</small>"
        f"<p>{escape(tool['description'])}</p>"
        f"<small>{tr("Запросы")} {display(counts.get('received', 0))} · "
        f"upstream {display(counts.get('upstream', 0))} · "
        f"{tr('средняя задержка')} {display(counts.get('latency_mean_ms'))} {tr('мс')}</small></li>"
    )


def connection_status(state: str, authenticated: bool) -> str:
    """Show the same connection state and accessible label on both dashboard pages."""
    if state == "ready":
        title, tone = ("Подключён без входа" if not authenticated else "Подключён"), "ready"
        marker = '<span aria-hidden="true">✓</span> '
    else:
        title, tone = FAILURE_LABELS.get(state, ("Подключение недоступно", "error"))
        marker = ""
    return f'<span class="status-badge status-{tone}">{marker}{escape(tr(title))}</span>'


def last_call_line(timestamp: str | None) -> str:
    """Render a UTC dispatch time or explain the empty current-process history."""
    value = tr("вызовов с запуска пока нет")
    if timestamp is not None:
        time = datetime.fromisoformat(timestamp)
        format_string = "%d.%m.%Y %H:%M:%S UTC" if language() == "ru" else "%Y-%m-%d %H:%M:%S UTC"
        label = time.strftime(format_string)
        value = f'<time datetime="{escape(timestamp, quote=True)}">{label}</time>'
    return f'<p class="muted">{tr("Последний вызов")}: {value}</p>'


def backend_card(backend: dict, can_manage: bool, tools_href: str | None, csrf: str | None,
                 *, reconnect_context: str = "", diagnostics: bool = False, notice: str = "") -> str:
    """Show confirmed transport readiness and relevant actions for one backend."""
    name = escape(backend["name"])
    state = backend["status"]
    auth = backend["auth"]
    transport = "Локальный процесс · stdio" if backend["transport"] == "stdio" else "Streamable HTTP"
    auth_label = {"basic": "HTTP Basic", "bearer": "Bearer", "none": "Без входа через шлюз"}[auth]
    actions = []
    if tools_href:
        actions.append(f'<a href="{escape(tools_href, quote=True)}">{tr("Инструменты")}</a>')
    if auth == "basic" and can_manage:
        action = "Переавторизоваться" if state == "ready" else "Настроить подключение"
        actions.append(f'<a href="/credentials?backend={name}">{tr(action)}</a>')
    if diagnostics:
        actions.append(f'<a href="/diagnostics?backend={name}">{tr("Диагностика")}</a>')
    if csrf is not None:
        context = escape(reconnect_context, quote=True)
        actions.append(f'<form method="post" action="/reconnect/{name}{context}#backend-{name}">'
                       f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
                       f'<button type="submit" aria-label="{tr("Переподключить")} {name}">{tr("Переподключить")}</button></form>')
    links = f'<div class="backend-actions">{"".join(actions)}</div>' if actions else ""
    feedback = f'<p role="status">{escape(notice)}</p>' if notice else ""
    return (
        f'<li id="backend-{name}"><div class="backend-heading"><h3>{name}</h3>'
        f'{connection_status(state, auth != "none")}</div>'
        f'<p class="muted">{tr(transport)} · {tr(auth_label)}</p>'
        f'<p>{tr("Доступных инструментов")} {display(backend["tools"])}</p>'
        f'{feedback}{last_call_line(backend.get("last_call_at"))}{links}</li>'
    )


def tool_groups(tools: list[dict], query: str, metrics: dict) -> tuple[dict[str, list[str]], int]:
    """Group matching descriptions by their configured backend namespace."""
    groups = {}
    term = query.casefold()
    count = 0
    for tool in tools:
        name = tool["name"]
        if term not in name.casefold() and term not in tool["description"].casefold():
            continue
        prefix = name.partition(".")[0]
        groups.setdefault(prefix, []).append(tool_row(tool, metrics))
        count += 1
    return groups, count


def catalog_url(query: str, backend: str | None, offset: int = 0) -> str:
    """Build a relative URL for browser filters and page navigation."""
    parameters = {}
    if query:
        parameters["q"] = query
    if backend:
        parameters["backend"] = backend
    if offset:
        parameters["offset"] = offset
    encoded = urlencode(parameters)
    return f"/?{encoded}" if encoded else "/"


def catalog_navigation(page: dict | None, query: str) -> str:
    """Render page links while keeping the selected backend and search term."""
    if page is None or page["total"] <= page["size"]:
        return ""
    offset, size, total = page["offset"], page["size"], page["total"]
    links = [f"<span>{offset + 1}–{min(total, offset + size)} {tr('из')} {total}</span>"]
    if offset:
        previous = escape(catalog_url(query, page["backend"], offset - size), quote=True)
        links.append(f'<a rel="prev" href="{previous}">{tr("Назад")}</a>')
    if offset + size < total:
        following = escape(catalog_url(query, page["backend"], offset + size), quote=True)
        links.append(f'<a rel="next" href="{following}">{tr("Далее")}</a>')
    return f'<nav aria-label="{tr("Страницы инструментов")}">{" ".join(links)}</nav>'


def render(status: dict, query: str, *, credentials: bool = False, management: bool = False,
           reconnect_csrf: str | None = None, notice: str = "", notice_backend: str | None = None,
           modules: bool = False, diagnostics: bool = False) -> str:
    """Render one dashboard snapshot without executing upstream text or links."""
    label = _ENVIRONMENTS.get(status.get("environment"))
    environment = f" · {tr(label)}" if label else ""
    controls = f'<a href="/credentials">{tr("Учётные данные MCP")}</a>' if credentials else ""
    if modules:
        controls += f'<a href="/modules">{tr("Модули")}</a>'
    if management:
        controls += f'<a href="/settings">{tr("Настройки")}</a>'
    if diagnostics:
        controls += f'<a href="/diagnostics">{tr("Диагностика")}</a>'
    metrics = status["metrics"]
    cards = "".join(
        f"<div><dt>{tr(label)}</dt><dd>{display(metrics['total'][key])}</dd></div>"
        for key, label in _METRICS.items()
    )
    page = status.get("page")
    groups, count = tool_groups(status["catalog"], "" if page else query, metrics["tools"])
    backend_filter = ""
    if page is not None:
        count = page["total"]
        if page["backend"]:
            backend = escape(page["backend"], quote=True)
            reset = escape(catalog_url(query, None), quote=True)
            backend_filter = (f'<input type="hidden" name="backend" value="{backend}">'
                              f'<span>{tr("Фильтр MCP")} <strong>{backend}</strong></span>'
                              f'<a href="{reset}">{tr("Все MCP")}</a>')
    reset_search = f'<a href="/">{tr("Сбросить фильтры")}</a>' if query else ""
    context = catalog_url(query, page["backend"], page["offset"]) if page is not None else "/"
    cards_html = []
    for item in status["connections"]:
        name = item["name"]
        href = None
        if page is None and name in groups:
            href = f"#tools-{name}"
        elif page is not None and (name in page["groups"] or (page["backend"] and item["tools"])):
            href = catalog_url(query, name) + "#catalog"
        cards_html.append(backend_card(item, credentials, href, reconnect_csrf,
                                       reconnect_context=context.removeprefix("/"), diagnostics=diagnostics,
                                       notice=notice if name == notice_backend else ""))
    backends = "".join(cards_html)
    catalog = "".join(
        f'<div><h3 id="tools-{escape(prefix)}">{escape(prefix)}</h3><ul class="tools">{"".join(rows)}</ul></div>'
        for prefix, rows in groups.items()
    ) or f'<ul class="tools"><li>{tr("Инструменты не найдены.")}</li></ul>'
    message = f'<p role="status">{escape(notice)}</p>' if notice and notice_backend is None else ""
    return f"""<!doctype html>
<html lang="{language()}">
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ag-mcp-gateway</title><link rel="stylesheet" href="/dashboard.css">
<main>
<header><div><h1>ag-mcp-gateway</h1><p>{tr("Профиль")} {escape(status['profile'])}{environment}</p></div>
<nav aria-label="{tr("Действия обзора")}"><a href="/">{tr("Обновить обзор")}</a>{controls}</nav>{language_switch()}</header>
<section aria-labelledby="backends"><h2 id="backends">{tr("Каталог MCP-серверов")}</h2>
{message}<ul class="backends">{backends}</ul></section>
<section aria-labelledby="metrics"><h2 id="metrics">{tr("Вызовы с момента запуска")}</h2>
<dl class="metrics">{cards}</dl>
<p class="muted">{tr("Оценка токенов по размеру JSON. Ошибки записи журнала")} {metrics["journal_errors"]}.</p>
</section>
<section aria-labelledby="catalog"><h2 id="catalog">{tr("Инструменты")} · {count}</h2>
<form action="/" method="get">
<label for="q">{tr("Имя или описание")}</label>
<input id="q" name="q" maxlength="200" value="{escape(query, quote=True)}">
<button type="submit">{tr("Найти")}</button>{backend_filter}{reset_search}
</form>
{catalog_navigation(page, query)}
{catalog}</section>
</main></html>"""
