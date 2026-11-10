"""Render the read-only catalog and metrics as escaped, responsive HTML."""

from html import escape

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
a, input, button { color: inherit; }
a:focus-visible, input:focus-visible, button:focus-visible { outline: 3px solid #3174c2; }
a { padding: .4rem 0; }
form { justify-content: start; align-items: center; }
input, button { font: inherit; border: 1px solid #64768b; border-radius: .45rem;
  padding: .4rem .65rem; background: transparent; max-width: 100%; box-sizing: border-box; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit,minmax(min(100%,9rem),1fr));
  gap: .65rem; margin: .8rem 0; }
.metrics div, .tools li, .backends li { background: #fff; border: 1px solid #d2dce7;
  border-radius: .55rem; padding: .65rem .8rem; }
dt { font-size: .88rem; }
dd { margin: 0; font-weight: 680; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
ul { padding: 0; margin: .7rem 0; list-style: none; display: grid; gap: .45rem; }
li, h1, header p { overflow-wrap: anywhere; }
.tools li p { white-space: pre-wrap; margin-top: .2rem; }
small, .muted { color: #445770; }
@media (prefers-color-scheme: dark) {
  body { color: #e7eef9; background: #151e2a; }
  .metrics div, .tools li, .backends li { background: #202d3c; border-color: #52667e; }
  small, .muted { color: #bacbe1; }
}
"""

_ENVIRONMENTS = {"test": "Тестовая среда", "production": "Production"}

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
    policy = "Запрещён" if tool["denied"] else "Доступен"
    if tool["singleflight"]:
        policy += " · singleflight"
    return (
        f"<li><strong>{escape(name)}</strong> <small>{policy}</small>"
        f"<p>{escape(tool['description'])}</p>"
        f"<small>Запросы {display(counts.get('received', 0))} · "
        f"upstream {display(counts.get('upstream', 0))} · "
        f"средняя задержка {display(counts.get('latency_mean_ms'))} мс</small></li>"
    )


def render(status: dict, query: str) -> str:
    """Render one dashboard snapshot without executing upstream text or links."""
    label = _ENVIRONMENTS.get(status.get("environment"))
    environment = f" · {label}" if label else ""
    metrics = status["metrics"]
    cards = "".join(
        f"<div><dt>{label}</dt><dd>{display(metrics['total'][key])}</dd></div>"
        for key, label in _METRICS.items()
    )
    backends = "".join(
        f"<li><strong>{escape(name)}</strong> · {escape(state)}</li>"
        for name, state in status["backends"].items()
    )
    term = query.casefold()
    rows = []
    for tool in status["catalog"]:
        if term in tool["name"].casefold() or term in tool["description"].casefold():
            rows.append(tool_row(tool, metrics["tools"]))
    catalog = "".join(rows) or "<li>Инструменты не найдены.</li>"
    return f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ag-mcp-gateway</title><link rel="stylesheet" href="/dashboard.css">
<main>
<header><div><h1>ag-mcp-gateway</h1><p>Профиль {escape(status['profile'])}{environment}</p></div>
<a href="/">Обновить обзор</a></header>
<section aria-labelledby="metrics"><h2 id="metrics">Вызовы с момента запуска</h2>
<dl class="metrics">{cards}</dl>
<p class="muted">Оценка токенов по размеру JSON. Ошибки записи журнала {metrics["journal_errors"]}.</p>
</section>
<section aria-labelledby="backends"><h2 id="backends">MCP-серверы</h2>
<ul class="backends">{backends}</ul></section>
<section aria-labelledby="catalog"><h2 id="catalog">Каталог инструментов · {len(rows)}</h2>
<form action="/" method="get">
<label for="q">Имя или описание</label>
<input id="q" name="q" maxlength="200" value="{escape(query, quote=True)}">
<button type="submit">Найти</button>
</form>
<ul class="tools">{catalog}</ul></section>
</main></html>"""
