"""Classify known transport failures without exposing upstream bodies or credentials."""

from asyncio import Event
from dataclasses import dataclass, field

import httpx
from mcp import types
from mcp.shared.exceptions import McpError

HTTP_FAILURES = {
    401: "auth-rejected",
    403: "access-denied",
    404: "invalid-endpoint",
    405: "invalid-endpoint",
}
FAILURE_LABELS = {
    "starting": ("Проверяем подключение", "pending"),
    "docker-required": ("Запустите Docker для подключения MCP", "attention"),
    "docker-cli-missing": ("Команда Docker не найдена", "error"),
    "docker-check-failed": ("Не удалось проверить доступ к Docker", "error"),
    "upstream-env-missing": ("Переменная окружения MCP не задана", "error"),
    "credentials-missing": ("Требуется вход", "attention"),
    "keychain-unavailable": ("Связка ключей недоступна", "attention"),
    "auth-rejected": ("Учётные данные отклонены · HTTP 401", "attention"),
    "access-denied": ("Сервер отказал в доступе · HTTP 403", "attention"),
    "invalid-endpoint": ("Проверьте MCP-адрес и транспорт", "error"),
    "invalid-mcp": ("Некорректный ответ MCP", "error"),
    "timeout": ("Истекло время ожидания", "error"),
    "unreachable": ("Сервер недоступен по сети", "error"),
    "upstream-http": ("Сервер вернул ошибку HTTP", "error"),
    "unavailable (command not found)": ("Команда MCP не найдена", "error"),
    "unavailable (connection closed)": ("Соединение закрыто", "error"),
}

DOCKER_RECOVERY = {
    "docker-required": ("start_docker", "Start Docker or restore access to its engine, then refresh tools/list."),
    "docker-cli-missing": ("check_docker_cli", "Make the Docker CLI available, then reconnect this MCP."),
    "docker-check-failed": ("check_docker_access", "Check Docker CLI permissions, then reconnect this MCP."),
}


def docker_recovery(backend: str, status: str) -> dict | None:
    """Describe a Docker dependency through fixed agent-facing codes and instructions."""
    recovery = DOCKER_RECOVERY.get(status)
    if recovery is None:
        return None
    action, message = recovery
    return {"backend": backend, "status": status, "action": action, "message": message}


@dataclass
class ConnectionProbe:
    """Observe the latest MCP HTTP response and SDK payload errors for one connection."""

    http_status: int | None = None
    invalid_message: bool = False
    terminal_http: int | None = None
    closed: Event = field(default_factory=Event)
    tools_changed: Event = field(default_factory=Event)

    async def response(self, response: httpx.Response):
        """Capture only the status of MCP POSTs, ignoring event-stream polling and cleanup."""
        if response.request.method != "POST":
            return
        status = response.status_code
        self.http_status = status
        self.invalid_message = False
        if self.terminal_http is None and status in {401, 403, 404}:
            self.terminal_http = status
            self.closed.set()

    async def message(self, item):
        """Record parser failures and coalesce SDK catalog notifications."""
        if isinstance(item, ValueError):
            self.invalid_message = True
        elif isinstance(item, types.ServerNotification) and isinstance(item.root, types.ToolListChangedNotification):
            self.tools_changed.set()

    def failure(self, error: BaseException | None) -> str:
        """Choose a stable reason from real transport facts, with a safe generic fallback."""
        status = self.terminal_http or self.http_status
        if status in HTTP_FAILURES:
            return HTTP_FAILURES[status]
        if status is not None and 300 <= status < 400:
            return "invalid-endpoint"
        if status is not None and status >= 400:
            return "upstream-http"
        if self.invalid_message:
            return "invalid-mcp"
        return self._failure_type(error)

    def _failure_type(self, error: BaseException | None) -> str:
        """Unwrap SDK task groups and classify only recognized exception types."""
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                reason = self._failure_type(child)
                if reason != "unavailable (connection failed)":
                    return reason
        if isinstance(error, (TimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(error, httpx.ConnectError):
            return "unreachable"
        if isinstance(error, FileNotFoundError):
            return "unavailable (command not found)"
        if isinstance(error, McpError):
            return "timeout" if error.error.code == 408 else "invalid-mcp"
        if error is None:
            return "unavailable (connection closed)"
        return "unavailable (connection failed)"
