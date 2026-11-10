"""Select and persist the language of the server-rendered browser pages."""

from contextvars import ContextVar
from dataclasses import dataclass
from html import escape
from http.cookies import SimpleCookie
from urllib.parse import urlencode

from starlette.datastructures import MutableHeaders
from starlette.requests import Request

from .ui_messages import ENGLISH, MODULE_ERRORS_RU

_NAMES = {"ru": "Русский", "en": "English"}
_COOKIE = "amg_ui_lang"
_FILTERS = {"q": 200, "backend": 32, "offset": 6, "edit": 32, "tool": 128, "trace_id": 32, "epoch": 128, "before": 19}
_PAGES = {"/", "/settings", "/modules", "/credentials", "/diagnostics"}


@dataclass(frozen=True)
class PageLocale:
    """Hold only browser presentation state for the current ASGI request."""

    language: str
    path: str = "/"
    filters: tuple[tuple[str, str], ...] = ()


_locale = ContextVar("gateway_page_locale", default=PageLocale("ru"))


def language() -> str:
    """Return the selected HTML language without changing the MCP contract."""
    return _locale.get().language


def tr(message: str) -> str:
    """Translate an owned Russian UI label. Keep protocol names and brands literal."""
    return ENGLISH.get(message, message) if language() == "en" else message


def module_error(message: str) -> str:
    """Localize known module errors without reflecting arbitrary exception text."""
    if message in MODULE_ERRORS_RU:
        return MODULE_ERRORS_RU[message] if language() == "ru" else message
    if message in ENGLISH:
        return tr(message)
    return tr("Не удалось выполнить операцию модуля.")


def language_switch(*, backend: str | None = None) -> str:
    """Render alternate links, retaining a validated backend selected by the page."""
    page = _locale.get()
    filters = dict(page.filters)
    if backend is not None:
        filters["backend"] = backend
    items = []
    for code, name in _NAMES.items():
        url = escape(f"{page.path}?{urlencode({**filters, 'lang': code})}", quote=True)
        current = ' aria-current="true"' if code == page.language else ""
        items.append(f'<a href="{url}" lang="{code}" hreflang="{code}" rel="alternate"{current}>{name}</a>')
    return f'<nav class="language-switch" aria-label="{tr("Язык интерфейса")}">{"".join(items)}</nav>'


def _browser_language(header: str) -> str:
    """Pick the highest-weight supported browser language, with Russian as fallback."""
    choice, best = "ru", 0.0
    for entry in header[:2048].split(","):
        tag, _, options = entry.partition(";")
        code = tag.strip().lower().partition("-")[0]
        if code not in _NAMES:
            continue
        try:
            option = options.strip()
            weight = float(option[2:]) if option.startswith("q=") else 1.0
        except ValueError:
            continue
        if best < weight <= 1:
            choice, best = code, weight
    return choice


def _browser_page(path: str) -> str | None:
    """Map browser actions back to their page and exclude protocol and collector routes."""
    if path in _PAGES:
        return path
    if path.startswith("/credentials/"):
        return "/credentials"
    if path.startswith("/reconnect/"):
        return "/"
    return None


class BrowserLocale:
    """Isolate request-local rendering and write preferences only for HTML GETs."""

    def __init__(self, app):
        """Wrap the app inside the existing loopback and authorization boundary."""
        self.app = app

    async def __call__(self, scope, receive, send):
        """Choose language from an explicit selection, cookie, browser hint or default."""
        page = _browser_page(scope.get("path", "")) if scope["type"] == "http" else None
        if page is None:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        explicit = request.query_params.get("lang") if request.method == "GET" else None
        selected = request.cookies.get(_COOKIE)
        if explicit in _NAMES:
            selected = explicit
        if selected not in _NAMES:
            selected = _browser_language(request.headers.get("accept-language", ""))
        filters = tuple((key, request.query_params[key][:limit])
                        for key, limit in _FILTERS.items() if key in request.query_params)
        token = _locale.set(PageLocale(selected, page, filters))

        async def localized_send(message):
            """Add language metadata and a non-sensitive cookie to localized HTML."""
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").startswith("text/html"):
                    headers["Content-Language"] = selected
                    headers.add_vary_header("Accept-Language, Cookie")
                    if explicit in _NAMES:
                        cookie = SimpleCookie()
                        cookie[_COOKIE] = selected
                        item = cookie[_COOKIE]
                        item["path"], item["max-age"], item["samesite"], item["httponly"] = "/", 31536000, "lax", True
                        headers.append("Set-Cookie", item.OutputString())
            await send(message)

        try:
            await self.app(scope, receive, localized_send)
        finally:
            _locale.reset(token)
