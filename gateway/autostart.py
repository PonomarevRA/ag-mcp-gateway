"""Start one local profile for an opt-in stdio client on macOS."""

import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep

from filelock import FileLock, Timeout

_START_TIMEOUT = 20
_LSOF = "/usr/sbin/lsof"


def _inspect(command: list[str]) -> str:
    """Read a bounded operating-system process or listener query."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("could not verify the local gateway process in time") from None
    if result.returncode not in (0, 1):
        raise ValueError("could not verify the local gateway process")
    return result.stdout.strip()


def _trusted_binary(path: Path, executable: Path) -> bool:
    """Accept the current executable or a sibling gateway release from the local installer."""
    if path == executable:
        return True
    releases = Path.home() / "Library/Application Support/ag-mcp-gateway/releases"
    return (executable.name == path.name == "ag-mcp-gateway"
            and executable.parent.parent == path.parent.parent == releases.resolve())


def _profile_owns_port(owners: set[str], query: list[str], state_dir: Path, executable: Path) -> bool:
    """Require the same PID on the port and the selected profile's state lock."""
    if len(owners) != 1:
        return False
    pid, = owners
    if not _is_gateway(pid, executable):
        return False
    return _lock_owners(state_dir) == {pid} and set(_inspect(query).splitlines()) == {pid}


def _is_gateway(pid: str, executable: Path) -> bool:
    """Verify the process owner and an installed gateway executable."""
    uid = _inspect(["/bin/ps", "-p", pid, "-o", "uid="])
    program = _inspect(["/bin/ps", "-ww", "-p", pid, "-o", "comm="])
    return (uid == str(os.getuid()) and program.startswith("/")
            and _trusted_binary(Path(program).resolve(), executable))


def _lock_owners(state_dir: Path) -> set[str]:
    """Read PIDs holding the persistent file open without deleting the lock file."""
    path = state_dir / "gateway.lock"
    return set(_inspect([_LSOF, "-t", str(path)]).splitlines()) if path.is_file() else set()


def _accepts_loopback(port: int) -> bool:
    """Probe TCP readiness without sending an HTTP request or credentials."""
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _ready(port: int, state_dir: Path, executable: Path) -> bool:
    """Allow socket and process snapshots to settle across a concurrent bind."""
    query = [_LSOF, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"]
    for _ in range(3):
        owners = set(_inspect(query).splitlines())
        if owners:
            if _profile_owns_port(owners, query, state_dir, executable):
                return True
        elif not _accepts_loopback(port):
            return False
        sleep(0.1)
    raise ValueError(f"port {port} is occupied by an unverified process")


@contextmanager
def startup_lock(state_dir: Path):
    """Serialize on-demand startup and port changes for one local profile."""
    if sys.platform != "darwin":
        raise ValueError("local gateway lifecycle control is available on macOS")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with FileLock(str(state_dir / "autostart.lock"), timeout=_START_TIMEOUT + 15):
            yield
    except Timeout:
        raise ValueError("timed out waiting for another gateway client to finish starting the profile") from None


def start_locked(port: int, state_dir: Path, command: list[str]) -> None:
    """Start or reuse a detached daemon while the caller holds the profile startup lock."""
    executable = Path(command[0]).resolve()
    if _ready(port, state_dir, executable):
        return
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    deadline = monotonic() + _START_TIMEOUT
    try:
        while monotonic() < deadline:
            if _ready(port, state_dir, executable):
                return
            if process.poll() is not None and not _lock_owners(state_dir):
                raise ValueError(f"gateway process exited before opening port {port}")
            sleep(0.15)
        raise ValueError(f"gateway did not start on port {port} within {_START_TIMEOUT} seconds")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise


def _running_profile(state_dir: Path, executable: Path, name: str) -> tuple[str, str, int] | None:
    """Identify the live profile by its lock owner, process start time and loopback socket."""
    owners = _lock_owners(state_dir)
    if not owners:
        return None
    if len(owners) == 1:
        pid, = owners
        started = _inspect(["/bin/ps", "-p", pid, "-o", "lstart="])
        command = _inspect(["/bin/ps", "-ww", "-p", pid, "-o", "command="])
        sockets = _inspect([_LSOF, "-a", "-nP", "-p", pid, "-iTCP", "-sTCP:LISTEN", "-Fn"])
        loopback = "n127.0.0.1:"
        ports = {line.removeprefix(loopback) for line in sockets.splitlines() if line.startswith(loopback)}
        if started and command.endswith(f" --profile {name} serve") and _is_gateway(pid, executable) and len(ports) == 1:
            port, = ports
            if port.isascii() and port.isdecimal():
                return pid, started, int(port)
    raise ValueError("could not verify the process holding this profile's lock")


def _stop_verified_profile(state_dir: Path, executable: Path, name: str, running: tuple[str, str, int]) -> None:
    """Signal only the previously verified process and wait for its state lock to be released."""
    if _running_profile(state_dir, executable, name) != running:
        raise ValueError("profile process changed before it could be stopped")
    pid, _, _ = running
    os.kill(int(pid), signal.SIGTERM)
    deadline = monotonic() + 15
    while _lock_owners(state_dir):
        if monotonic() >= deadline:
            raise ValueError("profile did not release its lock")
        sleep(0.15)


def stop_running_profile(state_dir: Path, name: str, executable: Path) -> None:
    """Stop a profile after the operator requests it, including after the app has reopened."""
    executable = executable.resolve()
    with startup_lock(state_dir):
        running = _running_profile(state_dir, executable, name)
        if running is not None:
            _stop_verified_profile(state_dir, executable, name, running)


def change_running_port(state_dir: Path, name: str, port: int, command: Callable[[int], list[str]],
                        commit: Callable[[], None], rollback: Callable[[], None],
                        check_credentials: Callable[[], None]) -> bool:
    """Restart a verified live profile, or just save its port when it is stopped."""
    executable = Path(command(port)[0]).resolve()
    with startup_lock(state_dir):
        running = _running_profile(state_dir, executable, name)
        if running is not None and running[2] == port:
            commit()
            return True
        if _inspect([_LSOF, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"]) or _accepts_loopback(port):
            raise ValueError(f"port {port} is already in use; port was not changed")
        if running is None:
            commit()
            return False
        check_credentials()
        _stop_verified_profile(state_dir, executable, name, running)
        _, _, old_port = running
        try:
            commit()
            start_locked(port, state_dir, command(port))
        except (ValueError, OSError) as error:
            try:
                rollback()
                start_locked(old_port, state_dir, command(old_port))
            except (ValueError, OSError):
                raise ValueError("port change failed and the old service could not be restored") from None
            raise ValueError(f"port change failed; the old service was restored ({error})") from None
        return True
