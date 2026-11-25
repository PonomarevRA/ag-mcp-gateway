"""Shared navigation for the local gateway workspace."""

from html import escape

from .ui_locale import language, language_switch, mock_tests_available, tr

SECTIONS = (
    ("/", "Каталог MCP", "links"),
    ("/tools", "Инструменты", "tools"),
    ("/statistics", "Статистика", "chart"),
    ("/diagnostics", "Диагностика", "activity"),
    ("/modules", "Модули", "layers"),
    ("/tests", "Мок-тесты", "tests"),
    ("/settings", "Политика инструментов", "shield"),
    ("/credentials", "Учётные данные MCP", "key"),
    ("/profiles", "Профили", "folders"),
)

_ENVIRONMENTS = {"test": "Тестовая среда", "production": "Рабочая среда"}

_PATHS = {
    "links": "M10 8l3-3a4 4 0 016 6l-3 3M14 16l-3 3a4 4 0 01-6-6l3-3m0 6l8-8",
    "tools": "M5 7h14v13H5zm4 0V4h6v3m-10 5h14m-9 0v2h4v-2",
    "chart": "M4 3v17h17M8 16v-4m5 4V8m5 8V5",
    "activity": "M3 12h5l3-7 3 14 2-7h5",
    "layers": "M12 3l9 5-9 5-9-5zm-9 9l9 5 9-5M3 16l9 5 9-5",
    "tests": "M8 3h8M10 3v7l-5 8a2 2 0 001.7 3h10.6a2 2 0 001.7-3l-5-8V3M8 15h8",
    "shield": "M12 3l8 3v6c0 4-4 8-8 9-4-1-8-5-8-9V6zm-3 9l2 2 4-4",
    "key": "M15 12a4 4 0 110-8 4 4 0 010 8zm-3-1l-8 8m2-2l2 2",
    "folders": "M3 6h7l2 2h9v11H3z",
    "sidebar": "M3 4h18v16H3zM9 4v16",
    "plus": "M12 5v14M5 12h14",
    "play": "M8 5v14l11-7z",
    "save": "M5 3h12l4 4v14H3V3zM7 3v6h10V3M7 21v-8h10v8",
    "sliders": "M4 7h5m4 0h7M4 17h9m4 0h3M9 4v6m8 4v6",
    "download": "M12 4v11m-4-4 4 4 4-4M4 16v4h16v-4",
    "edit": "m4 16-1 5 5-1L21 7l-4-4zm10-9 4 4",
    "move": "M4 18h12m-4-4 4 4-4 4M4 7h16m-4-4 4 4-4 4",
    "chevron": "m9 5 7 7-7 7",
    "more": "M5 11h2v2H5zm6 0h2v2h-2zm6 0h2v2h-2z",
    "folder-plus": "M3 6h7l2 2h9v11H3zm9 4v6m-3-3h6",
    "grip": "M9 6h1m4 0h1M9 12h1m4 0h1M9 18h1m4 0h1",
    "copy": "M8 8h12v12H8zM16 8V4H4v12h4",
    "refresh": "M20 7v5h-5M20 12a8 8 0 1 0-2.5 5.8",
    "search": "M15 15l5 5M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 1 1 13 0z",
}
LOGO = """<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
  <rect width="24" height="24" rx="5.3" fill="#102932"/>
  <g fill="none" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
    <path d="M4.5 4.5V7.5L8.5 11.5V12C8.5 13.5 7.25 14.5 7.25 16C7.25 17.25 8.9 18 10.25 19.5" stroke="#77DCEC"/>
    <path d="M12 4.5V12C12 13.5 10.75 14.5 10.75 16C10.75 17.25 12.4 18 13.75 19.5" stroke="#A0F0DA"/>
    <path d="M19.5 4.5V7.5L15.5 11.5V12C15.5 13.5 14.25 14.5 14.25 16C14.25 17.25 15.9 18 17.25 19.5" stroke="#77DCEC"/>
  </g>
  <g>
    <rect x="3" y="3" width="3" height="3" rx=".45" fill="#77DCEC"/>
    <rect x="10.5" y="3" width="3" height="3" rx=".45" fill="#A0F0DA"/>
    <rect x="18" y="3" width="3" height="3" rx=".45" fill="#77DCEC"/>
    <rect x="9" y="19" width="9.5" height="2" rx=".55" fill="#A0F0DA"/>
  </g>
</svg>"""


def icon(name: str) -> str:
    """Render one gateway glyph. Its control owns the accessible name."""
    return (f'<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" '
            f'stroke="currentColor" stroke-width="1.65"><path d="{_PATHS[name]}"/></svg>')


def utility_link(href: str, glyph: str, name: str) -> str:
    """Render a named icon action with a native pointer hint."""
    label = escape(name, quote=True)
    return (f'<a class="gateway-utility" href="{escape(href, quote=True)}" '
            f'aria-label="{label}" title="{label}">{icon(glyph)}</a>')


def sidebar(current: str, profile: str) -> str:
    """Render the main navigation and the profile footer in reading order."""
    links = []
    if current == "/catalog":
        current = "/"
    for path, title, glyph in SECTIONS:
        if path == "/tests" and not mock_tests_available():
            continue
        state = ' aria-current="page"' if path == current else ""
        label = escape(tr(title), quote=True)
        links.append(f'<a href="{path}"{state} aria-label="{label}" title="{label}">{icon(glyph)}<span>{label}</span></a>')
    environment = _ENVIRONMENTS.get(profile.partition("/")[0]) if "/" in profile else None
    environment_text = f"<small>{tr(environment)}</small>" if environment else ""
    return (f'<aside id="workspace-navigation" class="workspace-sidebar" data-ag-width="content" '
            f'data-ag-width-min-em="6.5"><nav aria-label="{tr("Основная навигация")}">{"".join(links)}</nav>'
            f'<div class="workspace-profile">{tr("Профиль")}<strong>{escape(profile)}</strong>'
            f'{environment_text}</div></aside>')


def revision_notice(kind: str, revision: str, refresh_url: str) -> str:
    """Announce a newer server revision without replacing the operator's draft."""
    return f"""<aside role="status" aria-live="polite">
<div class="module-message" data-revision-kind="{kind}" data-settings-revision="{escape(revision, quote=True)}" hidden>
{tr("Есть изменения. Откройте актуальную версию, чтобы продолжить.")}
<a href="{escape(refresh_url, quote=True)}">{tr("Обновить данные")}</a>
</div></aside>"""


def shell(current: str, profile: str, title: str, content: str, *,
          language_backend: str | None = None, tool_catalog: bool = False) -> str:
    """Wrap server-rendered sections in the shared toolbar and navigation."""
    hide, show = tr("Свернуть меню"), tr("Развернуть меню")
    add = ""
    if current in {"/", "/catalog"}:
        label = tr("Добавить MCP")
        key = escape(profile, quote=True)
        add = (f'<a class="header-icon" href="agm-gateway://add-mcp/{key}" '
               f'aria-label="{label}" title="{label}">{icon("plus")}</a>')
    assets = ('<link rel="stylesheet" href="/assets/ag-rail/styles.css">'
              '<script type="module" src="/assets/tool-catalog.js"></script>') if tool_catalog else ""
    if current == "/statistics":
        assets += '<script type="module" src="/assets/statistics-masonry.js"></script>'
    if current == "/tests":
        assets += ('<link rel="stylesheet" href="/assets/mock-test-workspace.css">'
                   '<script src="/assets/mock-test-tabs.js" defer></script>')
    return f"""<!doctype html>
<html lang="{language()}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · ag-mcp-gateway</title><link rel="icon" href="/gateway.svg">
<link rel="stylesheet" href="/assets/ag-controls/primitives.css">
<link rel="stylesheet" href="/assets/ag-controls/select.css">
<link rel="stylesheet" href="/assets/ag-controls/data-table.css">
<link rel="stylesheet" href="/dashboard.css"><script src="/workspace.js" defer></script>{assets}
<body class="workspace" data-ag-width="content" data-ag-width-min-em="15">
<a class="skip-link" href="#main-content">{tr("К содержанию")}</a>
<header class="workspace-bar" data-ag-width="content" data-ag-width-min-em="15">
<div class="workspace-nav-header" data-ag-width="content" data-ag-width-min-em="1.25"><button type="button" class="header-icon" id="sidebar-toggle" aria-controls="workspace-navigation"
 aria-expanded="true" aria-label="{hide}" title="{hide}" data-show="{show}" data-hide="{hide}" hidden>{icon("sidebar")}</button></div><div class="workspace-content-header" data-ag-width="content" data-ag-width-min-em="6">
<a class="workspace-brand" href="/" aria-label="ag-mcp-gateway">{LOGO}<span>ag-mcp-gateway</span></a>
{language_switch(backend=language_backend)}{add}</div></header>
{sidebar(current, profile)}
<main id="main-content" tabindex="-1" data-ag-width="content" data-ag-width-min-em="15"><h1 id="section-title" class="workspace-title">{escape(title)}</h1>
{content}</main></body></html>"""
