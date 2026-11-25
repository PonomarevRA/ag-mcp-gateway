#!/usr/bin/env python3
"""Reject operator profiles in Git trees before export or push."""

from hashlib import sha256
import json
from pathlib import PurePosixPath
import subprocess
import sys


# Updating a synthetic example requires reviewing the new bytes and their digest together.
SAMPLE_DIGESTS = {
    "profiles/company.example.json": "9928e8e1576916a6e0518f4bb7993862493b4a2c6251b2758ace2cd14130a371",
    "profiles/company-basic.example.json": "95283e3fe02d9929756d7746718c47f95ad750c95a5fd9fc8d2f890b20ff99e5",
    "profiles/company-test.example.json": "3c9ad82eca6089340c3185ac0ab5ad733d3096e444381183b2e8e8dfaceae053",
    "profiles/company-prod.example.json": "ab4a65812420fd87e0dcb4d8086a3d345c7fc684fbde22ede50dbf1fc199ffe2",
    "profiles/local-ag-tasks.example.json": "fb9ce4f1331ae1f54a79f1741cbc84e4a924aeebd4809f3e750e2462fdfa8324",
    "profiles/mock-tests.example.json": "8bf03eaae8edabf6e6c5207ad9e849a4528d1c79928b8ed541d1cbbe355a6d9d",
}


def check_profile_data(path: str, data: bytes) -> None:
    """Pin approved examples and detect Gateway profiles saved under other JSON filenames."""
    if path in SAMPLE_DIGESTS:
        if sha256(data).hexdigest() != SAMPLE_DIGESTS[path]:
            raise ValueError(f"Synthetic example differs from reviewed contents: {path!r}")
        return
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return
    if isinstance(value, dict) and "name" in value and "backends" in value:
        raise ValueError(f"Gateway profile belongs outside Git: {path!r}")


def git(*args: str) -> bytes:
    """Read local Git objects in the checkout from which the guard runs."""
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def check_entry(path: str, mode: str, oid: str) -> None:
    """Reject profile directories and Gateway-shaped JSON outside the approved examples."""
    parts = PurePosixPath(path.casefold()).parts
    if path in SAMPLE_DIGESTS:
        if mode not in {"100644", "100755"}:
            raise ValueError(f"Synthetic example must be a regular file: {path!r}")
        check_profile_data(path, git("cat-file", "blob", oid))
        return
    if path == "private/profiles/README.md" and mode == "100644":
        return
    if "profiles" in parts or parts[:2] == ("private", "runtime"):
        raise ValueError(f"Local profile data belongs outside Git: {path!r}")
    if path.casefold().endswith(".json") and mode in {"100644", "100755"}:
        check_profile_data(path, git("cat-file", "blob", oid))


def check_commits(revisions: list[str]) -> int:
    """Inspect every reachable commit in the supplied revision selection, caching checked entries."""
    commits = git("rev-list", *revisions).decode("ascii").splitlines()
    checked = set()
    for commit in commits:
        try:
            for record in git("ls-tree", "-rz", commit).split(b"\0"):
                if not record:
                    continue
                metadata, raw_path = record.split(b"\t", 1)
                mode, _kind, oid = metadata.decode("ascii").split()
                path = raw_path.decode("utf-8", "surrogateescape")
                entry = (path, mode, oid)
                if entry not in checked:
                    check_entry(*entry)
                    checked.add(entry)
        except ValueError as error:
            raise ValueError(f"{commit[:12]}: {error}") from error
    return len(commits)


def outgoing_revisions(lines) -> list[tuple[str, str | None]]:
    """Select commits sent by pre-push, accounting for new branches, tags and deletions."""
    ranges = []
    for line in lines:
        _local_ref, local, _remote_ref, remote = line.split()
        if not local.strip("0"):
            continue
        tip = git("rev-parse", "--verify", f"{local}^{{commit}}").decode("ascii").strip()
        base = None
        if remote.strip("0"):
            try:
                base = git("rev-parse", "--verify", f"{remote}^{{commit}}").decode("ascii").strip()
            except subprocess.CalledProcessError:
                pass
        ranges.append((tip, base))
    # Keep exclusions scoped to their own ref so one update cannot hide another's commits.
    return ranges


def main() -> int:
    """Audit all refs on demand, or inspect the refs supplied by Git's pre-push protocol."""
    try:
        if sys.argv[1:] == ["--audit"]:
            count = check_commits(["--all"])
            print(f"Profile guard: checked {count} commits.")
        elif len(sys.argv) == 3:
            for tip, base in outgoing_revisions(sys.stdin):
                check_commits([f"{base}..{tip}"] if base else [tip])
        else:
            raise ValueError("Use --audit, or install this script as the Git pre-push hook.")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Profile guard blocked: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
