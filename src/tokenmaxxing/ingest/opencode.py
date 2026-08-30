import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from tokenmaxxing.models import (
    CostUsage,
    LinkDraft,
    ObservationDraft,
    Projection,
    SessionDraft,
    SyncStats,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository


_PARSER_VERSION = "opencode-sqlite-v1"
_REQUIRED_COLUMNS = {
    "session": {
        "id",
        "parent_id",
        "version",
        "agent",
        "time_created",
        "time_updated",
        "cost",
        "tokens_input",
        "tokens_output",
        "tokens_reasoning",
        "tokens_cache_read",
        "tokens_cache_write",
    },
    "message": {"id", "session_id", "time_created", "time_updated", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "time_updated", "data"},
}
_METADATA_FIELDS = frozenset(
    {"provider", "model", "effort", "completed_at_ns"}
)
_STABLE_V1_SESSION_VERSION = re.compile(
    r"1\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)


@dataclass(frozen=True, slots=True)
class OpenCodeRoots:
    database: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "OpenCodeRoots":
        return cls(database=data_dir / "opencode.db")


@dataclass(frozen=True, slots=True)
class _UsageRow:
    source_id: str
    event_key: str
    event_type: str
    session_id: str
    provider: str | None
    model: str | None
    agent: str | None
    completed_at_ns: int | None
    input_tokens: int
    visible_output: int
    reasoning_tokens: int
    cache_read: int
    cache_write: int
    reported_total: int | None
    cost: str | None

    @property
    def normalized_output(self) -> int:
        return self.visible_output + self.reasoning_tokens

    @property
    def derived_total(self) -> int:
        return self.input_tokens + self.normalized_output + self.cache_read + self.cache_write

    @property
    def projection(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "source_id": self.source_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "agent": self.agent,
            "completed_at_ns": self.completed_at_ns,
            "input": self.input_tokens,
            "output": self.visible_output,
            "reasoning": self.reasoning_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reported_total": self.reported_total,
            "cost": self.cost,
        }

    @property
    def observation_key(self) -> str:
        encoded = json.dumps(self.projection, allow_nan=False, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"opencode:{self.event_type}:{self.source_id}:{digest}"

    def tokens(self) -> TokenUsage:
        return TokenUsage(
            input=self.input_tokens,
            output=self.normalized_output,
            reasoning=self.reasoning_tokens,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            reported_total=self.reported_total,
            derived_total=self.derived_total,
        )

    def cost_usage(self) -> CostUsage:
        if self.cost is None:
            return CostUsage()
        try:
            cost = Decimal(self.cost)
        except InvalidOperation:
            return CostUsage()
        if not cost.is_finite() or cost < 0:
            return CostUsage()
        try:
            return CostUsage.from_decimal(
                cost, source="opencode_reported_estimate", estimated=True
            )
        except ValueError:
            return CostUsage(
                total_nanos=int(
                    (cost * Decimal("1000000000")).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                ),
                original_decimal=self.cost,
                source="opencode_reported_estimate",
                estimated=True,
            )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _milliseconds_ns(value: object) -> int | None:
    timestamp = _nonnegative_int(value)
    return timestamp * 1_000_000 if timestamp else None


@contextmanager
def _read_source(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{quote(str(path))}?mode=ro"
    source = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        source.execute("PRAGMA query_only = ON")
        source.execute("BEGIN")
        _validate_schema(source)
        yield source
    finally:
        source.rollback()
        source.close()


def _validate_schema(source: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table, required_columns in _REQUIRED_COLUMNS.items():
        if table not in tables:
            raise ValueError("unsupported OpenCode V1 schema")
        columns = {str(row[1]) for row in source.execute(f"PRAGMA table_info({table})")}
        if not required_columns <= columns:
            raise ValueError("unsupported OpenCode V1 schema")
    versions = source.execute("SELECT version FROM session").fetchall()
    if any(
        not isinstance(row[0], str) or _STABLE_V1_SESSION_VERSION.fullmatch(row[0]) is None
        for row in versions
    ):
        raise ValueError("unsupported OpenCode V1 schema")


def _source_rows(source: sqlite3.Connection) -> list[_UsageRow]:
    steps = source.execute(
        "SELECT p.id, m.session_id, "
        "json_extract(m.data, '$.providerID'), json_extract(m.data, '$.modelID'), "
        "COALESCE(json_extract(m.data, '$.agent'), s.agent), "
        "COALESCE(json_extract(m.data, '$.time.completed'), m.time_updated), "
        "json_extract(p.data, '$.tokens.input'), json_extract(p.data, '$.tokens.output'), "
        "json_extract(p.data, '$.tokens.reasoning'), json_extract(p.data, '$.tokens.total'), "
        "json_extract(p.data, '$.tokens.cache.read'), json_extract(p.data, '$.tokens.cache.write'), "
        "p.data -> '$.cost' "
        "FROM part p JOIN message m ON m.id = p.message_id "
        "JOIN session s ON s.id = m.session_id "
        "WHERE json_extract(p.data, '$.type') = 'step-finish' "
        "AND json_extract(m.data, '$.role') = 'assistant'"
    ).fetchall()
    fallbacks = source.execute(
        "SELECT m.id, m.session_id, "
        "json_extract(m.data, '$.providerID'), json_extract(m.data, '$.modelID'), "
        "COALESCE(json_extract(m.data, '$.agent'), s.agent), "
        "COALESCE(json_extract(m.data, '$.time.completed'), m.time_updated), "
        "json_extract(m.data, '$.tokens.input'), json_extract(m.data, '$.tokens.output'), "
        "json_extract(m.data, '$.tokens.reasoning'), json_extract(m.data, '$.tokens.total'), "
        "json_extract(m.data, '$.tokens.cache.read'), json_extract(m.data, '$.tokens.cache.write'), "
        "m.data -> '$.cost' "
        "FROM message m JOIN session s ON s.id = m.session_id "
        "WHERE json_extract(m.data, '$.role') = 'assistant' "
        "AND NOT EXISTS (SELECT 1 FROM part p WHERE p.message_id = m.id "
        "AND json_extract(p.data, '$.type') = 'step-finish')"
    ).fetchall()
    return [
        _usage_row(row, event_type="part", event_key=f"opencode:part:{row[0]}")
        for row in steps
    ] + [
        _usage_row(row, event_type="message", event_key=f"opencode:message:{row[0]}")
        for row in fallbacks
    ]


def _usage_row(row: tuple[object, ...], *, event_type: str, event_key: str) -> _UsageRow:
    return _UsageRow(
        source_id=str(row[0]),
        event_key=event_key,
        event_type=event_type,
        session_id=str(row[1]),
        provider=_string(row[2]),
        model=_string(row[3]),
        agent=_string(row[4]),
        completed_at_ns=_milliseconds_ns(row[5]),
        input_tokens=_nonnegative_int(row[6]),
        visible_output=_nonnegative_int(row[7]),
        reasoning_tokens=_nonnegative_int(row[8]),
        reported_total=(
            _nonnegative_int(row[9])
            if isinstance(row[9], int) and not isinstance(row[9], bool) and row[9] >= 0
            else None
        ),
        cache_read=_nonnegative_int(row[10]),
        cache_write=_nonnegative_int(row[11]),
        cost=row[12] if isinstance(row[12], str) else None,
    )


def _session_drafts(source: sqlite3.Connection) -> tuple[SessionDraft, ...]:
    rows = source.execute(
        "SELECT id, parent_id, version, time_created, time_updated FROM session"
    ).fetchall()
    parents = {
        str(row[0]): str(row[1])
        for row in rows
        if isinstance(row[0], str) and isinstance(row[1], str) and row[1]
    }
    return tuple(
        SessionDraft(
            source="opencode",
            source_session_id=str(row[0]),
            root_session_id=_root_session_id(str(row[0]), parents),
            parent_session_id=_string(row[1]),
            harness_version=_string(row[2]),
            started_at_ns=_milliseconds_ns(row[3]),
            updated_at_ns=_milliseconds_ns(row[4]),
        )
        for row in rows
        if isinstance(row[0], str) and row[0]
    )


def _root_session_id(session_id: str, parents: Mapping[str, str]) -> str:
    current = session_id
    visited = {session_id}
    while current in parents and parents[current] not in visited:
        current = parents[current]
        visited.add(current)
    return current


def _root_database_id(repository: Repository, source_session_id: str) -> int | None:
    row = repository.connection.execute(
        "SELECT id FROM sessions WHERE source = 'opencode' AND source_session_id = ("
        "SELECT root_session_id FROM sessions WHERE source = 'opencode' AND source_session_id = ?)",
        (source_session_id,),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _event_is_current(repository: Repository, row: _UsageRow, session_id: int | None) -> bool:
    observation = repository.connection.execute(
        "SELECT 1 FROM observations WHERE source = 'opencode' AND channel = 'disk' AND stable_key = ?",
        (row.observation_key,),
    ).fetchone()
    event = repository.connection.execute(
        "SELECT status, session_id FROM usage_events WHERE source = 'opencode' AND event_key = ?",
        (row.event_key,),
    ).fetchone()
    return observation is not None and event == ("canonical", session_id)


def _projection(row: _UsageRow, session_id: int | None) -> Projection:
    observation = ObservationDraft(
        source="opencode",
        channel="disk",
        stable_key=row.observation_key,
        event_type=row.event_type,
        observed_at_ns=row.completed_at_ns or 0,
        parser_version=_PARSER_VERSION,
        projection={
            "usage": {
                "input": row.input_tokens,
                "output": row.visible_output,
                "reasoning": row.reasoning_tokens,
                "cache_read": row.cache_read,
                "cache_write": row.cache_write,
            }
        },
        source_session_id=row.session_id,
        source_turn_id=row.event_key,
    )
    event = UsageEventDraft(
        source="opencode",
        event_key=row.event_key,
        granularity="model_call",
        status="canonical",
        tokens=row.tokens(),
        cost=row.cost_usage(),
        session_id=session_id,
        provider=row.provider,
        model=row.model,
        effort=row.agent,
        completed_at_ns=row.completed_at_ns,
        replace_usage=True,
        replace_metadata_fields=_METADATA_FIELDS,
    )
    link = LinkDraft(
        source="opencode",
        channel="disk",
        observation_key=row.observation_key,
        event_key=row.event_key,
        method="part_id" if row.event_type == "part" else "message_id",
        role="primary",
        confidence="exact",
    )
    return Projection(observations=(observation,), events=(event,), links=(link,))


def _add_stats(total: SyncStats, writes: WriteStats) -> SyncStats:
    return SyncStats(
        artifacts_seen=total.artifacts_seen,
        lines_read=total.lines_read,
        observations_inserted=total.observations_inserted + writes.observations_inserted,
        events_inserted=total.events_inserted + writes.events_inserted,
        events_updated=total.events_updated + writes.events_updated,
        issues_recorded=total.issues_recorded + writes.issues_recorded,
    )


def sync_opencode(repository: Repository, roots: OpenCodeRoots) -> SyncStats:
    if not roots.database.is_file():
        return SyncStats()
    with _read_source(roots.database) as source:
        sessions = _session_drafts(source)
        rows = _source_rows(source)

    stats = SyncStats(artifacts_seen=1)
    with repository.transaction() as connection:
        session_writes = repository.apply_projection_in_transaction(
            connection, Projection(sessions=sessions)
        )
        stats = _add_stats(stats, session_writes)
        seen = {row.event_key for row in rows}
        for row in rows:
            session_id = _root_database_id(repository, row.session_id)
            if _event_is_current(repository, row, session_id):
                continue
            stats = _add_stats(
                stats,
                repository.apply_projection_in_transaction(connection, _projection(row, session_id)),
            )
        placeholders = ", ".join("?" for _ in seen)
        query = "UPDATE usage_events SET status = 'excluded' WHERE source = 'opencode' AND status != 'excluded'"
        parameters: tuple[str, ...] = ()
        if placeholders:
            query += f" AND event_key NOT IN ({placeholders})"
            parameters = tuple(sorted(seen))
        changed = connection.execute(query, parameters).rowcount
        if changed:
            stats = SyncStats(
                artifacts_seen=stats.artifacts_seen,
                lines_read=stats.lines_read,
                observations_inserted=stats.observations_inserted,
                events_inserted=stats.events_inserted,
                events_updated=stats.events_updated + changed,
                issues_recorded=stats.issues_recorded,
            )
    return stats
