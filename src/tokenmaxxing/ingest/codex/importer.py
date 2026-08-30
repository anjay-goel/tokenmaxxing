from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tokenmaxxing.ingest.codex import reconcile
from tokenmaxxing.ingest.codex.metadata import _import_state, _import_turns
from tokenmaxxing.ingest.codex.parse import CodexState, project_codex_line
from tokenmaxxing.ingest.jsonl import (
    SourceLine,
    _header_session_id,
    _latest_artifact,
    _needs_new_generation,
    _path_hash,
    _prefix_fingerprint,
    scan_jsonl,
)
from tokenmaxxing.models import Projection, SyncStats, WriteStats
from tokenmaxxing.repository import Repository

@dataclass(frozen=True, slots=True)
class CodexRoots:
    sessions: Path
    archived_sessions: Path
    state_db: Path | None = None
    thread_history_db: Path | None = None

    @classmethod
    def from_path(cls, root: Path) -> "CodexRoots":
        sessions = root / "sessions"
        if not sessions.exists():
            sessions = root
        state_db = root / "state_5.sqlite"
        thread_history_db = root / "thread_history_1.sqlite"
        return cls(
            sessions=sessions,
            archived_sessions=root / "archived_sessions",
            state_db=state_db if state_db.exists() else None,
            thread_history_db=thread_history_db if thread_history_db.exists() else None,
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


def _discover_jsonl(roots: CodexRoots) -> list[Path]:
    archived = roots.archived_sessions.resolve()
    paths: set[Path] = set()
    if roots.sessions.is_file() and roots.sessions.suffix == ".jsonl":
        paths.add(roots.sessions)
    elif roots.sessions.exists():
        for path in roots.sessions.rglob("*.jsonl"):
            if not path.resolve().is_relative_to(archived):
                paths.add(path)
    if roots.archived_sessions.is_file() and roots.archived_sessions.suffix == ".jsonl":
        paths.add(roots.archived_sessions)
    elif roots.archived_sessions.exists():
        paths.update(roots.archived_sessions.rglob("*.jsonl"))
    return sorted(paths)


def _mark_missing(repository: Repository, roots: CodexRoots, paths: list[Path]) -> None:
    if not roots.sessions.is_dir() and not roots.archived_sessions.is_dir():
        return
    connection = repository.connection
    present_hashes = {_path_hash(connection, path) for path in paths}
    rows = connection.execute(
        "SELECT a.id, a.path_hash FROM artifacts a "
        "WHERE a.source = 'codex' AND a.generation = ("
        "SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
        "AND COALESCE(a.is_missing, 0) = 0"
    ).fetchall()
    missing_ids = [int(row[0]) for row in rows if str(row[1]) not in present_hashes]
    if missing_ids:
        with repository.transaction() as transaction:
            transaction.executemany(
                "UPDATE artifacts SET is_missing = 1 WHERE id = ?",
                ((artifact_id,) for artifact_id in missing_ids),
            )


def _rediscovered_owners(repository: Repository, path: Path) -> set[str]:
    connection = repository.connection
    artifact = _latest_artifact(connection, "codex", _path_hash(connection, path))
    if artifact is None:
        return set()
    row = connection.execute(
        "SELECT is_missing FROM artifacts WHERE id = ?", (artifact.id,)
    ).fetchone()
    if row is None or row[0] != 1:
        return set()
    with repository.transaction() as transaction:
        owners = {
            str(row[0])
            for row in transaction.execute(
                "SELECT DISTINCT source_session_id FROM observations "
                "WHERE source = 'codex' AND channel = 'disk' "
                "AND event_type = 'token_count' AND artifact_id = ? "
                "AND source_session_id IS NOT NULL",
                (artifact.id,),
            ).fetchall()
        }
        transaction.execute("UPDATE artifacts SET is_missing = 0 WHERE id = ?", (artifact.id,))
        transaction.execute(
            "UPDATE usage_events SET status = 'provisional' WHERE id IN ("
            "SELECT l.usage_event_id FROM observation_links l "
            "JOIN observations o ON o.id = l.observation_id "
            "WHERE o.source = 'codex' AND o.channel = 'disk' "
            "AND o.artifact_id = ?)",
            (artifact.id,),
        )
    return owners


def _resume_state(repository: Repository, path: Path) -> CodexState:
    connection = repository.connection
    artifact = _latest_artifact(connection, "codex", _path_hash(connection, path))
    if artifact is None:
        return CodexState()
    stat = path.stat()
    prefix = _prefix_fingerprint(path, artifact.size_bytes)
    if _needs_new_generation(artifact, stat, prefix, _header_session_id(path)):
        return CodexState()
    row = connection.execute(
        "SELECT event_type, source_session_id, source_sequence FROM observations "
        "WHERE source = 'codex' AND channel = 'disk' AND artifact_id = ? "
        "ORDER BY ordinal DESC LIMIT 1",
        (artifact.id,),
    ).fetchone()
    if row is None or not isinstance(row[1], str):
        return CodexState()
    sequence = int(row[2] or 0)
    return CodexState(
        owner_session_id=str(row[1]),
        owner_ordinal=sequence + int(row[0] == "token_count"),
    )


def sync_codex(repository: Repository, roots: CodexRoots) -> SyncStats:
    stats = SyncStats()
    changed_owners: set[str] = set()
    paths = _discover_jsonl(roots)
    _mark_missing(repository, roots, paths)
    for path in paths:
        changed_owners.update(_rediscovered_owners(repository, path))
        state = _resume_state(repository, path)

        def project(line: SourceLine, project_state: object) -> Projection:
            projection = project_codex_line(line, cast(CodexState, project_state))
            for observation in projection.observations:
                if observation.event_type == "token_count" and observation.source_session_id:
                    changed_owners.add(observation.source_session_id)
            return projection

        stats = _add_stats(stats, scan_jsonl(repository, "codex", path, project, state))
    state_writes, state_owners = _import_state(repository, roots.state_db)
    changed_owners.update(state_owners)
    stats = _add_stats(stats, state_writes)
    stats = _add_stats(stats, _import_turns(repository, roots.thread_history_db))
    changed_owners.update(reconcile._provisional_owners(repository))
    changed_owners.update(reconcile._stale_counted_owners(repository))
    stats = _add_stats(stats, reconcile._rebuild_owners(repository, changed_owners))
    return stats
