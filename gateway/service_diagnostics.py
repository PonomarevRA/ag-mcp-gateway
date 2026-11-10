"""Collect local process measurements and profile counters for pull-based monitoring."""

import ctypes
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

APPLICATION = "ag-mcp-gateway"
SCHEMA = "service-diagnostics/v1"
_STARTED = monotonic()
_STORAGE_TTL_SECONDS = 5


def _resident_bytes() -> int | None:
    """Read current RSS from the macOS or Linux process interface."""
    if sys.platform == "darwin":
        proc_pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
        proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        proc_pidinfo.restype = ctypes.c_int
        # PROC_PIDTASKINFO returns a 96-byte proc_taskinfo with resident_size at byte eight.
        info = (ctypes.c_uint64 * 12)()
        size = ctypes.sizeof(info)
        if proc_pidinfo(os.getpid(), 4, 0, info, size) == size:
            return info[1]
    elif sys.platform.startswith("linux"):
        with Path("/proc/self/statm").open(encoding="ascii") as source:
            fields = source.read(1024).split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    return None


def _windows_resources() -> tuple[int | None, int | None, float | None, float | None]:
    """Return current and peak working set bytes and cumulative user and kernel CPU seconds."""
    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        """Native PROCESS_MEMORY_COUNTERS layout with pointer-sized byte counts."""

        _fields_ = [("size", wintypes.DWORD), ("page_faults", wintypes.DWORD),
                    ("peak", ctypes.c_size_t), ("current", ctypes.c_size_t),
                    ("pool", ctypes.c_size_t * 4), ("pagefile", ctypes.c_size_t * 2)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.GetCurrentProcess()
    counters = MemoryCounters()
    counters.size = ctypes.sizeof(counters)
    memory = kernel.K32GetProcessMemoryInfo
    memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    memory.restype = wintypes.BOOL
    resident = peak = None
    if memory(handle, ctypes.byref(counters), counters.size):
        resident, peak = counters.current, counters.peak

    times = (ctypes.c_uint64 * 4)()
    process_times = kernel.GetProcessTimes
    process_times.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
    process_times.restype = wintypes.BOOL
    pointers = [ctypes.byref(times, index * ctypes.sizeof(ctypes.c_uint64)) for index in range(4)]
    if process_times(handle, *pointers):
        return resident, peak, times[3] / 10_000_000, times[2] / 10_000_000
    return resident, peak, None, None


def _process_snapshot() -> dict:
    """Report process gauges independently where available, without disclosing native errors."""
    resident = peak = user = system = None
    if os.name == "nt":
        try:
            resident, peak, user, system = _windows_resources()
        except (AttributeError, OSError):
            pass
    else:
        import resource

        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
        except OSError:
            usage = None
        if usage is not None:
            peak = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
            user, system = usage.ru_utime, usage.ru_stime
        try:
            resident = _resident_bytes()
        except (AttributeError, IndexError, OSError, ValueError):
            pass
    return {
        "processId": os.getpid(),
        "residentBytes": resident,
        "peakResidentBytes": peak,
        "uptimeSeconds": monotonic() - _STARTED,
        "cpuUserSeconds": user,
        "cpuSystemSeconds": system,
    }


class Diagnostics:
    """Expose one profile's process, bounded journal storage and aggregate MCP counters."""

    def __init__(self, telemetry, technical):
        """Bind only gateway-owned journal paths and the shared in-process counters."""
        self.telemetry = telemetry
        self._paths = list(telemetry.journal.paths)
        if technical is not None:
            path = Path(technical.path)
            self._paths.append(path)
            for suffix in ("-journal", "-wal", "-shm"):
                self._paths.append(path.with_name(path.name + suffix))
        self._storage_expiry = 0
        self._journal_bytes = None

    def _storage_bytes(self) -> int | None:
        """Measure known files once every five seconds without following symlinks."""
        now = monotonic()
        if now < self._storage_expiry:
            return self._journal_bytes
        total = 0
        try:
            for path in self._paths:
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Journal is not a regular file")
                total += info.st_size
        except OSError:
            total = None
        self._journal_bytes = total
        self._storage_expiry = now + _STORAGE_TTL_SECONDS
        return total

    def snapshot(self) -> dict:
        """Build the diagnostics envelope with cumulative CPU seconds and gateway counters."""
        process = _process_snapshot()
        journal_bytes = self._storage_bytes()
        error = None
        if any(value is None for value in process.values()):
            error = "process_measurement_unavailable"
        elif journal_bytes is None:
            error = "journal_measurement_unavailable"
        gateway = self.telemetry.total.snapshot()
        gateway["journal_errors"] = self.telemetry.journal_errors
        return {
            "schemaVersion": SCHEMA,
            "application": APPLICATION,
            "collectedAt": datetime.now(timezone.utc).isoformat(),
            "process": process,
            "storage": {"journalBytes": journal_bytes, "dataBytes": None, "measurement": "journals-only"},
            "health": {"status": "degraded" if error else "ok", "errorCode": error},
            "gateway": gateway,
        }
