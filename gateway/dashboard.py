"""Render the read-only catalog and metrics as escaped, responsive HTML."""

import json
from datetime import datetime
from html import escape
from urllib.parse import urlencode

from .connection_diagnostics import FAILURE_LABELS
from .mcp_server import ToolSort
from .profile import backend_client_name
from .ui_locale import language, mock_tests_available, tr
from .ui_shell import icon, shell, utility_link

CATALOG_PAGE_SIZE = 100

STYLE = """
:root { color-scheme: light dark; font: 100% system-ui, sans-serif;
  --ag-surface: #fafafc; --ag-surface-alt: #f1f2f5; --ag-panel: #fff; --ag-text: #26272d;
  --ag-muted: #626570; --ag-border: #d5d7de; --ag-accent: #006b68;
  --control-border: #757884; --workspace-focus: #006b68;
  --ag-on-accent: #fff; --ag-danger: #a02726;
  --ag-font: system-ui, sans-serif; --ag-text-size: .875rem;
  --ag-space-2: 8px; --ag-space-3: 12px; --ag-space-4: 16px;
  --ag-radius-control: 7px; --ag-control-height: 32px; }
@media (pointer: coarse) { :root { --ag-control-height: 44px; } }
body { margin: 0; color: var(--ag-text); background: var(--ag-surface); line-height: 1.5; }
main { max-width: 76rem; margin: auto; padding: clamp(.875rem, 2vw, 1.75rem) clamp(.75rem, 2.5vw, 2rem); }
h1, h2, p { margin: 0; }
h1 { font-size: clamp(1.4rem, 3vw, 1.8rem); line-height: 1.18; }
h2 { margin-bottom: .75rem; font-size: 1.2rem; }
section { margin-top: 1.25rem; }
.workspace > main > section:first-of-type { margin-top: .875rem; }
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
  font: inherit; border: 1px solid var(--control-border); background: transparent; border-radius: .45rem; padding: .5rem; }
.module-form textarea { font-family: ui-monospace, monospace; resize: vertical; }
.module-form :is(textarea, select):focus-visible { outline: 3px solid var(--workspace-focus); }
.module-preview pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.module-message { margin-block: 1rem; padding: .6rem; border-inline-start: 3px solid currentColor; }
.policy-description { margin-block-start: .15rem; overflow-wrap: anywhere; }
@media (max-width: 52rem) {
  .module-layout { grid-template-columns: minmax(0, 1fr); gap: 1rem; }
  .module-nav { display: flex; flex-wrap: wrap; gap: .3rem; }
}
header nav { display: flex; flex-wrap: wrap; gap: .75rem; }
.language-switch { margin-inline-start: auto; }
.language-switch [aria-current] { font-weight: 700; text-decoration: underline; }
a, input, select, button { color: inherit; }
a:focus-visible, input:focus-visible, select:focus-visible, button:focus-visible { outline: 3px solid var(--workspace-focus); }
a { padding: .4rem 0; }
form { justify-content: start; align-items: center; }
.credential-form { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start;
  margin-top: .8rem; min-width: 0; }
.credential-form button { justify-self: start; }
.credential-form div { display: grid; grid-template-columns: minmax(0, 1fr);
  flex: 1 1 min(100%, 12rem); gap: .25rem; min-width: 0; max-width: 100%; }
.credential-form input { width: 100%; min-width: 0; }
.credential-form button { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
input, select, button { font: inherit; border: 1px solid var(--control-border); border-radius: .45rem;
  padding: .4rem .65rem; background: transparent; max-width: 100%; box-sizing: border-box; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit,minmax(min(100%,9rem),1fr));
  gap: .65rem; margin: .8rem 0; }
.metrics div, .tools li, .backends li { background: var(--ag-panel); border: 1px solid var(--ag-border);
  border-radius: .55rem; padding: .65rem .8rem; }
dt { font-size: .88rem; }
dd { margin: 0; font-weight: 680; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
ul { padding: 0; margin: .7rem 0; list-style: none; display: grid; gap: .45rem; }
li, h1, h2, header p { overflow-wrap: anywhere; }
.tools li p { white-space: pre-wrap; margin-top: .2rem; }
small, .muted { color: var(--ag-muted); }
.backends { grid-template-columns: repeat(auto-fit,minmax(min(100%,17rem),1fr)); }
.backend-heading { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem .65rem; }
.backend-heading h3 { margin: 0; }
.diagnostic-fields { margin: .35rem 0 0; display: flex; flex-wrap: wrap; gap: .25rem 1rem; }
.diagnostic-fields > div { min-width: min(100%, 7rem); }
.diagnostic-fields dd { font-weight: normal; }
.backend-actions { display: flex; flex-wrap: wrap; align-items: center; gap: .22rem; }
.workspace .gateway-utility {
  display: inline-grid; place-items: center; inline-size: var(--ag-control-height);
  min-block-size: var(--ag-control-height); box-sizing: border-box; padding: .3rem;
  border: 1px solid transparent; border-radius: .32rem; text-decoration: none; cursor: pointer;
}
.gateway-utility:hover { border-color: var(--ag-border); background: var(--ag-surface-alt); }
.gateway-utility svg { inline-size: 1.05rem; block-size: 1.05rem; }
.workspace .gateway-utility--submit { border-color: var(--control-border); }
.tool-card-heading { display: flex; align-items: center; flex-wrap: wrap; gap: .1rem .45rem; }
.tool-card-heading .gateway-utility { margin-inline-start: auto; }
.backend-actions + [data-copy-status]:empty { margin: 0; }
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
  :root { --ag-surface: #1b1c20; --ag-surface-alt: #222328; --ag-panel: #26272d; --ag-text: #e8e9ee;
    --ag-muted: #adb0bb; --ag-border: #444650; --ag-accent: #79c8be;
    --control-border: #747782; --workspace-focus: #91d9cf; --ag-on-accent: #102323; --ag-danger: #ff9c96; }
  .status-ready { background: #214936; color: #caf4dc; }
  .status-attention { background: #59451e; color: #fff0c4; }
  .status-error { background: #572734; color: #ffe2e7; }
  .status-pending { background: #294767; color: #e2edf8; }
}

/* The browser titlebar and navigation use the same column width. */
.workspace { display: grid; grid-template-columns: minmax(0, 1fr); min-height: 100vh;
  grid-template-areas: "bar" "sidebar" "main"; grid-template-rows: auto auto 1fr;
  --sidebar-width: 13.75rem;
  --workspace-surface: var(--ag-surface-alt); --workspace-border: var(--ag-border);
  --workspace-active: color-mix(in srgb, var(--ag-accent) 12%, var(--ag-panel)); }
.workspace[data-sidebar-layout=wide] { grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  grid-template-areas: "bar bar" "sidebar main"; grid-template-rows: auto 1fr; }
.workspace[data-sidebar-layout=wide][data-sidebar-collapsed=true] { --sidebar-width: 3.5rem; }
.workspace [hidden] { display: none !important; }
.workspace :is(a,button,summary,input,textarea,select):focus-visible {
  outline: 3px solid var(--workspace-focus); outline-offset: 2px;
}
.workspace-bar { grid-area: bar; display: flex; flex-wrap: nowrap; align-items: stretch;
  justify-content: normal; gap: 0; min-width: 0;
  background: var(--workspace-surface); border-bottom: 1px solid var(--workspace-border); }
.workspace-nav-header, .workspace-content-header { display: flex; align-items: center; padding: .4rem .6rem; }
.workspace-content-header { flex: 1; min-width: 0; gap: .5rem; }
.workspace[data-sidebar-layout=wide] .workspace-bar {
  display: grid; grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
}
.workspace[data-sidebar-layout=wide] .workspace-nav-header {
  justify-content: flex-end; border-inline-end: 1px solid var(--workspace-border);
}
.workspace[data-sidebar-layout=wide][data-sidebar-collapsed=true] .workspace-nav-header { justify-content: center; }
.workspace[data-native-host=true] { grid-template-areas: "sidebar" "main"; grid-template-rows: auto 1fr; }
.workspace[data-native-host=true][data-sidebar-layout=wide] {
  grid-template-areas: "sidebar main"; grid-template-rows: 1fr;
}
.workspace[data-native-host=true] .workspace-bar { display: none; }
/* AppKit displays the section title. Keep the document heading for heading navigation. */
.workspace[data-native-host=true] .workspace-title {
  position: absolute; inline-size: 1px; block-size: 1px; margin: -1px; overflow: hidden;
  clip-path: inset(50%); white-space: nowrap;
}
.workspace[data-native-host=true] > main > section:first-of-type { margin-top: 0; }
.workspace > main { grid-area: main; box-sizing: border-box; width: 100%; min-width: 0; margin: 0 auto; }
.workspace-sidebar { grid-area: sidebar; background: var(--workspace-surface);
  border-inline-end: 1px solid var(--workspace-border); min-width: 0; overflow-wrap: anywhere; }
.workspace-brand { display: flex; align-items: center; gap: .45rem; min-width: 0; padding: 0;
  font-weight: 760; text-decoration: none; }
.workspace-brand span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.workspace-brand svg { width: 1.5rem; height: 1.5rem; flex: none; }
/* Match the AG navigation rhythm with a fixed icon column and a quiet profile caption. */
.workspace-profile { margin: .375rem .625rem 0; padding: .875rem .75rem;
  border-block-start: 1px solid var(--ag-border); color: var(--ag-muted); font-size: .8125rem; }
.workspace-profile strong { display: block; color: var(--ag-text); font-weight: 600; }
.workspace-sidebar nav { display: flex; flex-direction: column; gap: .25rem; padding: 1rem .625rem; }
.workspace-sidebar nav a { display: flex; flex: none;
  align-items: center; gap: .625rem; min-block-size: 2.25rem; min-inline-size: 0;
  box-sizing: border-box; padding: .3125rem .75rem; border-radius: .5rem;
  font-size: .875rem; line-height: 1.4; font-weight: 500; text-decoration: none; }
.workspace-sidebar nav span { flex: 1; min-inline-size: 0; overflow-wrap: anywhere; }
.workspace-sidebar nav a:hover { background: color-mix(in srgb, var(--ag-text) 8%, var(--ag-surface-alt)); }
.workspace-sidebar nav a[aria-current="page"] {
  background: var(--workspace-active); color: var(--ag-accent); font-weight: 600;
}
.workspace-sidebar nav svg { flex: none; width: 1.15rem; height: 1.15rem; margin-inline: .1125rem; }
.workspace[data-sidebar-layout=wide][data-sidebar-collapsed=true] .workspace-sidebar nav {
  padding-inline: .375rem;
}
.workspace[data-sidebar-layout=wide][data-sidebar-collapsed=true] .workspace-sidebar nav a {
  justify-content: center; padding-inline: .25rem;
}
.workspace[data-sidebar-layout=wide][data-sidebar-collapsed=true] .workspace-profile { display: none; }
.workspace[data-sidebar-layout=wide][data-sidebar-collapsed=true] .workspace-sidebar nav span {
  position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap;
}
.workspace[data-sidebar-layout=compact] :is(.workspace-nav-header, .workspace-content-header) { padding-inline: .3rem; }
.workspace[data-sidebar-layout=compact] .workspace-content-header { gap: .25rem; }
.workspace[data-sidebar-layout=compact] .workspace-brand span { display: none; }
@media (pointer: fine) {
  .workspace-nav-header, .workspace-content-header { padding-block: .24rem; }
  .workspace-sidebar nav { gap: .16rem; padding-block: .6rem; }
  .workspace-sidebar nav a { min-block-size: 2rem; padding-block: .22rem; }
  .workspace-profile { padding-block: .58rem; }
  .workspace > main { padding-block-start: .95rem; }
}
.header-icon { display: inline-grid; place-items: center; flex: none; cursor: pointer; box-sizing: border-box;
  inline-size: 2.25rem; block-size: 2.25rem; min-inline-size: 36px; min-block-size: 36px;
  padding: 0; border: 0; border-radius: .4rem; background: transparent; color: inherit; }
.header-icon svg { width: 1.2rem; height: 1.2rem; }
.header-icon:hover, .header-icon:active, .header-icon[aria-expanded=true]:active { background: var(--workspace-active); }
.header-icon:disabled { opacity: .5; cursor: default; }
.language-switch { margin: 0 0 0 auto; position: relative; flex: none; }
.language-switch summary { list-style: none; }
.language-switch summary::-webkit-details-marker { display: none; }
.language-switch nav { position: absolute; z-index: 2; inset-inline-end: 0; top: 100%; display: grid;
  padding: .4rem .65rem; border: 1px solid var(--workspace-border); border-radius: .4rem;
  background: var(--workspace-surface); }
.server-search { margin-block: .5rem; align-items: end; }
.tool-keywords { margin-block-start: var(--ag-space-2); overflow-wrap: anywhere; }
.tool-keywords summary { cursor: pointer; padding-block: var(--ag-space-2); min-height: var(--ag-control-height); }
.tool-keywords summary:focus-visible { outline: 2px solid var(--workspace-focus); outline-offset: 2px; }
.catalog-count { font-size: .875rem; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.gateway-filter { display: grid; grid-template-columns: minmax(0, 1fr); margin-block: .5rem .75rem; min-width: 0; }
.gateway-filter :is(.ag-select, .ag-field) { display: grid; width: 100%; gap: var(--ag-space-2); min-width: 0; }
.gateway-tool-filters { display: grid; grid-template-columns: minmax(0, 24em) minmax(0, 13em) auto;
  align-items: end; gap: var(--ag-space-3); justify-content: start; }
.gateway-tool-filters :is(input, select, button) { min-height: max(var(--ag-control-height), 2.45em); }
.gateway-tool-filters input { width: 100%; box-sizing: border-box; }
@media (max-width: 720px) {
  .gateway-tool-filters { grid-template-columns: minmax(0, 1fr); justify-items: start; }
}
.gateway-filter label { font-weight: 600; font-size: var(--ag-text-size); overflow-wrap: anywhere; }
.gateway-control-line { display: flex; flex-wrap: wrap; align-items: center; gap: var(--ag-space-2); min-width: 0; }
.gateway-control-line > :is(input, select) {
  flex: 1 1 min(100%, 14em); width: auto; min-width: 0; max-width: 24em;
  color: var(--ag-text); background-color: var(--ag-surface-alt); border-color: var(--control-border);
}
.workspace :is(.gateway-control-line > :is(input, select, button), .profile-overview .ag-button) {
  min-height: max(var(--ag-control-height), 2.45em);
  font-size: var(--ag-text-size); line-height: 1.4;
}
.gateway-control-line > .ag-button, .profile-overview .ag-button {
  border-color: var(--control-border); max-width: 100%; white-space: normal; overflow-wrap: anywhere;
}
.gateway-control-line > :is(input, select, button) { padding-block: 5px; }
.gateway-control-line > :is(input, select):focus-visible,
.gateway-control-line > .ag-button:focus-visible, .profile-overview .ag-button:focus-visible {
  outline: 2px solid var(--workspace-focus); outline-offset: 2px;
}
.catalog-filters { display: flex; flex-wrap: wrap; align-items: center; gap: var(--ag-space-2); }
.profile-overview { max-inline-size: 40em; justify-items: start; }
.profile-overview h2 { margin: 0; font-size: 1rem; }
.profile-overview code { overflow-wrap: anywhere; }
.workspace:not([data-native-host=true]) .profile-native-copy,
.workspace[data-native-host=true] .profile-browser-copy { display: none; }
.credential-actions { margin-block: .5rem; gap: .4rem .65rem; }
.credential-actions button, .credential-unavailable a { overflow-wrap: anywhere; }
.credential-unavailable { display: grid; gap: .5rem; justify-items: start; }
.credential-unavailable .backend-actions { align-items: center; }
.credential-form { max-inline-size: 34rem; }
.backend-heading h2 { margin: 0; }
.metric-tables { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22em), 1fr));
  align-items: start; gap: var(--ag-space-3); margin-block: var(--ag-space-4); }
.metric-tables .ag-data-table__scroll { background: var(--ag-panel); }
.metric-tables caption { background: var(--ag-surface-alt); }
.metric-tables tbody th { font-weight: 500; color: var(--ag-muted); }
.metric-tables td[data-align="end"] { text-align: end; font-weight: 650; }
.metric-notes { display: grid; gap: 4px; }
.search-field { display: grid; grid-template-columns: minmax(0, 1fr); gap: .22rem; min-width: 0;
  flex: 1 1 min(100%, 15rem); max-width: 25rem; }
.search-field :is(input, select) { width: 100%; min-width: 0; }
.workspace form :is(input, select, textarea, button) { min-height: 2rem; }
.workspace .backend-actions form { margin: 0; }
.mcp-config { box-sizing: border-box; width: 100%; min-width: 0; margin-block: .5rem;
  border: 1px solid var(--workspace-border); border-radius: .45rem; padding: .5rem;
  background: var(--workspace-surface); color: inherit; font: .875em/1.5 ui-monospace, monospace;
  resize: vertical; }
.workspace form + p { margin-block-start: .75rem; }
.skip-link { position: absolute; inset: .5rem auto auto .5rem; transform: translateY(-200%); z-index: 3;
  padding: .55rem; background: var(--workspace-surface); }
.skip-link:focus { transform: none; }
.primary-action { display: inline-block; background: var(--ag-accent); color: var(--ag-surface); text-decoration: none;
  padding: .5rem .8rem; border-radius: .45rem; font-weight: 650; }
/* The AG rail owns its dock and preview. Only the narrow gutter belongs to the page grid. */
.tool-catalog[data-rail] { display: grid; grid-template-columns: auto minmax(0, 1fr);
  gap: .75rem; align-items: start; }
.tool-catalog-content, .tool-catalog [data-tool-results] { min-width: 0; }
.tool-catalog .ag-vt-feed-rail { --ag-surface: var(--ag-panel); }
@media (pointer: fine) {
  .tool-catalog { --ag-feed-rail-gutter-width: 10px; }
  .tool-catalog .ag-vt-feed-rail__dock { width: 12px; }
  .tool-catalog[data-rail] .ag-vt-feed-rail:is(:hover, :focus-within, :has([aria-expanded="true"])) .ag-vt-feed-rail__dock {
    width: 32px; padding-inline: 2px;
  }
  .tool-catalog .ag-vt-feed-rail .ag-vt-rail--compact .ag-vt-rail__tick { max-width: 14px; }
}
.tools li[data-tool-card] { overflow-wrap: anywhere; scroll-margin-block-start: 1rem; }
@media (max-width: 32rem) {
  .tool-catalog[data-rail] { gap: .5rem; }
}

"""

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
_METRIC_GROUPS = (
    ("Объём вызовов", ("received", "upstream", "merged")),
    ("Задержки", ("latency_mean_ms", "latency_max_ms")),
    ("Политика и ошибки", ("denied", "errors")),
    ("Трафик", ("input_tokens_estimate", "output_tokens_estimate", "upstream_output_bytes_avoided_estimate")),
)


def display(value) -> str:
    """Render missing observations separately from an observed zero."""
    return "—" if value is None else escape(str(value))


def tool_row(tool: dict, metrics: dict, *, show_keywords: bool = False) -> str:
    """Show one tool policy, its upstream description and observed counters."""
    name = tool["name"]
    counts = metrics.get(name, {})
    uses = counts.get("received", 0) - counts.get("denied", 0)
    policy = tr("Запрещён" if tool["denied"] else "Доступен")
    if tool["singleflight"]:
        policy += " · singleflight"
    keywords = ""
    if show_keywords and not tool["denied"]:
        keywords = (
            f'<details class="tool-keywords" data-keyword-tool="{escape(name, quote=True)}" '
            f'data-loading="{tr("Загрузка ключевых слов…")}" data-error="{tr("Не удалось загрузить ключевые слова.")}" '
            f'data-empty="{tr("Ключевые слова не найдены.")}">'
            f'<summary>{tr("Ключевые слова")}</summary><p role="status"></p></details>'
        )
    test_link = ""
    if mock_tests_available() and "." in name:
        href = "/tests?" + urlencode({"tool": name})
        test_link = utility_link(href, "tests", f'{tr("Открыть в тестах")} · {name}')
    return (
        f'<li data-tool-card><div class="tool-card-heading" data-ag-width="fill"><strong>{escape(name)}</strong> '
        f"<small>{policy}</small>{test_link}</div>"
        f"<p>{escape(tool['description'])}</p>"
        f"<small>{tr('Использований')} {display(uses)} · "
        f"upstream {display(counts.get('upstream', 0))} · "
        f"{tr('средняя задержка')} {display(counts.get('latency_mean_ms'))} {tr('мс')}</small>"
        f"{keywords}</li>"
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



def connection_copy(profile_key: str, backend: str, address: str, protected: bool) -> tuple[str, str]:
    """Build a card action and its selectable fallback from the gateway's public connection data."""
    entry = {"url": address}
    if protected:
        entry["headers"] = {"Authorization": "Bearer <gateway-profile-token>"}
    name = backend_client_name(profile_key, backend)
    settings = json.dumps({"mcpServers": {name: entry}}, indent=2)
    copy = tr("Скопировать настройки")
    copied = tr("Настройки скопированы. Подставьте токен профиля.") if protected else tr("Настройки скопированы.")
    manual = tr("Скопируйте выделенный JSON вручную. Подставьте токен профиля.") if protected else tr("Скопируйте выделенный JSON вручную.")
    caption = escape(f"{copy} · {backend}", quote=True)
    button = (f'<button type="button" class="gateway-utility" data-copy-mcp data-copied="{copied}" data-manual="{manual}" '
              f'aria-label="{caption}" title="{caption}">{icon("copy")}</button>')
    label = tr("Настройки подключения MCP в JSON")
    fallback = (f'<p role="status" data-copy-status></p><textarea class="mcp-config" data-mcp-config rows="8" '
                f'aria-label="{label}" readonly hidden>{escape(settings)}</textarea>')
    return button, fallback

def backend_card(backend: dict, can_manage: bool, tools_href: str | None, csrf: str | None,
                 *, profile_key: str, protected: bool = False, reconnect_context: str = "",
                 diagnostics: bool = False, notice: str = "") -> str:
    """Show confirmed transport readiness and relevant actions for one backend."""
    name = escape(backend["name"])
    state = backend["status"]
    auth = backend["auth"]
    transport = "Локальный процесс · stdio" if backend["transport"] == "stdio" else "Streamable HTTP"
    auth_label = {"basic": "HTTP Basic", "bearer": "Bearer", "none": "Без авторизации MCP"}[auth]
    actions = []
    if tools_href:
        actions.append(utility_link(tools_href, "tools", f'{tr("Инструменты")} · {backend["name"]}'))
    statistics_href = catalog_url("", backend["name"], path="/statistics")
    actions.append(utility_link(statistics_href, "chart", f'{tr("Статистика MCP")} · {backend["name"]}'))
    if auth == "basic" and can_manage:
        action = "Переавторизоваться" if state == "ready" else "Настроить подключение"
        actions.append(utility_link(f"/credentials?backend={backend['name']}", "key", tr(action)))
    if diagnostics:
        actions.append(utility_link(f"/diagnostics?backend={backend['name']}", "activity", tr("Диагностика")))
    address = backend.get("mcp_url")
    copy_feedback = ""
    if address:
        copy_button, copy_feedback = connection_copy(profile_key, backend["name"], address, protected)
        actions.append(copy_button)
    if csrf is not None:
        context = escape(reconnect_context, quote=True)
        caption = escape(f'{tr("Переподключить")} · {backend["name"]}', quote=True)
        actions.append(f'<form method="post" action="/reconnect/{name}{context}#backend-{name}">'
                       f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
                       f'<button type="submit" class="gateway-utility" aria-label="{caption}" title="{caption}">'
                       f'{icon("refresh")}</button></form>')
    links = f'<div class="backend-actions" data-ag-width="fill">{"".join(actions)}</div>' if actions else ""
    feedback = f'<p role="status">{escape(notice)}</p>' if notice else ""
    endpoint = (f'<p class="muted">{tr("Адрес MCP")} <code>{escape(address)}</code></p>'
                if address else "")
    return (
        f'<li id="backend-{name}"><div class="backend-heading"><h3>{name}</h3>'
        f'{connection_status(state, auth != "none")}</div>'
        f'<p class="muted">{tr(transport)} · {tr(auth_label)}</p>'
        f'<p>{tr("Доступных инструментов")} {display(backend["tools"])}</p>'
        f'{endpoint}{feedback}{last_call_line(backend.get("last_call_at"))}{links}{copy_feedback}</li>'
    )


def catalog_url(query: str, backend: str | None, offset: int = 0, *, path: str = "/", sort: ToolSort = "usage") -> str:
    """Build a relative URL for browser filters and page navigation."""
    parameters = {}
    if query:
        parameters["q"] = query
    if backend:
        parameters["backend"] = backend
    if offset:
        parameters["offset"] = offset
    if sort == "name":
        parameters["sort"] = sort
    encoded = urlencode(parameters)
    return f"{path}?{encoded}" if encoded else path


def catalog_navigation(page: dict | None, query: str, path: str) -> str:
    """Render page links while keeping the selected backend and search term."""
    if page is None or page["total"] <= page["size"]:
        return ""
    offset, size, total = page["offset"], page["size"], page["total"]
    links = [f"<span>{offset + 1}–{min(total, offset + size)} {tr('из')} {total}</span>"]
    if offset:
        previous = escape(catalog_url(query, page["backend"], offset - size, path=path, sort=page["sort"]), quote=True)
        links.append(f'<a rel="prev" href="{previous}">{tr("Назад")}</a>')
    if offset + size < total:
        following = escape(catalog_url(query, page["backend"], offset + size, path=path, sort=page["sort"]), quote=True)
        links.append(f'<a rel="next" href="{following}">{tr("Далее")}</a>')
    return f'<nav aria-label="{tr("Страницы инструментов")}">{" ".join(links)}</nav>'


def backend_filter(backends, backend: str | None, *, path: str, identity: str, label: str, action: str,
                   invalid: bool = False) -> str:
    """Render the AG native Select and its submit action on a common control line."""
    options = [f'<option value="">{tr("Все MCP")}</option>']
    for name in sorted(backends):
        selected = " selected" if name == backend else ""
        options.append(f'<option value="{escape(name)}"{selected}>{escape(name)}</option>')
    error = ""
    field_state = ""
    if invalid:
        error = f'<p class="ag-select__error" id="{identity}-error" role="alert">{tr("Выберите MCP этого профиля.")}</p>'
        field_state = f' aria-invalid="true" aria-describedby="{identity}-error" autofocus'
    return f"""<form class="gateway-filter" action="{path}" method="get" data-ag-form-row="stack">
<div class="ag-select" data-ag-form-field><label for="{identity}">{tr(label)}</label>
<div class="gateway-control-line">
<select class="ag-select__control" id="{identity}" name="backend"{field_state}>{"".join(options)}</select>
<button type="submit" class="ag-button ag-button--secondary">{tr(action)}</button></div>{error}</div></form>"""


def metric_table(label: str, keys, total: dict) -> str:
    """Use the AG Data Table structure for one group of comparable values."""
    rows = "".join(f'<tr><th scope="row">{tr(_METRICS[key])}</th>'
                   f'<td data-align="end" data-metric="{key}">{display(total[key])}</td></tr>' for key in keys)
    return f"""<div class="ag-data-table" data-ag-width="fill">
<div class="ag-data-table__scroll"><table><caption>{tr(label)}</caption>
<tbody>{rows}</tbody></table></div></div>"""


def render_statistics(profile_key: str, backends, backend: str | None, metrics: dict, *, invalid: bool = False) -> str:
    """Render process-lifetime metrics with a selector independent of catalog search and paging."""
    selector = backend_filter(backends, backend, path="/statistics", identity="statistics-backend",
                              label="Фильтр MCP", action="Показать статистику", invalid=invalid)
    summary = ""
    if not invalid:
        groups = "".join(metric_table(label, keys, metrics["total"]) for label, keys in _METRIC_GROUPS)
        summary = f"""<div class="metric-tables" data-metric-masonry>{groups}</div>
<div class="metric-notes muted"><p>{tr("Токены оценены по размеру JSON.")}</p>
<p>{tr("Ошибки записи журнала шлюза")} {metrics["journal_errors"]}.</p></div>"""
    content = f"""<section data-ag-width="fill" aria-labelledby="metrics">
<h2 id="metrics">{tr("Вызовы с момента запуска")}</h2>{selector}{summary}</section>"""
    return shell("/statistics", profile_key, tr("Статистика"), content)


def render(status: dict, query: str, *, credentials: bool = False,
           reconnect_csrf: str | None = None, notice: str = "", notice_backend: str | None = None,
           diagnostics: bool = False, section: str = "/", protected: bool = False) -> str:
    """Render one dashboard snapshot without executing upstream text or links."""
    metrics = status["metrics"]
    page = status.get("page")
    sort = page["sort"] if page else "usage"
    tools = status["catalog"]
    if page is None and query:
        term = query.casefold()
        tools = [tool for tool in tools if term in tool["name"].casefold()
                 or term in tool["description"].casefold()]
    count = len(tools)
    backend_filter = ""
    if page is not None:
        count = page["total"]
        if page["backend"]:
            backend = escape(page["backend"], quote=True)
            reset = escape(catalog_url(query, None, path=section, sort=sort), quote=True)
            backend_filter = (f'<input type="hidden" name="backend" value="{backend}">'
                              f'<span>{tr("Фильтр MCP")} <strong>{backend}</strong></span>'
                              f'<a href="{reset}">{tr("Все MCP")}</a>')
    reset_url = escape(catalog_url("", None, path=section, sort=sort), quote=True)
    reset_search = f'<a href="{reset_url}">{tr("Сбросить фильтры")}</a>' if query else ""
    context = catalog_url(query, page["backend"], page["offset"], sort=sort) if page is not None else "/"
    if section == "/catalog":
        separator = "&" if "?" in context else "?"
        context += f"{separator}return_to=catalog"
    profile_key = f"{status['environment']}/{status['profile']}" if status.get("environment") else status["profile"]
    cards_html = []
    server_query = query.strip() if section == "/catalog" else ""
    for item in status["connections"]:
        name = item["name"]
        if server_query and server_query.casefold() not in name.casefold():
            continue
        href = catalog_url("", name, path="/tools") + "#catalog" if item["tools"] else None
        cards_html.append(backend_card(item, credentials, href, reconnect_csrf,
                                       profile_key=profile_key, protected=protected,
                                       reconnect_context=context.removeprefix("/"), diagnostics=diagnostics,
                                       notice=notice if name == notice_backend else ""))
    backends = "".join(cards_html) or f'<li>{tr("MCP не найдены.")}</li>'
    server_search = ""
    if section in {"/", "/catalog"}:
        server_search = f"""<form class="server-search" action="/catalog" method="get" data-ag-form-row="stack">
<div class="search-field" data-ag-form-field><label for="server-query">{tr("Имя MCP")}</label>
<input id="server-query" name="q" maxlength="200" value="{escape(server_query, quote=True)}"></div>
<button type="submit" class="gateway-utility gateway-utility--submit" aria-label="{tr("Найти")}"
 title="{tr("Найти")}">{icon("search")}</button>
{f'<a href="/catalog">{tr("Сбросить фильтры")}</a>' if server_query else ""}</form>"""
    catalog = "".join(tool_row(tool, metrics["tools"], show_keywords=section == "/tools") for tool in tools)
    if not catalog:
        catalog = f'<li>{tr("Инструменты не найдены.")}</li>'
    catalog = f'<ul class="tools">{catalog}</ul>'
    usage_selected = " selected" if sort == "usage" else ""
    name_selected = " selected" if sort == "name" else ""
    message = f'<p role="status">{escape(notice)}</p>' if notice and notice_backend is None else ""
    servers_section = f"""<section id="backends" data-ag-width="fill" aria-labelledby="section-title">
{message}{server_search}<p class="muted">{tr("Настройка выполняется в локальном приложении macOS.")}</p><ul class="backends">{backends}</ul></section>"""
    rail_label = escape(tr("Быстрый переход к инструментам"), quote=True)
    tool_heading = "" if section == "/tools" else f'<h2 id="tool-results-title">{tr("Инструменты")}</h2>'
    tools_label = "section-title" if section == "/tools" else "tool-results-title"
    result_count = tr("Найдено инструментов: {count}").format(count=count)
    active_filters = f'<div class="catalog-filters">{backend_filter}{reset_search}</div>' if backend_filter or reset_search else ""
    tools_section = f"""<section id="catalog" data-ag-width="fill" aria-labelledby="{tools_label}">
<div class="tool-catalog" data-tool-catalog data-rail-label="{rail_label}">
<div class="tool-catalog-content" data-ag-width="content" data-ag-width-min-em="8">
{tool_heading}
<form class="gateway-filter" action="{section}" method="get" data-ag-form-row="stack">
<div class="gateway-tool-filters" data-ag-width="fill">
<div class="ag-field" data-ag-form-field><label class="ag-field__label" for="q">{tr("Имя или описание")}</label>
<input id="q" name="q" maxlength="200" value="{escape(query, quote=True)}"></div>
<div class="ag-select" data-ag-form-field><label for="tool-sort">{tr("Сортировка")}</label>
<select class="ag-select__control" id="tool-sort" name="sort">
<option value="usage"{usage_selected}>{tr("Часто используемые")}</option>
<option value="name"{name_selected}>{tr("По имени")}</option></select></div>
<button type="submit" class="gateway-utility gateway-utility--submit" aria-label="{tr("Найти")}"
 title="{tr("Найти")}">{icon("search")}</button>
</div><div class="gateway-control-line catalog-count muted">{result_count}{active_filters}</div></form>
{catalog_navigation(page, query, section)}
<div data-tool-results>{catalog}</div></div></div></section>"""
    show_legacy_tools = bool(page and (page["backend"] or page["offset"] or query or sort == "name"))
    sections = {
        "/catalog": (tr("Каталог MCP"), servers_section),
        "/tools": (tr("Инструменты"), tools_section),
        "/": (tr("Каталог MCP"), servers_section + (tools_section if show_legacy_tools else "")),
    }
    title, content = sections[section]
    return shell(section, profile_key, title, content, tool_catalog=section == "/tools" or (section == "/" and show_legacy_tools))
