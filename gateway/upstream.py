"""Own one MCP session per configured upstream and discover its tool catalog."""

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta

import httpx
from anyio import ClosedResourceError, EndOfStream
from anyio.abc import ObjectReceiveStream
from mcp import StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

from .connection_diagnostics import ConnectionProbe
from .mcp_client import CatalogSession
from .profile import HttpBackend, StdioBackend, secret
from .trace_context import inject_http_trace

_LOG = logging.getLogger("gateway")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class ObservedReceiveStream(ObjectReceiveStream):
    """Expose SDK transport EOF without consuming messages outside ClientSession."""

    def __init__(self, source: ObjectReceiveStream, closed: asyncio.Event):
        """Pair the receive channel with an event owned by the backend worker."""
        self.source = source
        self.closed = closed

    async def receive(self):
        """Read one message or signal that the underlying transport can no longer deliver."""
        try:
            return await self.source.receive()
        except (EndOfStream, ClosedResourceError):
            self.closed.set()
            raise

    async def aclose(self):
        """Release the receive channel when the SDK closes its session."""
        await self.source.aclose()


class EventStreamPacing:
    """Bound cursorless notification stream reconnects on one HTTP session."""

    def __init__(self, minimum_interval: float = 1.0):
        """Space cursorless notification GETs by at least the given interval in seconds."""
        self.minimum_interval = minimum_interval
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    async def request(self, request: httpx.Request):
        """Apply the idle timeout only where the SDK cannot be resuming a tool response."""
        # SDK response resumption always carries Last-Event-ID. The same header can
        # also appear on a notification stream, so leave all cursor-bearing GETs alone.
        if request.method != "GET" or "last-event-id" in request.headers:
            return
        timeout = request.extensions.get("timeout", {})
        read_timeout = timeout.get("read")
        if read_timeout is not None:
            request.extensions["timeout"] = {**timeout, "read": max(read_timeout, 300)}
        clock = asyncio.get_running_loop().time
        async with self.lock:
            remaining = self.next_start - clock()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.next_start = clock() + self.minimum_interval


class McpHttpClient(httpx.AsyncClient):
    """Give each detached MCP HTTP request task a total lifetime in seconds."""

    def __init__(self, rpc_timeout: float, **options):
        """Retain HTTP inactivity timeouts as well as the JSON-RPC deadline."""
        super().__init__(timeout=rpc_timeout, **options)
        self.rpc_timeout = rpc_timeout

    def build_request(self, method, url, **kwargs) -> httpx.Request:
        """Start the deadline on the SDK's own POST task before it sends the request."""
        request = super().build_request(method, url, **kwargs)
        message = kwargs.get("json")
        if request.method == "POST" and isinstance(message, dict) and "method" in message and "id" in message:
            # The pinned SDK runs each JSON-RPC request in a separate task. Bound
            # that task through SSE heartbeats and resumption, independently of
            # the gateway caller waiting for a reply. Responses to server
            # requests, notifications and idle GETs run on longer-lived tasks.
            task = asyncio.current_task()
            deadline = asyncio.get_running_loop().call_later(self.rpc_timeout, task.cancel)
            task.add_done_callback(lambda _completed: deadline.cancel())
        return request


@asynccontextmanager
async def open_session(config, timeout: float, authorization: str | None = None,
                       probe: ConnectionProbe | None = None):
    """Open the selected SDK transport and keep its contexts on the owning task."""
    probe = probe or ConnectionProbe()
    async with AsyncExitStack() as stack:
        if isinstance(config, StdioBackend):
            params = StdioServerParameters(command=config.command, args=config.args, env=stdio_environment(config))
            # Child stderr may contain credentials or payloads. Keep the gateway journal metadata-only.
            sink = stack.enter_context(open(os.devnull, "w"))
            streams = await stack.enter_async_context(stdio_client(params, errlog=sink))
        else:
            assert isinstance(config, HttpBackend)
            if authorization is None:
                token = secret(config.bearer_token_env)
                authorization = f"Bearer {token}" if token else None
            headers = {"Authorization": authorization} if authorization else {}
            pacing = EventStreamPacing()
            request_hooks = [pacing.request]
            if config.forward_trace_context:
                request_hooks.append(inject_http_trace)
            hooks = {"response": [probe.response], "request": request_hooks}
            client = await stack.enter_async_context(McpHttpClient(
                timeout, headers=headers, follow_redirects=False, trust_env=not config.loopback,
                event_hooks=hooks,
            ))
            streams = await stack.enter_async_context(streamable_http_client(config.url, http_client=client))
        incoming = ObservedReceiveStream(streams[0], probe.closed)
        session = CatalogSession(incoming, streams[1], read_timeout_seconds=timedelta(seconds=timeout),
                                message_handler=probe.message)
        await stack.enter_async_context(session)
        yield session, incoming.closed


def stdio_environment(config: StdioBackend) -> dict[str, str]:
    """Resolve only the environment overrides explicitly configured for this child."""
    environment = {}
    for target, source in config.env_from.items():
        if source not in os.environ:
            raise ValueError("missing upstream environment variable")
        environment[target] = os.environ[source]
    return environment


async def docker_status(command: str, config: StdioBackend) -> str | None:
    """Check the Docker engine from the same environment as the MCP command."""
    try:
        environment = get_default_environment() | stdio_environment(config)
    except ValueError:
        return "upstream-env-missing"
    try:
        options = docker_endpoint_options(config.args) if command == config.command else []
        process = await asyncio.create_subprocess_exec(
            command, *options, "info", env=environment, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return "docker-cli-missing"
    except OSError:
        return "docker-check-failed"
    try:
        async with asyncio.timeout(3):
            result = await process.wait()
    except TimeoutError:
        return "docker-required"
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    return None if result == 0 else "docker-required"


def docker_endpoint_options(args: list[str]) -> list[str]:
    """Carry Docker's global endpoint and TLS flags up to the subcommand."""
    options = []
    values = {"-H", "--host", "-c", "--context", "--config", "--tlscacert",
              "--tlscert", "--tlskey", "-l", "--log-level"}
    switches = {"--tls", "--tlsverify", "-D", "--debug"}
    index = 0
    while index < len(args):
        argument = args[index]
        flag, separator, _ = argument.partition("=")
        if flag in switches or (separator and flag in values):
            options.append(argument)
        elif argument in values and index + 1 < len(args):
            options.extend(args[index:index + 2])
            index += 1
        elif argument.startswith(("-H", "-c", "-l")) and len(argument) > 2:
            options.append(argument)
        else:
            break
        index += 1
    return options


async def discover(session: CatalogSession, prefix: str):
    """Read paginated tools and reject ambiguous names or an unbounded catalog."""
    tools = {}
    cursor = None
    seen_cursors = set()
    for _ in range(64):
        page = await session.catalog_page(cursor)
        for tool in page.tools:
            name = f"{prefix}.{tool.name}"
            if len(name) > 128 or not _TOOL_NAME.fullmatch(tool.name):
                raise ValueError("invalid upstream tool name")
            if name in tools:
                raise ValueError("duplicate upstream tool name")
            tools[name] = tool
            if len(tools) > 2000:
                raise ValueError("upstream catalog exceeds 2000 tools")
        cursor = page.nextCursor
        if cursor is None:
            return tools
        if cursor in seen_cursors:
            raise ValueError("repeated upstream cursor")
        seen_cursors.add(cursor)
    raise ValueError("upstream catalog exceeds 64 pages")


async def refresh_catalog(session, prefix, timeout, publish, tools, changed, closed):
    """Publish only complete changed catalogs and retain the old one on discovery failure."""
    while not closed.is_set():
        await changed.wait()
        # Fold notification bursts into one discovery without blocking the SDK reader.
        await asyncio.sleep(0.1)
        changed.clear()
        if closed.is_set():
            return
        try:
            async with asyncio.timeout(timeout):
                updated = await discover(session, prefix)
        except Exception:
            if not closed.is_set():
                _LOG.warning("Catalog refresh failed for %s; retaining existing tools", prefix)
            continue
        if not closed.is_set() and updated != tools:
            publish(prefix, session, updated, "ready")
            tools = updated


async def run_backend(prefix, config, timeout, publish, authorization: str | None = None, *,
                      probe: ConnectionProbe | None = None):
    """Keep the discovered session alive and publish a sanitized failure on exit."""
    probe = probe or ConnectionProbe()
    failure_published = False
    try:
        async with open_session(config, timeout, authorization, probe) as (session, closed):
            try:
                async with asyncio.timeout(timeout):
                    await session.initialize()
                    tools = await discover(session, prefix)
                if not closed.is_set():
                    publish(prefix, session, tools, "ready")
                    watcher = asyncio.create_task(
                        refresh_catalog(session, prefix, timeout, publish, tools, probe.tools_changed, closed)
                    )
                    try:
                        await closed.wait()
                    finally:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
            except Exception as error:
                status = probe.failure(error)
            else:
                status = probe.failure(None)
            # Withdraw the route before SDK cleanup, which can await a remote DELETE.
            publish(prefix, None, {}, status)
            failure_published = True
    except Exception as error:
        if not failure_published:
            publish(prefix, None, {}, probe.failure(error))
