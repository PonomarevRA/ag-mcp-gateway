"""Render the profile-local collection tree and its native form actions."""

from collections import defaultdict
from html import escape
from urllib.parse import urlencode

from .ui_locale import language
from .ui_shell import icon


def label(ru: str, en: str) -> str:
    """Return an owned label in the current workspace language."""
    return en if language() == "en" else ru


def scenario_url(identity: str, run_id: str = "", *, editing: bool = True) -> str:
    """Link to a request and optionally to a retained run beside it."""
    query = {"edit": identity} if identity else {}
    if not editing or not identity:
        query["mode"] = "edit" if editing else "view"
    if run_id:
        query["run"] = run_id
    return "/tests?" + urlencode(query)


def new_request_url(group_id: str = "") -> str:
    """Open a fresh request with the destination collection preselected."""
    return "/tests?" + urlencode({"mode": "edit", "group": group_id})


def icon_link(href: str, glyph: str, name: str) -> str:
    """Render a compact navigation action with a localized focus hint."""
    caption = escape(name, quote=True)
    return (f'<span class="mock-icon-slot" data-hint="{caption}"><a class="mock-icon" '
            f'href="{escape(href, quote=True)}" aria-label="{caption}">{icon(glyph)}</a></span>')


def hidden(name: str, value: str | int) -> str:
    """Encode one form value for native and enhanced submissions."""
    return f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'


def group_options(groups: list[dict], selected: str | None) -> str:
    """Give each placement control the same stable set of destinations."""
    choices = [("", label("Без коллекции", "Ungrouped"))]
    choices.extend((item["id"], item["title"]) for item in groups)
    options = []
    for value, title in choices:
        state = " selected" if value == (selected or "") else ""
        options.append(f'<option value="{escape(value)}"{state}>{escape(title)}</option>')
    return "".join(options)


def disclosure(glyph: str, name: str, content: str, *, show_label: bool = False) -> str:
    """Expose a native disclosure for a compact action or a labelled menu choice."""
    caption = escape(name, quote=True)
    visible = f"<span>{caption}</span>" if show_label else ""
    style = "mock-menu-choice" if show_label else "mock-icon mock-tree-icon"
    return (f'<details class="mock-catalog-action"><summary class="{style}" '
            f'aria-label="{caption}" title="{caption}">{icon(glyph)}{visible}</summary>{content}</details>')


def catalog_submit(glyph: str, name: str) -> str:
    """Keep catalog actions usable when JavaScript is disabled."""
    caption = escape(name, quote=True)
    return (f'<span class="mock-icon-slot" data-hint="{caption}">'
            f'<button type="submit" class="mock-icon" aria-label="{caption}">{icon(glyph)}</button></span>')


def collection_form(group: dict, csrf: str, identity: str) -> str:
    """Render the shared create and rename collection control."""
    group_id = group["id"]
    caption = label("Переименовать коллекцию", "Rename collection") if group_id else label(
        "Новая коллекция", "New collection")
    return disclosure("edit" if group_id else "folder-plus", caption, f"""
<form action="/tests/groups" method="post" class="mock-catalog-form">
{hidden("csrf", csrf)}{hidden("id", group_id)}{hidden("revision", group["revision"])}{hidden("current", identity)}
<label>{label("Название коллекции", "Collection name")}
<input name="title" value="{escape(group["title"])}" required maxlength="100"></label>
{catalog_submit("save", label("Сохранить коллекцию", "Save collection"))}</form>""", show_label=bool(group_id))


def request_row(item: dict, groups: list[dict], identity: str, editing: bool, csrf: str) -> str:
    """Render a saved request with an optional move action and drag handle."""
    request_id = item["id"]
    current = ' aria-current="page"' if identity == request_id else ""
    move = ""
    handle = ""
    if editing:
        handle = f'<span class="mock-drag-handle" data-mock-drag aria-hidden="true">{icon("grip")}</span>'
        move = disclosure("move", label("Переместить запрос", "Move request"), f"""
<form action="/tests/move" method="post" class="mock-catalog-form" data-mock-move>
{hidden("csrf", csrf)}{hidden("id", request_id)}{hidden("revision", item["placement_revision"])}{hidden("current", identity)}
<label>{label("Коллекция назначения", "Destination collection")}
<select name="group_id">{group_options(groups, item["group_id"])}</select></label>
{catalog_submit("move", label("Переместить", "Move"))}</form>""")
    href = escape(scenario_url(request_id, editing=editing))
    title = escape(item["title"], quote=True)
    request_label = escape(f'{item["title"]} · {item["tool"]}', quote=True)
    marker = f'<span class="mock-request-marker">{icon("tests")}</span>'
    return (f'<li data-mock-request="{escape(request_id)}"><div class="mock-request-row" data-ag-width="fill">{handle}'
            f'<a href="{href}"{current} aria-label="{request_label}" title="{request_label}">'
            f'{marker}<span>{title}</span></a>{move}</div></li>')


def collection_actions(group: dict, csrf: str, identity: str) -> str:
    """Show one row action for collections and group related commands in its disclosure."""
    group_id = group["id"]
    if not group_id:
        return ""
    url = new_request_url(group_id)
    create = label("Новый запрос в коллекции", "New request in collection")
    new_link = f'<a class="mock-menu-choice" href="{escape(url, quote=True)}">{icon("plus")}<span>{create}</span></a>'
    actions = new_link + collection_form(group, csrf, identity)
    return disclosure("more", label("Действия коллекции", "Collection actions"),
                      f'<div class="mock-collection-menu">{actions}</div>')


def collection_section(group: dict, entries: list[dict], groups: list[dict],
                       identity: str, editing: bool, csrf: str) -> str:
    """Keep empty collections visible as possible destinations."""
    group_id = group["id"]
    title = escape(group["title"])
    actions = collection_actions(group, csrf, identity) if editing else ""
    list_id = "mock-group-rows-" + group_id if group_id else "mock-unassigned-rows"
    rows = "".join(request_row(item, groups, identity, editing, csrf) for item in entries)
    return f"""<section class="mock-collection" data-mock-collection="{escape(group_id)}" aria-label="{title}"
 data-ag-width="fill">
<header class="mock-collection-row" data-ag-width="fill">
<button type="button" class="mock-collection-toggle" data-mock-collapse aria-expanded="true" aria-controls="{list_id}"
 aria-label="{title}" title="{title}" hidden>{icon("chevron")}{icon("folders")}<span>{title}</span></button>
<h3 data-mock-unenhanced>{title}</h3><span data-mock-count>{len(entries)}</span>{actions}</header>
<ul id="{list_id}" data-ag-width="fill">{rows}</ul></section>"""


def render_catalog(data: dict, identity: str, editing: bool, csrf: str, run_id: str = "", *, profile: str = "") -> str:
    """Compose the collection tree with explicit modes and client-side search."""
    groups = data["groups"]
    ungrouped = {"id": "", "title": label("Без коллекции", "Ungrouped")}
    by_group = defaultdict(list)
    known = {group["id"] for group in groups}
    for entry in data["scenarios"]:
        by_group[entry["group_id"] if entry["group_id"] in known else ""].append(entry)
    sections = "".join(collection_section(group, by_group[group["id"]], groups, identity, editing, csrf)
                       for group in [*groups, ungrouped])
    actions = icon_link(new_request_url(), "plus", label("Новый запрос", "New request"))
    if editing:
        actions += collection_form({"id": "", "title": "", "revision": 0}, csrf, identity)
    title = label("Коллекции", "Collections")
    modes = []
    for option, caption in [(False, label("Просмотр", "View")), (True, label("Редактирование", "Edit"))]:
        selected = ' aria-current="page"' if option == editing else ""
        href = escape(scenario_url(identity, run_id, editing=option))
        modes.append(f'<a href="{href}"{selected}>{caption}</a>')
    search = label("Поиск запросов", "Find requests")
    return f"""<nav class="mock-library" aria-labelledby="mock-library-title" data-ag-width="fill" data-mock-catalog
 data-mock-profile="{escape(profile, quote=True)}"
 data-moved="{label("Запрос перемещён.", "Request moved.")}"
 data-move-error="{label("Перенос не выполнен. Проверьте соединение и обновите каталог.",
                        "Could not move the request. Check the connection and refresh the catalog.")}">
<div class="mock-library-header"><h2 id="mock-library-title">{title}</h2><div class="mock-catalog-actions">{actions}</div></div>
<div class="mock-catalog-modes">{"".join(modes)}</div>
<input type="search" data-mock-search hidden aria-label="{search}" placeholder="{search}">
<p data-mock-catalog-status role="status"></p>
<div class="mock-collections" data-ag-width="fill">{sections}</div></nav>"""
