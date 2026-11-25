"""Read and write a stable Git representation of synthetic MCP requests."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .mock_test_models import (
    MAX_COLLECTIONS, MAX_REQUESTS, GroupMetadata, GroupRecord, MockTestError, ScenarioRecord, ValidateScenario,
    encode_scenario_record, validation_issues,
)
from .mock_tests import MockTests
from .policy_settings import write_state
from .profile import Identifier

_MANIFEST = "mock-catalog.json"
_FORMAT = {"schema": "ag-mcp-gateway/mock-catalog", "version": 1}
_FOLDERS = ("collections", "requests")
_MAX_FILE = 1024 * 1024


class GitRequest(ValidateScenario):
    """Store a request and its collection membership without runtime revisions."""

    group_id: Identifier | None


@dataclass(frozen=True)
class GitCatalog:
    """Represent a checked set of portable groups and requests."""

    groups: dict[str, GroupMetadata]
    requests: dict[str, GitRequest]

    def counts(self) -> dict[str, int]:
        """Summarize the checked contents for the command output."""
        return {"collections": len(self.groups), "requests": len(self.requests)}


def _filename(identity: str) -> str:
    """Preserve case-distinct IDs even on case-insensitive and Windows filesystems."""
    return "id-" + "".join("~" + char.lower() if char.isupper() else char for char in identity) + ".json"


def _json(value: dict) -> bytes:
    """Produce deterministic UTF-8 with readable key ordering and a final newline."""
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _unique_keys(pairs: list[tuple]) -> dict:
    """Reject duplicate object keys that could hide a Git conflict resolution mistake."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _read_json(path: Path) -> dict:
    """Load one bounded regular file without following a repository symlink."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular catalog file: {path.name}")
    with path.open("rb") as source:
        raw = source.read(_MAX_FILE + 1)
    if len(raw) > _MAX_FILE:
        raise ValueError(f"Catalog file exceeds its size limit: {path.name}")
    try:
        return json.loads(raw, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Invalid JSON in catalog file: {path.name}") from error


def _catalog_folder(path: Path) -> Path:
    """Validate existing directories and allow Git to omit empty sections."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Expected a regular catalog directory: {path.name}")
    return path


def _read_entries(folder: Path, model, identity, limit: int) -> dict:
    """Read portable entries and verify every ID matches its filename."""
    entries = {}
    for path in _catalog_folder(folder).glob("*.json"):
        if len(entries) == limit:
            raise ValueError(f"Too many files in {folder.name}")
        try:
            entry = model.model_validate(_read_json(path))
        except ValidationError as error:
            raise ValueError(f"Invalid definition in {folder.name}/{path.name}") from error
        key = identity(entry)
        if path.name != _filename(key) or key in entries:
            raise ValueError(f"Catalog ID does not match {folder.name}/{path.name}")
        entries[key] = entry
    return entries


def read_git_catalog(root: Path) -> GitCatalog:
    """Validate the schema, references, payload size and local-only test hooks."""
    _catalog_folder(root)
    manifest = _read_json(root / _MANIFEST)
    if _json(manifest) != _json(_FORMAT):
        raise ValueError("Unsupported mock catalog format")
    groups = _read_entries(root / "collections", GroupMetadata, lambda item: item.id, MAX_COLLECTIONS)
    requests = _read_entries(root / "requests", GitRequest, lambda item: item.scenario.id, MAX_REQUESTS)
    for key, request in requests.items():
        if request.group_id is not None and request.group_id not in groups:
            raise ValueError(f"Missing collection for request {key}")
        _validate_request(request)
    return GitCatalog(groups, requests)


def _validate_request(request: GitRequest) -> None:
    """Check executable definitions with the same limits and rules as saved requests."""
    issues = validation_issues(request.scenario, check_ambiguity=False)
    if issues:
        raise ValueError(f"Invalid request {request.scenario.id}: {issues[0]['code']}")
    record = ScenarioRecord(scenario=request.scenario, group_id=request.group_id, revision=1)
    encode_scenario_record(record)


def export_git_catalog(service: MockTests, root: Path) -> dict[str, int]:
    """Write authored definitions only and skip bytes already present in the checkout."""
    root = _catalog_folder(root)
    if root.resolve().is_relative_to(service.directory):
        raise ValueError("Choose a Git catalog directory outside the profile's mock state")
    manifest = root / _MANIFEST
    if manifest.exists() or manifest.is_symlink():
        read_git_catalog(root)
    elif any((root / folder).exists() or (root / folder).is_symlink() for folder in _FOLDERS):
        raise ValueError("Choose a directory without existing catalog folders")
    groups, scenarios = service.catalog_records()
    files = {}
    for key, group in groups.items():
        files[root / "collections" / _filename(key)] = _json(group.model_dump(exclude={"revision"}))
    for key, record in scenarios.items():
        files[root / "requests" / _filename(key)] = _json(record.model_dump(exclude={"revision", "placement_revision"}))
    root.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Expected a regular catalog file: {path.name}")
    for path, data in files.items():
        path.parent.mkdir(exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            write_state(path, data)
    if not manifest.exists():
        write_state(manifest, _json(_FORMAT))
    return {"collections": len(groups), "requests": len(scenarios)}


def import_git_catalog(service: MockTests, catalog: GitCatalog) -> dict[str, int]:
    """Prepare and atomically apply changed entries, preserving local-only requests."""
    groups, scenarios = service.catalog_records()
    changes = 0
    for key, group in catalog.groups.items():
        current = groups.get(key)
        if current and current.title == group.title:
            continue
        revision = current.revision + 1 if current else 1
        groups[key] = GroupRecord(id=group.id, title=group.title, revision=revision)
        changes += 1
    for key, request in catalog.requests.items():
        current = scenarios.get(key)
        if current is None:
            scenarios[key] = ScenarioRecord(scenario=request.scenario, revision=1, group_id=request.group_id)
        else:
            before = _json(current.scenario.model_dump())
            after = _json(request.scenario.model_dump())
            moved = current.group_id != request.group_id
            if not moved and before == after:
                continue
            scenarios[key] = ScenarioRecord(scenario=request.scenario, group_id=request.group_id,
                                            revision=current.revision + (before != after),
                                            placement_revision=current.placement_revision + moved)
        try:
            service.require_valid(scenarios[key].scenario)
        except MockTestError as error:
            raise ValueError(f"Invalid request {key}: {error.code}") from error
        changes += 1
    if changes:
        service.replace_catalog(groups, scenarios)
    return {**catalog.counts(), "changed": changes}
