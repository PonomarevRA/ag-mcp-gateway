"""Render the browser-facing module catalog and dispatch authenticated forms."""

import json
from datetime import UTC, datetime
from hmac import compare_digest
from html import escape

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


def encoded(value) -> str:
    """Serialize a document for an escaped, inert browser editor or preview."""
    return escape(json.dumps(value, ensure_ascii=False, indent=2))


def empty_row(message: str) -> str:
    """Render the localized empty state for a module list."""
    return f"<li>{tr(message)}</li>"


def options(values, *, blank: str | None = None) -> str:
    """Render value/label pairs with an optional first empty choice."""
    items = [f'<option value="">{escape(blank)}</option>'] if blank is not None else []
    for value, label in values:
        items.append(f'<option value="{escape(value)}">{escape(label)}</option>')
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

    def hidden(self, action: str, revision: int) -> str:
        """Bind an action to this UI instance and the currently rendered revision."""
        return (f'<input type="hidden" name="csrf" value="{escape(self.csrf)}">'
                f'<input type="hidden" name="action" value="{action}">'
                f'<input type="hidden" name="revision" value="{revision}">')

    def lifecycle(self, current: dict, local: list[tuple[str, str]]) -> str:
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
<form method="post" action="/modules">{self.hidden("open", revision)}<button>{tr("Открыть сессию на час")}</button></form>
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

    def render(self, edit: str = "", *, tool_hint: str = "", notice: str = "", result: dict | None = None, status: int = 200):
        """Render bounded definitions, form actions and the last explicit run result."""
        current = self.service.registry.catalog()
        modules = current["modules"]
        revision = current["revision"]
        selected = modules.get(edit, {**EXAMPLE, "title": tr(EXAMPLE["title"])})
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
        module_options = options((name, value["title"]) for name, value in sorted(modules.items()))
        hook_options = options(local, blank=tr("Снять привязку"))
        bind_disabled = "" if routes else " disabled"
        alert = f'<p role="status" class="module-message">{escape(tr(notice))}</p>' if notice else ""
        preview = (f'<div class="module-preview"><h3>{tr("Результат")}</h3><pre>{encoded(result["result"])}</pre></div>'
                   if result is not None else "")
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
<p>{tr("Преобразования JSON, вычисления и цепочки чтения")}</p></div>{language_switch()}</header>{alert}
<section aria-labelledby="library"><h2 id="library">{tr("Реестр")}</h2><p>{tr("Ревизия")} {revision}</p>
<ul class="tools">{"".join(items) or empty_row("Модули ещё не добавлены.")}</ul>
<a href="/modules#editor">{tr("Новый модуль")}</a></section>
<section aria-labelledby="editor"><h2 id="editor">{tr("Определение модуля")}</h2>
<form class="module-form" method="post" action="/modules" data-ag-form-row="stack">{self.hidden("save", revision)}
<div class="module-field" data-ag-form-field><label for="definition">{tr("Определение JSON")}</label>
<textarea id="definition" name="data" rows="11" maxlength="65536" aria-describedby="definition-help" required>{encoded(selected)}</textarea>
<p id="definition-help" class="muted">{tr("Операции: select, object, compare, contains, filter, aggregate, math, call.")}</p></div>
<button>{tr("Сохранить модуль")}</button></form>{delete}</section>
<section aria-labelledby="hooks"><h2 id="hooks">{tr("Привязки к инструментам")}</h2>
<ul class="tools">{"".join(hooks) or empty_row("Привязок пока нет.")}</ul>
<form class="module-form" method="post" action="/modules" data-ag-form-row="stack">{self.hidden("bind", revision)}
<div class="module-field" data-ag-form-field><label for="tool">{tr("Инструмент")}</label>
<input id="tool" name="tool" value="{escape(tool_hint)}" list="module-tools" maxlength="128"
 aria-describedby="tool-help" required{bind_disabled}>
<datalist id="module-tools">{tool_options}</datalist>
<p id="tool-help" class="muted">{tr("Укажите полное имя из каталога. Подсказки показывают до 100 имён.")}</p></div>
<div class="module-field" data-ag-form-field><label for="phase">{tr("Этап")}</label><select id="phase" name="phase"><option value="before">{tr("До запроса")}</option>
<option value="after">{tr("После ответа")}</option></select></div>
<div class="module-field" data-ag-form-field><label for="binding">{tr("Локальный модуль")}</label><select id="binding" name="module">{hook_options}</select></div>
<button{bind_disabled}>{tr("Сохранить привязку")}</button></form></section>
<section aria-labelledby="run"><h2 id="run">{tr("Ручной запуск")}</h2>
<form class="module-form" method="post" action="/modules" data-ag-form-row="stack">{self.hidden("run", revision)}
<div class="module-field" data-ag-form-field><label for="run-module">{tr("Модуль")}</label><select id="run-module" name="id" required{disabled}>{module_options}</select></div>
<div class="module-field" data-ag-form-field><label for="run-data">{tr("Входной JSON")}</label><textarea id="run-data" name="data" rows="4" maxlength="65536" required{disabled}>{{"value": 42}}</textarea></div>
<button{disabled}>{tr("Запустить модуль")}</button></form>{preview}</section>{self.lifecycle(current, local)}</div></main></html>"""
        return HTMLResponse(html, status_code=status, headers=SECURE_HTML)

    async def overview(self, request):
        """Open the library or edit one selected module."""
        return self.render(request.query_params.get("edit", "")[:32], tool_hint=request.query_params.get("tool", "")[:128])

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
        try:
            action = fields.get("action")
            revision = self.revision(fields.get("revision", ""))
            if action == "open":
                opened = await self.service.open_session()
                return self.render(notice="Сессия открыта. Сохраните ключ для клиента.", result={"result": opened})
            if action == "run":
                query = Run.model_validate({"id": fields.get("id"), "data": parse_json(fields.get("data", "")),
                                            "expected_revision": revision})
                return self.render(notice="Модуль выполнен.", result=await self.service.run(query))
            await self.edit(action, fields, revision)
        except ValidationError:
            return self.render(notice="Проверьте поля и определение модуля.", status=400)
        except ModuleError as error:
            return self.render(notice=module_error(str(error)), status=400)
        except OSError:
            return self.render(notice="Не удалось сохранить настройки.", status=503)
        return RedirectResponse("/modules", status_code=303, headers=SECURE_HTML)

    @staticmethod
    def revision(value: str) -> int:
        """Parse a bounded revision from one form field."""
        if not value.isascii() or not value.isdecimal() or len(value) > 20:
            raise ModuleError("Некорректная ревизия.")
        return int(value)

    async def edit(self, action, fields: dict[str, str], revision: int):
        """Translate one form action into the same typed edit used by Bridge."""
        payload = {"expected_revision": revision}
        if action == "save":
            payload["module"] = parse_json(fields.get("data", ""))
            await self.service.save(Save.model_validate(payload))
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
