import json
from pathlib import Path
from typing import cast

from tokenmaxxing.config import hash_workspace, load_or_create_salt
from tokenmaxxing.ingest.jsonl import _database_path, scan_jsonl
from tokenmaxxing.ingest.pi import reconcile, subagents
from tokenmaxxing.ingest.pi.parse import (
    PiState,
    _Header,
    _SubagentRecord,
    _nonnegative_int,
    _string,
    _timestamp_ns,
    project_pi_line,
)
from tokenmaxxing.models import SyncStats, WriteStats
from tokenmaxxing.repository import Repository

def _read_header(path: Path) -> _Header | None:
    try:
        with path.open("r", encoding="utf-8") as session_file:
            value = json.loads(session_file.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("type") != "session":
        return None
    session_id = _string(value.get("id"))
    if session_id is None:
        return None
    parent = _string(value.get("parentSession"))
    parent_path = None
    if parent is not None:
        candidate = Path(parent).expanduser()
        parent_path = (
            (path.parent / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
    return _Header(
        path=path.resolve(),
        session_id=session_id,
        version=_nonnegative_int(value.get("version")),
        timestamp=_string(value.get("timestamp")),
        cwd=_string(value.get("cwd")),
        parent_path=parent_path,
    )


def _discover_jsonl(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix == ".jsonl" else []
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def _resolve_header(
    header: _Header,
    headers: dict[Path, _Header],
    seen: set[Path] | None = None,
) -> tuple[str | None, str]:
    if header.parent_path is None:
        return None, header.session_id
    seen = set() if seen is None else seen
    if header.path in seen:
        return None, header.session_id
    seen.add(header.path)
    parent = headers.get(header.parent_path)
    if parent is None and header.parent_path.exists():
        parent = _read_header(header.parent_path)
        if parent is not None:
            headers[parent.path] = parent
    if parent is None:
        return None, header.session_id
    _, root = _resolve_header(parent, headers, seen)
    return parent.session_id, root


def _workspace_hash(repository: Repository, cwd: str | None) -> str | None:
    if cwd is None:
        return None
    connection = repository.connection
    salt = load_or_create_salt(_database_path(connection).parent / "salt")
    return hash_workspace(cwd, salt)


def _session_state(
    repository: Repository, session_id: str
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    row = repository.connection.execute(
        "SELECT provider, initial_model, current_model, reasoning_effort, "
        "parent_session_id, root_session_id "
        "FROM sessions WHERE source = 'pi' AND source_session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None, None, None, None, None, None
    return cast(
        tuple[
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ],
        tuple(row),
    )


def _add_stats(total: SyncStats, added: SyncStats | WriteStats) -> SyncStats:
    return SyncStats(
        artifacts_seen=total.artifacts_seen + getattr(added, "artifacts_seen", 0),
        lines_read=total.lines_read + getattr(added, "lines_read", 0),
        observations_inserted=total.observations_inserted + added.observations_inserted,
        events_inserted=total.events_inserted + added.events_inserted,
        events_updated=total.events_updated + added.events_updated,
        issues_recorded=total.issues_recorded + added.issues_recorded,
    )


def sync_pi(repository: Repository, root: Path) -> SyncStats:
    paths = _discover_jsonl(root)
    headers = {
        header.path: header
        for path in paths
        if (header := _read_header(path)) is not None
    }
    changed_events = reconcile._mark_missing(repository, root, paths)
    subagent_records: dict[str, _SubagentRecord] = {}
    stats = SyncStats()
    for path in paths:
        header = headers.get(path.resolve())
        if header is None:
            continue
        parent_id, lineage_root = _resolve_header(header, headers)
        (
            provider,
            initial_model,
            current_model,
            thinking_level,
            persisted_parent_id,
            persisted_lineage_root,
        ) = _session_state(repository, header.session_id)
        if (
            header.parent_path is not None
            and parent_id is None
            and persisted_lineage_root is not None
        ):
            parent_id = persisted_parent_id
            lineage_root = persisted_lineage_root
        state = PiState(
            session_id=header.session_id,
            lineage_root_id=lineage_root,
            parent_session_id=parent_id,
            schema_version=header.version,
            started_at_ns=_timestamp_ns(header.timestamp),
            workspace_hash=_workspace_hash(repository, header.cwd),
            current_provider=provider,
            initial_model=initial_model,
            current_model=current_model,
            thinking_level=thinking_level,
            changed_events=changed_events,
            subagent_records=subagent_records,
        )
        changed_events.update(reconcile._rediscovered_events(repository, path))
        superseded_events = reconcile._superseded_events(repository, path)
        changed_events.update(superseded_events)
        stats = _add_stats(
            stats, scan_jsonl(repository, "pi", path, project_pi_line, state)
        )
    subagents._repair_subagent_runs(repository, subagent_records)
    changed_events.update(reconcile._provisional_events(repository))
    changed_events.update(reconcile._stale_events(repository))
    return _add_stats(stats, reconcile._rebuild_events(repository, changed_events))
