"""Render the browser-facing module catalog and dispatch authenticated forms."""

import json
from datetime import UTC, datetime
from hmac import compare_digest
from html import escape
from secrets import token_urlsafe
from urllib.parse import urlencode

from pydantic import ValidationError
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .credential_ui import SECURE_HTML, read_form
from .module_data import MAX_DATA, ModuleError, parse_json
from .module_registry import ActivateHook, Bind, Delete, HookCapability, PinHook, Run, Save, SetGenerations
from .ui_locale import language, language_switch, module_error, tr

EXAMPLE = {
    "id": "project",
    "title": "Выбрать поле",
    "steps": [{"op": "object", "fields": {"value": "/input/value"}}],
}
OPS = {
    "select": "Выбор по пути", "object": "Сборка объекта", "compare": "Сравнение JSON",
    "contains": "Проверка включения", "filter": "Фильтр JSON", "aggregate": "Агрегация",
    "math": "Арифметика", "call": "Вызов MCP",
}
_CONFIRMATIONS = {
    "save": "Модуль сохранён.", "bind": "Привязка обновлена.", "generations": "Пороги сохранены.",
    "pin": "Закрепление обновлено.", "activate": "Активация обновлена.", "close": "Сессия закрыта.",
    "delete": "Модуль удалён.",
}
_RETAINED_FIELDS = {
    "save": {"data": 65_536}, "run": {"id": 32, "data": 65_536},
    "bind": {"tool": 128, "phase": 6, "module": 32},
}
_FORM_SECTIONS = {"save": "editor", "run": "run", "bind": "hooks"}


def encoded(value) -> str:
    """Serialize a document for an escaped, inert browser editor or preview."""
    return escape(json.dumps(value, ensure_ascii=False, indent=2))


def empty_row(message: str) -> str:
    """Render the localized empty state for a module list."""
    return f"<li>{tr(message)}</li>"


def result_panel(result) -> str:
    """Render the explicit operation's escaped JSON result beside its form."""
    return f'<div class="module-preview"><h3>{tr("Результат")}</h3><pre>{encoded(result)}</pre></div>'


def options(values, *, blank: str | None = None, selected: str | None = None) -> str:
    """Render value/label pairs with an optional first empty choice."""
    items = [f'<option value="">{escape(blank)}</option>'] if blank is not None else []
    for value, label in values:
        current = " selected" if value == selected else ""
        items.append(f'<option value="{escape(value)}"{current}>{escape(label)}</option>')
    return "".join(items)


def session_time(value: int) -> str:
    """Display a session event in UTC without exposing a caller's timezone."""
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M UTC")


class ModulePage:
    """Keep the browser UI on the same registry and policy path as management MCP."""

    def __init__(self, service, origin: str, csrf: str):
        """Bind the authenticated profile service and browser request boundary."""
        self.service = service
        self.origin = origin
        self.csrf = csrf
        self.confirmations: dict[str, str] = {}

    def hidden(self, action: str, revision: int) -> str:
        """Bind an action to this UI instance and the currently rendered revision."""
        return (f'<input type="hidden" name="csrf" value="{escape(self.csrf)}">'
                f'<input type="hidden" name="action" value="{action}">'
                f'<input type="hidden" name="revision" value="{revision}">')

    def lifecycle(self, current: dict, local: list[tuple[str, str]], *, feedback: str = "", preview: str = "") -> str:
        """Render generation settings, history and explicit session controls."""
        revision = current["revision"]
        rules = current["generation_rules"]
        items = []
        for identity, life in sorted(current["hooks"].items()):
            pin = tr("Снять закрепление" if life["pinned"] else "Закрепить")
            pin_value = "no" if life["pinned"] else "yes"
            sessions = []
            for session in life["sessions"]:
                sessions.append(f'<li><code>{session["id"]}</code> · {session_time(session["used_at"])}</li>')
            protection = " · " + tr("закреплён") if life["pinned"] else ""
            expiry = ""
            if life["retention_deadline"] is not None:
                expiry = f'<p class="muted">{tr("Порог очистки")} {session_time(life["retention_deadline"])} {tr("без активной привязки")}</p>'
            items.append(f"""<li><strong>{escape(current["modules"][identity]["title"])}</strong>
<p>Gen{life["generation"]}{protection} · {tr("Сессий")} {len(sessions)}</p>{expiry}
<details><summary>{tr("Выжившие сессии")}</summary><ul>{"".join(sessions) or empty_row("Успешных сессий пока нет.")}</ul></details>
<form method="post" action="/modules">{self.hidden("pin", revision)}
<input type="hidden" name="id" value="{identity}"><input type="hidden" name="pinned" value="{pin_value}">
<button>{pin}</button></form></li>""")
        active = "".join(f'<li><code>{session["id"]}</code> · {tr("до")} {session_time(session["expires_at"])} · {tr("привязок")} {session["bindings"]}</li>'
                         for session in current["active_sessions"])
        local_options = options(local, blank=tr("Снять активацию"))
        return f"""<section aria-labelledby="lifecycle"><h2 id="lifecycle">{tr("Хуки и сессии")}</h2>
<p>{tr("Считается одно успешное применение на сессию. Срок неактивности Gen3, Gen2 и Gen1 — 1, 7 и 30 дней.")}</p>
<ul class="tools">{"".join(items) or empty_row("Локальных шаблонов пока нет.")}</ul>
<h3>{tr("Пороги выживания")}</h3>
<form class="module-form" method="post" action="/modules" data-ag-form-row="stack">{self.hidden("generations", revision)}
<div class="module-field" data-ag-form-field><label for="gen2">{tr("Сессий для Gen2")}</label>
<input id="gen2" name="gen2" type="number" min="2" max="15" value="{rules["gen2_sessions"]}" required></div>
<div class="module-field" data-ag-form-field><label for="gen1">{tr("Сессий для Gen1")}</label>
<input id="gen1" name="gen1" type="number" min="3" max="16" value="{rules["gen1_sessions"]}" required></div>
<button>{tr("Сохранить пороги")}</button></form>
<h3>{tr("Активные сессии")}</h3><ul class="tools">{active or empty_row("Активных сессий нет.")}</ul>
<form method="post" action="/modules#lifecycle">{self.hidden("open", revision)}<button>{tr("Открыть сессию на час")}</button></form>
{feedback}{preview}
<details><summary>{tr("Управление активацией")}</summary>
<form class="module-form" method="post" action="/modules" data-ag-form-row="stack">{self.hidden("activate", revision)}
<div class="module-field" data-ag-form-field><label for="capability">{tr("Ключ сессии")}</label>
<input id="capability" name="capability" type="password" minlength="43" maxlength="43" autocomplete="off" required></div>
<div class="module-field" data-ag-form-field><label for="session-tool">{tr("Инструмент сессии")}</label>
<input id="session-tool" name="tool" list="module-tools" maxlength="128" required></div>
<div class="module-field" data-ag-form-field><label for="session-phase">{tr("Этап сессии")}</label>
<select id="session-phase" name="phase"><option value="before">{tr("До запроса")}</option><option value="after">{tr("После ответа")}</option></select></div>
<div class="module-field" data-ag-form-field><label for="session-module">{tr("Шаблон сессии")}</label>
<select id="session-module" name="module">{local_options}</select></div><button>{tr("Сохранить активацию")}</button></form>
<form class="module-form" method="post" action="/modules" data-ag-form-row="stack">{self.hidden("close", revision)}
<div class="module-field" data-ag-form-field><label for="close-capability">{tr("Ключ закрываемой сессии")}</label>
<input id="close-capability" name="capability" type="password" minlength="43" maxlength="43" autocomplete="off" required></div>
<button>{tr("Закрыть сессию")}</button></form></details></section>"""

    def render(self, edit: str = "", *, tool_hint: str = "", notice: str = "", result: dict | None = None,
               status: int = 200, form: dict[str, str] | None = None, opened_session: dict | None = None):
        """Render bounded definitions, form actions and the last explicit run result."""
        current = self.service.registry.catalog()
        modules = current["modules"]
        revision = current["revision"]
        form = form or {}
        action = form.get("action")

        def form_revision(name: str) -> int:
            """Keep stale drafts fenced until the operator reloads the saved version."""
            value = form.get("revision", "")
            return self.revision(value) if action == name and value else revision

        selected = modules.get(edit, {**EXAMPLE, "title": tr(EXAMPLE["title"])})
        definition = escape(form["data"]) if action == "save" else encoded(selected)
        edit_action = "/modules?" + urlencode({"edit": edit}) if edit in modules else "/modules"
        local = []
        items = []
        for identity, module in sorted(modules.items()):
            operations = [step["op"] for step in module["steps"]]
            if "call" not in operations:
                local.append((identity, module["title"]))
            labels = " → ".join(tr(OPS[operation]) for operation in operations)
            items.append(f'<li><a href="/modules?edit={identity}#editor">{escape(module["title"])}</a>'
                         f'<p class="muted">{identity} · {escape(labels)}</p></li>')
        hooks = []
        labels = {"before": "До запроса", "after": "После ответа"}
        for tool, phases in sorted(current["bindings"].items()):
            for phase, name in phases.items():
                hooks.append(f'<li><strong>{escape(tool)}</strong> · {tr(labels[phase])} · {escape(name)}</li>')
        routes = set(self.service.router.routes) | set(current["bindings"])
        tool_options = options((tool, tool) for tool in sorted(routes)[:100])
        run_module = form.get("id") if action == "run" else edit
        run_data = form["data"] if action == "run" else '{"value": 42}'
        module_options = options(((name, value["title"]) for name, value in sorted(modules.items())), selected=run_module)
        hook_module = form.get("module") if action == "bind" else None
        hook_options = options(local, blank=tr("Снять привязку"), selected=hook_module)
        if action == "bind":
            tool_hint = form["tool"]
        after_selected = " selected" if action == "bind" and form.get("phase") == "after" else ""
        bind_disabled = "" if routes else " disabled"
        role = "alert" if status >= 400 else "status"
        alert = f'<p id="module-feedback" role="{role}" class="module-message">{escape(tr(notice))}</p>' if notice else ""
        feedback_section = "lifecycle" if opened_session is not None else _FORM_SECTIONS.get(action)
        preview = result_panel(result["result"]) if result is not None else ""
        session_preview = result_panel(opened_session) if opened_session is not None else ""
        lifecycle = self.lifecycle(current, local, feedback=alert if feedback_section == "lifecycle" else "",
                                   preview=session_preview)
        delete = ""
        if edit in modules:
            delete = (f'<form method="post" action="/modules">{self.hidden("delete", revision)}'
                      f'<input type="hidden" name="id" value="{edit}"><button>{tr("Удалить модуль")}</button></form>')
        disabled = "" if modules else " disabled"
        html = f"""<!doctype html>
<html lang="{language()}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{tr("Модули MCP")}</title><link rel="stylesheet" href="/dashboard.css">
<main class="module-layout">
<nav class="module-nav" aria-label="{tr("Разделы")}"><a href="/">{tr("Каталог MCP")}</a>
<a href="/settings">{tr("Настройки")}</a><a href="/modules" aria-current="page">{tr("Модули")}</a></nav>
<div class="module-main"><header><div><h1>{tr("Модули MCP")}</h1>
<p>{tr("Преобразования JSON, вычисления и цепочки чтения")}</p></div>{language_switch()}</header>{alert if feedback_section is None else ""}
<section aria-labelledby="library"><h2 id="library">{tr("Реестр")}</h2><p>{tr("Ревизия")} {revision}</p>
<ul class="tools">{"".join(items) or empty_row("Модули ещё не добавлены.")}</ul>
<a href="/modules#editor">{tr("Новый модуль")}</a></section>
<section aria-labelledby="editor"><h2 id="editor">{tr("Определение модуля")}</h2>
{alert if feedback_section == "editor" else ""}
<form class="module-form" method="post" action="{escape(edit_action)}#editor" data-ag-form-row="stack">{self.hidden("save", form_revision("save"))}
<div class="module-field" data-ag-form-field><label for="definition">{tr("Определение JSON")}</label>
<textarea id="definition" name="data" rows="11" maxlength="65536" aria-describedby="definition-help" required>{definition}</textarea>
<p id="definition-help" class="muted">{tr("Операции: select, object, compare, contains, filter, aggregate, math, call.")}</p></div>
<button>{tr("Сохранить модуль")}</button></form>{delete}</section>
<section aria-labelledby="hooks"><h2 id="hooks">{tr("Привязки к инструментам")}</h2>
{alert if feedback_section == "hooks" else ""}
<ul class="tools">{"".join(hooks) or empty_row("Привязок пока нет.")}</ul>
<form class="module-form" method="post" action="{escape(edit_action)}#hooks" data-ag-form-row="stack">{self.hidden("bind", form_revision("bind"))}
<div class="module-field" data-ag-form-field><label for="tool">{tr("Инструмент")}</label>
<input id="tool" name="tool" value="{escape(tool_hint)}" list="module-tools" maxlength="128"
 aria-describedby="tool-help" required{bind_disabled}>
<datalist id="module-tools">{tool_options}</datalist>
<p id="tool-help" class="muted">{tr("Укажите полное имя из каталога. Подсказки показывают до 100 имён.")}</p></div>
<div class="module-field" data-ag-form-field><label for="phase">{tr("Этап")}</label><select id="phase" name="phase"><option value="before">{tr("До запроса")}</option>
<option value="after"{after_selected}>{tr("После ответа")}</option></select></div>
<div class="module-field" data-ag-form-field><label for="binding">{tr("Локальный модуль")}</label><select id="binding" name="module">{hook_options}</select></div>
<button{bind_disabled}>{tr("Сохранить привязку")}</button></form></section>
<section aria-labelledby="run"><h2 id="run">{tr("Ручной запуск")}</h2>
{alert if feedback_section == "run" else ""}
<form class="module-form" method="post" action="{escape(edit_action)}#run" data-ag-form-row="stack">{self.hidden("run", form_revision("run"))}
<div class="module-field" data-ag-form-field><label for="run-module">{tr("Модуль")}</label><select id="run-module" name="id" required{disabled}>{module_options}</select></div>
<div class="module-field" data-ag-form-field><label for="run-data">{tr("Входной JSON")}</label><textarea id="run-data" name="data" rows="4" maxlength="65536" required{disabled}>{escape(run_data)}</textarea></div>
<button{disabled}>{tr("Запустить модуль")}</button></form>{preview}</section>{lifecycle}</div></main></html>"""
        return HTMLResponse(html, status_code=status, headers=SECURE_HTML)

    async def overview(self, request):
        """Open the library or edit one selected module."""
        receipt = request.query_params.get("confirmation", "")
        notice = self.confirmations.pop(receipt, "") if request.method == "GET" else ""
        return self.render(request.query_params.get("edit", "")[:32], tool_hint=request.query_params.get("tool", "")[:128],
                           notice=notice)

    def confirmation(self, action: str, edit: str, tool: str):
        """Confirm a successful form edit once without letting URL text impersonate a result."""
        receipt = token_urlsafe(24)
        self.confirmations[receipt] = _CONFIRMATIONS[action]
        if len(self.confirmations) > 32:
            self.confirmations.pop(next(iter(self.confirmations)))
        query = urlencode({key: value for key, value in (("edit", edit), ("tool", tool), ("confirmation", receipt))
                           if value})
        return RedirectResponse(f"/modules?{query}#module-feedback", status_code=303, headers=SECURE_HTML)

    async def submit(self, request):
        """Validate browser origin and CSRF before making changes or running modules."""
        if request.headers.get("origin") != self.origin:
            return PlainTextResponse(tr("Недопустимый источник формы"), status_code=403, headers=SECURE_HTML)
        try:
            fields = await read_form(request, limit=3 * MAX_DATA + 1024, max_fields=7)
        except ValueError:
            return self.render(notice="Некорректная форма.", status=400)
        if not compare_digest(fields.get("csrf", "").encode(), self.csrf.encode()):
            return PlainTextResponse(tr("Недопустимый токен формы"), status_code=403, headers=SECURE_HTML)
        action = fields.get("action", "")
        edit = request.query_params.get("edit", "")[:32]
        form = {key: fields.get(key, "")[:limit] for key, limit in _RETAINED_FIELDS.get(action, {}).items()}
        if form:
            form["action"] = action
        try:
            revision = self.revision(fields.get("revision", ""))
            if form:
                form["revision"] = str(revision)
            if action == "open":
                opened = await self.service.open_session()
                return self.render(edit, notice="Сессия открыта. Сохраните ключ для клиента.", opened_session=opened)
            if action == "run":
                query = Run.model_validate({"id": fields.get("id"), "data": parse_json(fields.get("data", "")),
                                            "expected_revision": revision})
                return self.render(edit, notice="Модуль выполнен.", result=await self.service.run(query), form=form)
            saved = await self.edit(action, fields, revision)
        except ValidationError:
            return self.render(edit, notice="Проверьте поля и определение модуля.", status=400, form=form)
        except ModuleError as error:
            return self.render(edit, notice=module_error(str(error)), status=400, form=form)
        except OSError:
            return self.render(edit, notice="Не удалось сохранить настройки.", status=503, form=form)
        tool = fields.get("tool", "") if action == "bind" else ""
        return self.confirmation(action, saved or edit, tool)

    @staticmethod
    def revision(value: str) -> int:
        """Parse a bounded revision from one form field."""
        if not value.isascii() or not value.isdecimal() or len(value) > 20:
            raise ModuleError("Некорректная ревизия.")
        return int(value)

    async def edit(self, action: str, fields: dict[str, str], revision: int) -> str | None:
        """Translate one form action into the same typed edit used by Bridge."""
        payload = {"expected_revision": revision}
        if action == "save":
            payload["module"] = parse_json(fields.get("data", ""))
            change = Save.model_validate(payload)
            await self.service.save(change)
            return change.module.id
        elif action == "bind":
            payload.update(tool=fields.get("tool"), phase=fields.get("phase"), module=fields.get("module") or None)
            await self.service.bind(Bind.model_validate(payload))
        elif action == "generations":
            payload["rules"] = {"gen2_sessions": self.revision(fields.get("gen2", "")),
                                "gen1_sessions": self.revision(fields.get("gen1", ""))}
            await self.service.set_generations(SetGenerations.model_validate(payload))
        elif action == "pin":
            pinned = fields.get("pinned")
            if pinned not in {"yes", "no"}:
                raise ModuleError("Укажите, нужно ли закрепить хук.")
            payload.update(id=fields.get("id"), pinned=pinned == "yes")
            await self.service.pin(PinHook.model_validate(payload))
        elif action == "activate":
            payload.update(tool=fields.get("tool"), phase=fields.get("phase"), module=fields.get("module") or None,
                           capability=fields.get("capability"))
            await self.service.activate(ActivateHook.model_validate(payload))
        elif action == "close":
            await self.service.close_session(HookCapability.model_validate({"capability": fields.get("capability")}))
        elif action == "delete":
            payload["id"] = fields.get("id")
            await self.service.delete(Delete.model_validate(payload))
        else:
            raise ModuleError("Неизвестное действие.")
        return None
