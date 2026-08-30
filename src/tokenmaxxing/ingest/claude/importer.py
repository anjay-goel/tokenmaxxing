from pathlib import Path

from tokenmaxxing.ingest.claude import reconcile
from tokenmaxxing.ingest.claude.parse import project_claude_line
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

def _add_stats(total: SyncStats, added: SyncStats | WriteStats) -> SyncStats:
    return SyncStats(
        artifacts_seen=total.artifacts_seen + getattr(added, "artifacts_seen", 0),
        lines_read=total.lines_read + getattr(added, "lines_read", 0),
        observations_inserted=total.observations_inserted + added.observations_inserted,
        events_inserted=total.events_inserted + added.events_inserted,
        events_updated=total.events_updated + added.events_updated,
        issues_recorded=total.issues_recorded + added.issues_recorded,
    )


def _discover_jsonl(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix == ".jsonl" else []
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def _superseded_messages(repository: Repository, path: Path) -> set[str]:
    connection = repository.connection
    artifact = _latest_artifact(connection, "claude", _path_hash(connection, path))
    if artifact is None:
        return set()
    stat = path.stat()
    prefix = _prefix_fingerprint(path, artifact.size_bytes)
    if not _needs_new_generation(
        artifact, stat, prefix, _header_session_id(path)
    ):
        return set()
    rows = connection.execute(
        "SELECT DISTINCT source_turn_id FROM observations "
        "WHERE source = 'claude' AND channel = 'disk' AND artifact_id = ? "
        "AND source_turn_id IS NOT NULL",
        (artifact.id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _rediscovered_messages(repository: Repository, path: Path) -> set[str]:
    connection = repository.connection
    artifact = _latest_artifact(connection, "claude", _path_hash(connection, path))
    if artifact is None:
        return set()
    row = connection.execute(
        "SELECT is_missing FROM artifacts WHERE id = ?",
        (artifact.id,),
    ).fetchone()
    if row is None or row[0] != 1:
        return set()
    with repository.transaction() as transaction:
        messages = {
            str(message[0])
            for message in transaction.execute(
                "SELECT DISTINCT source_turn_id FROM observations "
                "WHERE source = 'claude' AND channel = 'disk' AND artifact_id = ? "
                "AND source_turn_id IS NOT NULL",
                (artifact.id,),
            ).fetchall()
        }
        transaction.execute(
            "UPDATE usage_events SET status = 'provisional' WHERE id IN ("
            "SELECT l.usage_event_id FROM observation_links l "
            "JOIN observations o ON o.id = l.observation_id "
            "WHERE o.source = 'claude' AND o.channel = 'disk' "
            "AND o.artifact_id = ?)",
            (artifact.id,),
        )
    return messages


def sync_claude(repository: Repository, root: Path) -> SyncStats:
    stats = SyncStats()
    changed_messages: set[str] = set()
    for path in _discover_jsonl(root):
        changed_messages.update(_rediscovered_messages(repository, path))
        changed_messages.update(_superseded_messages(repository, path))

        def project(line: SourceLine, _: object) -> Projection:
            projection = project_claude_line(line)
            for observation in projection.observations:
                if observation.source_turn_id is not None:
                    changed_messages.add(observation.source_turn_id)
            return projection

        stats = _add_stats(
            stats,
            scan_jsonl(repository, "claude", path, project, object()),
        )
    changed_messages.update(reconcile._provisional_messages(repository))
    changed_messages.update(reconcile._stale_counted_messages(repository))
    return _add_stats(stats, reconcile._rebuild_messages(repository, changed_messages))
