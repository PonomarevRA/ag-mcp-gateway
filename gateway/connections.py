"""Own upstream connection generations and rotate one Basic credential at a time."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp import ClientSession, types

from .connection_diagnostics import ConnectionProbe, FAILURE_LABELS
from .credentials import SecretStore, SecretStoreError, credential_account
from .profile import HttpBackend, Profile, StdioBackend
from .token_secrets import resolve_token
from .upstream import docker_status, run_backend


class ReauthorizationError(RuntimeError):
    """Expose a fixed action failure code without upstream response details."""


_DOCKER_RETRY_SECONDS = 3


@dataclass
class Connection:
    """Hold one worker, its latest catalog and the drain state of its tool calls."""

    closed: asyncio.Event
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    session: ClientSession | None = None
    tools: dict[str, types.Tool] = field(default_factory=dict)
    status: str = "starting"
    awaiting_docker: bool = False
    requests: int = 0

    def __post_init__(self):
        """Mark a newly allocated connection idle until it dispatches a tool."""
        self.idle.set()

    @property
    def live(self) -> bool:
        """Return true while the initialized session can receive requests, even before worker cleanup."""
        return self.session is not None and not self.closed.is_set()

    def acquire(self):
        """Hold the worker alive through one in-flight dispatch."""
        self.requests += 1
        self.idle.clear()

    def release(self):
        """Signal that a superseded worker can close after its final dispatch."""
        self.requests -= 1
        if self.requests == 0:
            self.idle.set()


class BackendConnections:
    """Load configured sessions, verify candidates and publish only active generations."""

    def __init__(self, profile: Profile, publish, store: SecretStore | None, profile_file: str, *, technical=None):
        """Bind credential identities and generation state to one immutable profile."""
        self.profile = profile
        self.publish = publish
        self.store = store
        self.technical = technical
        self.accounts = {}
        for name, backend in profile.backends.items():
            if isinstance(backend, HttpBackend) and backend.basic_keychain:
                self.accounts[name] = credential_account(profile_file, profile.endpoint_key, name, backend.url)
        self.active: dict[str, Connection] = {}
        self.workers: set[asyncio.Task] = set()
        self.rotations: dict[str, asyncio.Task] = {}
        self.closing = True

    @asynccontextmanager
    async def running(self):
        """Start all workers and wait only for MCPs required by profile startup."""
        self.closing = False
        try:
            required = []
            for name, config in self.profile.backends.items():
                worker = self._start(name)
                self.active[name] = worker
                # A repeated ASGI lifespan must clear routes from the previous workers.
                self.publish(name, worker)
                if not config.background_startup:
                    required.append(worker)
            await asyncio.gather(*(worker.ready.wait() for worker in required))
            yield
        finally:
            self.closing = True
            await asyncio.gather(*self.rotations.values(), return_exceptions=True)
            workers = tuple(self.workers)
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self.active.clear()

    def _track(self, operation) -> asyncio.Task:
        """Keep a background transport or retirement task until it completes."""
        task = asyncio.create_task(operation)
        self.workers.add(task)
        task.add_done_callback(self.workers.discard)
        return task

    def _start(self, name: str, authorization: str | None = None, *, wait_for_docker: bool = True) -> Connection:
        """Start a candidate without changing active routes or stored credentials."""
        probe = ConnectionProbe()
        config = self.profile.backends[name]
        docker = config.docker_cli if isinstance(config, StdioBackend) else None
        worker = Connection(closed=probe.closed, awaiting_docker=wait_for_docker and docker is not None)

        def update(_name, session, tools, status):
            """Settle discovery and apply later events only for this active worker."""
            worker.session = session
            worker.tools = tools
            worker.status = status
            worker.awaiting_docker = status == "docker-required" and wait_for_docker
            if self.active.get(name) is worker:
                self.publish(name, worker)
            worker.ready.set()

        async def run():
            """Load the selected saved authentication before starting the transport."""
            if docker is not None:
                while reason := await docker_status(docker, config):
                    if worker.status != reason:
                        update(name, None, {}, reason)
                    if reason != "docker-required" or not wait_for_docker:
                        return
                    await asyncio.sleep(_DOCKER_RETRY_SECONDS)
                if worker.status == "docker-required":
                    update(name, None, {}, "starting")
                # Keep reconnect serialized through initial MCP discovery.
                worker.awaiting_docker = wait_for_docker
            header = authorization
            if name in self.accounts and header is None:
                try:
                    header = await self._read(name)
                except ReauthorizationError as error:
                    update(name, None, {}, str(error))
                    return
            if isinstance(config, HttpBackend) and config.bearer_token_keychain:
                try:
                    token = await asyncio.to_thread(
                        resolve_token, config.bearer_token_keychain, config.bearer_token_env, self.store)
                except SecretStoreError:
                    update(name, None, {}, "keychain-unavailable")
                    return
                except ValueError:
                    update(name, None, {}, "credentials-missing")
                    return
                header = f"Bearer {token}"
            await run_backend(name, config, self.profile.timeout_seconds, update, header, probe=probe)

        worker.task = self._track(run())
        return worker

    async def _read(self, name: str) -> str:
        """Resolve a Basic header from the protected store and distinguish absence."""
        if self.store is None:
            raise ReauthorizationError("keychain-unavailable")
        try:
            header = await asyncio.to_thread(self.store.read, self.accounts[name])
        except SecretStoreError as error:
            raise ReauthorizationError("keychain-unavailable") from error
        if header is None:
            raise ReauthorizationError("credentials-missing")
        return header

    async def rotate(self, name: str, authorization: str | None) -> bool:
        """Replace one selected backend, shielding its transaction from browser cancellation."""
        if self.closing or name not in self.accounts:
            raise ReauthorizationError("not-enabled")
        if self.store is None:
            raise ReauthorizationError("keychain-unavailable")
        return await self._run_candidate(name, authorization)

    async def verify_saved(self, name: str) -> bool:
        """Check saved Basic credentials on a disposable connection."""
        if name not in self.accounts:
            raise ReauthorizationError("not-enabled")
        return await self._run_candidate(name, None, replace=False)

    async def reconnect(self, name: str) -> bool:
        """Open the configured transport again with its current saved authentication."""
        current = self.active.get(name)
        config = self.profile.backends.get(name)
        if (not self.closing and current is not None and current.ready.is_set()
                and not current.live and not current.awaiting_docker and name not in self.rotations
                and isinstance(config, StdioBackend) and config.docker_cli is not None):
            return await self._restart_docker_backend(name, current)
        return await self._run_candidate(name, None)

    async def _restart_docker_backend(self, name: str, previous: Connection) -> bool:
        """Replace a failed Docker worker and retain its dependency wait after this call."""
        worker = self._start(name)
        self.active[name] = worker
        self.publish(name, worker)
        self._track(self._retire(previous))
        await worker.ready.wait()
        if not worker.live:
            raise ReauthorizationError(worker.status if worker.status in FAILURE_LABELS else "connection-failed")
        return True

    async def _run_candidate(self, name: str, authorization: str | None, *, replace: bool = True) -> bool:
        """Serialize connection checks and replacements for each backend."""
        if self.closing or name not in self.active:
            raise ReauthorizationError("not-enabled")
        current = self.active[name]
        if current.awaiting_docker:
            reason = current.status if current.status == "docker-required" else "busy"
            raise ReauthorizationError(reason)
        if name in self.rotations:
            raise ReauthorizationError("busy")
        task = asyncio.create_task(self._connect_candidate(name, authorization, replace=replace))
        self.rotations[name] = task
        # A disconnected caller must not interrupt a verified replacement or Keychain write.
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
        return await asyncio.shield(task)

    async def _connect_candidate(self, name: str, authorization: str | None, *, replace: bool) -> bool:
        """Verify a candidate, then close it or publish it after any required Keychain write."""
        candidate = None
        activated = False
        try:
            candidate = self._start(name, authorization, wait_for_docker=False)
            await candidate.ready.wait()
            if not candidate.live:
                reason = candidate.status
                if reason not in FAILURE_LABELS or reason == "starting":
                    reason = "connection-failed"
                raise ReauthorizationError(reason)
            if not replace:
                return True
            if authorization is not None:
                try:
                    await asyncio.to_thread(self.store.write, self.accounts[name], authorization)
                except SecretStoreError as error:
                    raise ReauthorizationError("keychain-unavailable") from error
            # The candidate can close while the system credential write is pending.
            # Keep the old routes and leave the verified secret available for a new connection.
            if not candidate.live:
                if self.technical is not None:
                    self.technical.connection(name, candidate.status, replacement=True)
                return False
            old = self.active[name]
            self.active[name] = candidate
            activated = True
            self.publish(name, candidate)
            self._track(self._retire(old))
            if self.technical is not None:
                self.technical.connection(name, "ready", replacement=True)
            return True
        except ReauthorizationError as error:
            if replace and self.technical is not None:
                self.technical.connection(name, str(error), replacement=True)
            raise
        finally:
            if candidate is not None and not activated:
                await self._stop(candidate)
            self.rotations.pop(name, None)

    async def _retire(self, worker: Connection):
        """Let already-dispatched tool calls settle without replaying them."""
        await worker.idle.wait()
        await self._stop(worker)

    async def _stop(self, worker: Connection):
        """Close a worker on its owning task and await transport cleanup."""
        task = worker.task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
