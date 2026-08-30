import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from tokenmaxxing.ingest.jsonl import (
    SourceLine,
    _header_session_id,
    _latest_artifact,
    _needs_new_generation,
    _path_hash,
    _prefix_fingerprint,
    scan_jsonl,
)
from tokenmaxxing.models import (
    IssueDraft,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    SessionDraft,
    SyncStats,
    TokenUsage,
    TurnDraft,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository


_PARSER_VERSION = "codex-disk-v1"
_TOKEN_FIELDS = (
    "input",
    "output",
    "cache_read",
    "cache_write",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
    "reported_total",
    "derived_total",
)
_SOURCE_TOKEN_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cached_input_tokens": "cache_read",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
    "cache_creation_5m_input_tokens": "cache_write_5m",
    "cache_creation_1h_input_tokens": "cache_write_1h",
    "reasoning_output_tokens": "reasoning",
    "total_tokens": "reported_total",
}


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


@dataclass(slots=True)
class CodexState:
    owner_session_id: str | None = None
    owner_ordinal: int = 0


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _timestamp_ns(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1_000_000_000)


def _database_timestamp_ns(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    magnitude = abs(value)
    if magnitude >= 1_000_000_000_000_000:
        return value
    if magnitude >= 1_000_000_000_000:
        return value * 1_000_000
    return value * 1_000_000_000


def _usage_values(value: object) -> dict[str, int]:
    source = _mapping(value)
    if source is None:
        return {}
    usage: dict[str, int] = {}
    for source_name, target_name in _SOURCE_TOKEN_FIELDS.items():
        token_value = source.get(source_name)
        if isinstance(token_value, int) and not isinstance(token_value, bool) and token_value >= 0:
            usage[target_name] = token_value
    return usage


def _usage(usage: Mapping[str, int]) -> TokenUsage:
    return TokenUsage(**{name: usage[name] for name in _TOKEN_FIELDS if name in usage})


def _physical_key(line: SourceLine) -> str:
    return f"codex:disk:{line.artifact_id}:{line.generation}:{line.ordinal}"


def _issue(line: SourceLine, category: str, observed_type: str) -> Projection:
    return Projection(
        issues=(
            IssueDraft(
                source="codex",
                category=category,
                severity="error",
                identifier=(
                    f"artifact:{line.artifact_id}:generation:{line.generation}:ordinal:{line.ordinal}"
                ),
                field_path=str(line.ordinal),
                observed_type=observed_type,
            ),
        )
    )


def project_codex_line(line: SourceLine, state: CodexState) -> Projection:
    record_type = line.value.get("type")
    payload = _mapping(line.value.get("payload"))
    observed_at_ns = _timestamp_ns(line.value.get("timestamp"))

    if record_type == "session_meta":
        session_id = payload.get("id") if payload is not None else None
        if not isinstance(session_id, str) or not session_id:
            state.owner_session_id = None
            state.owner_ordinal = 0
            return _issue(line, "missing_session_id", type(session_id).__name__)
        state.owner_session_id = session_id
        state.owner_ordinal = 0
        cli_version = payload.get("cli_version")
        provider = payload.get("model_provider")
        model = payload.get("model")
        observation = ObservationDraft(
            source="codex",
            channel="disk",
            stable_key=_physical_key(line),
            event_type="session_meta",
            observed_at_ns=observed_at_ns,
            parser_version=_PARSER_VERSION,
            projection={},
            source_session_id=session_id,
            source_sequence=0,
            artifact_id=line.artifact_id,
            ordinal=line.ordinal,
        )
        session = SessionDraft(
            source="codex",
            source_session_id=session_id,
            root_session_id=session_id,
            harness_version=cli_version if isinstance(cli_version, str) else None,
            provider=provider if isinstance(provider, str) else None,
            initial_model=model if isinstance(model, str) else None,
            current_model=model if isinstance(model, str) else None,
            started_at_ns=observed_at_ns or None,
            updated_at_ns=observed_at_ns or None,
        )
        run = RunDraft(
            source="codex",
            source_session_id=session_id,
            source_run_id=session_id,
            role="session",
            model=model if isinstance(model, str) else None,
            started_at_ns=observed_at_ns or None,
        )
        return Projection(observations=(observation,), sessions=(session,), runs=(run,))

    if record_type != "event_msg" or payload is None or payload.get("type") != "token_count":
        return Projection()

    owner = state.owner_session_id
    if owner is None:
        return _issue(line, "missing_owner", "token_count")

    owner_ordinal = state.owner_ordinal
    info = _mapping(payload.get("info"))
    total = _usage_values(info.get("total_token_usage") if info is not None else None)
    last = _usage_values(info.get("last_token_usage") if info is not None else None)
    if not total or not last:
        return _issue(line, "invalid_usage", type(payload.get("info")).__name__)
    state.owner_ordinal += 1

    observation_key = _physical_key(line)
    event_key = f"codex:{owner}:{owner_ordinal}"
    observation = ObservationDraft(
        source="codex",
        channel="disk",
        stable_key=observation_key,
        event_type="token_count",
        observed_at_ns=observed_at_ns,
        parser_version=_PARSER_VERSION,
        projection={"usage": {"last": last, "total": total}},
        source_session_id=owner,
        source_sequence=owner_ordinal,
        artifact_id=line.artifact_id,
        ordinal=line.ordinal,
    )
    event = UsageEventDraft(
        source="codex",
        event_key=event_key,
        granularity="counter_delta",
        status="provisional",
        tokens=_usage(last),
        completed_at_ns=observed_at_ns or None,
    )
    link = LinkDraft(
        source="codex",
        channel="disk",
        observation_key=observation_key,
        event_key=event_key,
        method="owner_ordinal",
        role="primary",
        confidence="exact",
    )
    return Projection(observations=(observation,), events=(event,), links=(link,))


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
    connection = repository._database.connection
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
    connection = repository._database.connection
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
    connection = repository._database.connection
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


def _delta_usage(
    previous: Mapping[str, int] | None,
    total: Mapping[str, int],
    last: Mapping[str, int],
) -> tuple[TokenUsage, bool]:
    reset = previous is None or any(
        name in previous and name in total and total[name] < previous[name]
        for name in _TOKEN_FIELDS
    )
    if reset:
        values = dict(last)
    else:
        values = {}
        for name in _TOKEN_FIELDS:
            if name not in total:
                continue
            if name in previous:
                values[name] = max(total[name] - previous[name], 0)
            elif name in last:
                values[name] = last[name]
    token_usage = _usage(values)
    return token_usage, any(value > 0 for value in values.values())


def _rebuild_owners(repository: Repository, owners: set[str]) -> WriteStats:
    if not owners:
        return WriteStats()
    connection = repository._database.connection
    source_owners = sorted(owners)
    chunk_size = max(
        1, min(500, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    )
    rows: list[tuple[object, ...]] = []
    for offset in range(0, len(source_owners), chunk_size):
        owner_chunk = source_owners[offset : offset + chunk_size]
        placeholders = ", ".join("?" for _ in owner_chunk)
        rows.extend(
            connection.execute(
                "SELECT o.source_session_id, o.source_sequence, o.observed_at_ns, o.projection_json "
                "FROM observations o JOIN artifacts a ON a.id = o.artifact_id "
                "WHERE o.source = 'codex' AND o.channel = 'disk' AND o.event_type = 'token_count' "
                f"AND o.source_session_id IN ({placeholders}) "
                "AND COALESCE(a.is_missing, 0) = 0 "
                "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
                "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
                "ORDER BY o.source_session_id, o.source_sequence, o.artifact_id, o.ordinal",
                tuple(owner_chunk),
            ).fetchall()
        )
    semantic: dict[tuple[str, int], tuple[int, dict[str, object]]] = {}
    conflicts: set[tuple[str, int]] = set()
    for owner_value, ordinal_value, observed_at_ns, projection_json in rows:
        if not isinstance(owner_value, str) or ordinal_value is None:
            continue
        key = (owner_value, int(ordinal_value))
        projection = json.loads(str(projection_json))
        usage = projection.get("usage") if isinstance(projection, dict) else None
        if not isinstance(usage, dict):
            continue
        candidate = (int(observed_at_ns), cast(dict[str, object], usage))
        if key in semantic and semantic[key][1] != candidate[1]:
            conflicts.add(key)
            continue
        semantic.setdefault(key, candidate)

    previous_by_owner: dict[str, dict[str, int]] = {}
    active_keys_by_owner: dict[str, set[str]] = {owner: set() for owner in owners}
    writes = WriteStats()
    with repository.transaction() as target:
        for (owner, ordinal), (observed_at_ns, usage) in sorted(semantic.items()):
            event_key = f"codex:{owner}:{ordinal}"
            active_keys_by_owner.setdefault(owner, set()).add(event_key)
            total = cast(dict[str, int], usage.get("total"))
            last = cast(dict[str, int], usage.get("last"))
            if (owner, ordinal) in conflicts:
                previous_by_owner.pop(owner, None)
                projection = Projection(
                    events=(
                        UsageEventDraft(
                            source="codex",
                            event_key=event_key,
                            granularity="counter_delta",
                            status="conflicted",
                            tokens=TokenUsage(),
                        ),
                    ),
                    issues=(
                        IssueDraft(
                            source="codex",
                            category="conflicting_owner_event",
                            severity="error",
                            identifier=event_key,
                            field_path=str(ordinal),
                            observed_type="usage",
                        ),
                    ),
                )
            else:
                tokens, countable = _delta_usage(previous_by_owner.get(owner), total, last)
                session_row = target.execute(
                    "SELECT id, root_session_id FROM sessions "
                    "WHERE source = 'codex' AND source_session_id = ?",
                    (owner,),
                ).fetchone()
                session_id: int | None = None
                run_id: int | None = None
                if session_row is not None:
                    root_id = str(session_row[1] or owner)
                    root_row = target.execute(
                        "SELECT id FROM sessions WHERE source = 'codex' AND source_session_id = ?",
                        (root_id,),
                    ).fetchone()
                    session_id = int(root_row[0]) if root_row is not None else int(session_row[0])
                    run_row = target.execute(
                        "SELECT r.id FROM runs r JOIN sessions s ON s.id = r.session_id "
                        "WHERE s.source = 'codex' AND s.source_session_id = ? "
                        "AND r.source_run_id = ?",
                        (owner, owner),
                    ).fetchone()
                    run_id = int(run_row[0]) if run_row is not None else None
                projection = Projection(
                    events=(
                        UsageEventDraft(
                            source="codex",
                            event_key=event_key,
                            granularity="counter_delta",
                            status="canonical" if countable else "excluded",
                            tokens=tokens,
                            session_id=session_id,
                            run_id=run_id,
                            completed_at_ns=observed_at_ns or None,
                        ),
                    )
                )
            added = repository.apply_projection_in_transaction(target, projection)
            writes = WriteStats(
                observations_inserted=writes.observations_inserted + added.observations_inserted,
                events_inserted=writes.events_inserted + added.events_inserted,
                events_updated=writes.events_updated + added.events_updated,
                links_inserted=writes.links_inserted + added.links_inserted,
                samples_inserted=writes.samples_inserted + added.samples_inserted,
                issues_recorded=writes.issues_recorded + added.issues_recorded,
            )
            if (owner, ordinal) not in conflicts:
                previous_by_owner[owner] = total
        for owner in sorted(owners):
            existing_events = {
                (int(row[0]), str(row[1]))
                for row in target.execute(
                    "SELECT DISTINCT e.id, e.event_key FROM observations o "
                    "INDEXED BY idx_observations_codex_owner_link "
                    "CROSS JOIN observation_links l "
                    "INDEXED BY sqlite_autoindex_observation_links_1 "
                    "CROSS JOIN usage_events e "
                    "WHERE o.source = 'codex' AND o.channel = 'disk' "
                    "AND o.source_session_id = ? "
                    "AND l.observation_id = o.id AND e.id = l.usage_event_id "
                    "AND e.source = 'codex'",
                    (owner,),
                )
            }
            target.executemany(
                "UPDATE usage_events SET status = 'excluded' "
                "WHERE id = ?",
                (
                    (event_id,)
                    for event_id, event_key in sorted(
                        existing_events
                    )
                    if event_key not in active_keys_by_owner.get(owner, set())
                ),
            )
    return writes


def _provisional_owners(repository: Repository) -> set[str]:
    rows = repository._database.connection.execute(
        "SELECT DISTINCT o.source_session_id FROM usage_events e "
        "INDEXED BY idx_usage_events_status "
        "CROSS JOIN observation_links l INDEXED BY idx_observation_links_usage_event "
        "CROSS JOIN observations o "
        "WHERE o.source = 'codex' AND o.channel = 'disk' "
        "AND o.event_type = 'token_count' AND e.status = 'provisional' "
        "AND l.usage_event_id = e.id AND o.id = l.observation_id "
        "AND o.source_session_id IS NOT NULL"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _stale_counted_owners(repository: Repository) -> set[str]:
    rows = repository._database.connection.execute(
        "SELECT DISTINCT old.source_session_id FROM usage_events e "
        "INDEXED BY idx_usage_events_status "
        "CROSS JOIN observation_links l "
        "INDEXED BY idx_observation_links_usage_event "
        "CROSS JOIN observations old "
        "WHERE e.source = 'codex' AND e.status IN ('canonical', 'provisional', 'conflicted') "
        "AND l.usage_event_id = e.id AND old.id = l.observation_id "
        "AND old.source = 'codex' AND old.channel = 'disk' "
        "AND old.event_type = 'token_count' AND old.source_session_id IS NOT NULL "
        "AND (NOT EXISTS ("
        "SELECT 1 FROM observations live "
        "INDEXED BY idx_observations_codex_owner_artifact "
        "JOIN artifacts a ON a.id = live.artifact_id "
        "WHERE live.source = 'codex' AND live.channel = 'disk' "
        "AND live.event_type = 'token_count' "
        "AND live.source_session_id = old.source_session_id "
        "AND COALESCE(a.is_missing, 0) = 0 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash)) "
        "OR (e.status = 'conflicted' AND EXISTS ("
        "SELECT 1 FROM observations missing "
        "INDEXED BY idx_observations_codex_owner_artifact "
        "JOIN artifacts a ON a.id = missing.artifact_id "
        "WHERE missing.source = 'codex' AND missing.channel = 'disk' "
        "AND missing.event_type = 'token_count' "
        "AND missing.source_session_id = old.source_session_id "
        "AND a.is_missing = 1 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash))))"
    ).fetchall()
    return {str(row[0]) for row in rows}


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
    changed_owners.update(_provisional_owners(repository))
    changed_owners.update(_stale_counted_owners(repository))
    stats = _add_stats(stats, _rebuild_owners(repository, changed_owners))
    return stats
