import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tokenmaxxing.ingest.codex.parse import _database_timestamp_ns
from tokenmaxxing.models import (
    Projection,
    RunDraft,
    SessionDraft,
    TurnDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository

def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _select_rows(
    connection: sqlite3.Connection, table: str, requested: tuple[str, ...]
) -> list[dict[str, object]]:
    available = _columns(connection, table)
    selected = [name for name in requested if name in available]
    if not selected:
        return []
    cursor = connection.execute(f"SELECT {', '.join(selected)} FROM {table}")
    return [
        {selected[index]: value for index, value in enumerate(row)}
        for row in cursor.fetchall()
    ]


def _root_and_depth(
    session_id: str, parents: Mapping[str, str]
) -> tuple[str, int]:
    current = session_id
    depth = 0
    seen = {session_id}
    while current in parents and parents[current] not in seen:
        current = parents[current]
        seen.add(current)
        depth += 1
    return current, depth


def _import_state(repository: Repository, path: Path | None) -> tuple[WriteStats, set[str]]:
    if path is None or not path.is_file():
        return WriteStats(), set()
    with _readonly_connection(path) as source:
        threads = _select_rows(
            source,
            "threads",
            (
                "id",
                "created_at",
                "updated_at",
                "created_at_ms",
                "updated_at_ms",
                "model_provider",
                "cli_version",
                "model",
                "reasoning_effort",
                "agent_role",
                "archived_at",
            ),
        )
        edges = _select_rows(
            source,
            "thread_spawn_edges",
            ("parent_thread_id", "child_thread_id", "status"),
        )
    parents = {
        str(row["child_thread_id"]): str(row["parent_thread_id"])
        for row in edges
        if isinstance(row.get("child_thread_id"), str)
        and isinstance(row.get("parent_thread_id"), str)
    }
    edge_status = {
        str(row["child_thread_id"]): cast(str, row.get("status"))
        for row in edges
        if isinstance(row.get("child_thread_id"), str) and isinstance(row.get("status"), str)
    }
    sessions: list[SessionDraft] = []
    runs: list[RunDraft] = []
    depths: dict[str, int] = {}
    for row in threads:
        session_id = row.get("id")
        if not isinstance(session_id, str):
            continue
        root, depth = _root_and_depth(session_id, parents)
        depths[session_id] = depth
        created = row.get("created_at_ms", row.get("created_at"))
        updated = row.get("updated_at_ms", row.get("updated_at"))
        model = row.get("model")
        provider = row.get("model_provider")
        cli_version = row.get("cli_version")
        role = row.get("agent_role")
        sessions.append(
            SessionDraft(
                source="codex",
                source_session_id=session_id,
                root_session_id=root,
                parent_session_id=parents.get(session_id),
                harness_version=cli_version if isinstance(cli_version, str) else None,
                provider=provider if isinstance(provider, str) else None,
                initial_model=model if isinstance(model, str) else None,
                current_model=model if isinstance(model, str) else None,
                started_at_ns=_database_timestamp_ns(created),
                updated_at_ns=_database_timestamp_ns(updated),
                completed_at_ns=_database_timestamp_ns(row.get("archived_at")),
            )
        )
        runs.append(
            RunDraft(
                source="codex",
                source_session_id=session_id,
                source_run_id=session_id,
                parent_run_id=parents.get(session_id),
                role=role if isinstance(role, str) else None,
                status=edge_status.get(session_id),
                model=model if isinstance(model, str) else None,
                started_at_ns=_database_timestamp_ns(created),
                completed_at_ns=_database_timestamp_ns(row.get("archived_at")),
            )
        )
    runs_by_session = {run.source_session_id: run for run in runs}
    changed_owners: set[str] = set()
    with repository.transaction() as target:
        for session in sessions:
            existing = target.execute(
                "SELECT root_session_id, parent_session_id FROM sessions "
                "WHERE source = 'codex' AND source_session_id = ?",
                (session.source_session_id,),
            ).fetchone()
            run = runs_by_session[session.source_session_id]
            run_row = target.execute(
                "SELECT r.parent_run_id, r.depth FROM runs r "
                "JOIN sessions s ON s.id = r.session_id "
                "WHERE s.source = 'codex' AND s.source_session_id = ? "
                "AND r.source_run_id = ?",
                (session.source_session_id, session.source_session_id),
            ).fetchone()
            if (
                existing != (session.root_session_id, session.parent_session_id)
                or run_row != (run.parent_run_id, depths[session.source_session_id])
            ):
                changed_owners.add(session.source_session_id)
        writes = repository.apply_projection_in_transaction(
            target, Projection(sessions=tuple(sessions), runs=tuple(runs))
        )
        for session_id, depth in depths.items():
            target.execute(
                "UPDATE runs SET depth = ? WHERE id = ("
                "SELECT r.id FROM runs r JOIN sessions s ON s.id = r.session_id "
                "WHERE s.source = 'codex' AND s.source_session_id = ? "
                "AND r.source_run_id = ?)",
                (depth, session_id, session_id),
            )
    return writes, changed_owners


def _import_turns(repository: Repository, path: Path | None) -> WriteStats:
    if path is None or not path.is_file():
        return WriteStats()
    with _readonly_connection(path) as source:
        rows = _select_rows(
            source,
            "thread_turns",
            (
                "thread_id",
                "turn_id",
                "status",
                "started_at",
                "completed_at",
                "duration_ms",
            ),
        )
    projections: list[Projection] = []
    for row in rows:
        session_id = row.get("thread_id")
        turn_id = row.get("turn_id")
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            continue
        status = row.get("status")
        duration_ms = row.get("duration_ms")
        duration_ns = (
            duration_ms * 1_000_000
            if isinstance(duration_ms, int) and not isinstance(duration_ms, bool)
            else None
        )
        projections.append(
            Projection(
                sessions=(SessionDraft(source="codex", source_session_id=session_id),),
                runs=(
                    RunDraft(
                        source="codex",
                        source_session_id=session_id,
                        source_run_id=session_id,
                    ),
                ),
                turns=(
                    TurnDraft(
                        source="codex",
                        source_session_id=session_id,
                        source_turn_id=turn_id,
                        source_run_id=session_id,
                        started_at_ns=_database_timestamp_ns(row.get("started_at")),
                        completed_at_ns=_database_timestamp_ns(row.get("completed_at")),
                        duration_ns=duration_ns,
                        status=status if isinstance(status, str) else None,
                    ),
                ),
            )
        )
    writes = WriteStats()
    with repository.transaction() as target:
        for projection in projections:
            added = repository.apply_projection_in_transaction(target, projection)
            writes = WriteStats(
                observations_inserted=writes.observations_inserted + added.observations_inserted,
                events_inserted=writes.events_inserted + added.events_inserted,
                events_updated=writes.events_updated + added.events_updated,
                links_inserted=writes.links_inserted + added.links_inserted,
                samples_inserted=writes.samples_inserted + added.samples_inserted,
                issues_recorded=writes.issues_recorded + added.issues_recorded,
            )
    return writes
