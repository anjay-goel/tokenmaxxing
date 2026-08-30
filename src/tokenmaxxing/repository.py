import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from tokenmaxxing.db import Database
from tokenmaxxing.models import (
    Channel,
    CostUsage,
    Granularity,
    IssueDraft,
    LinkDraft,
    ObservationDraft,
    Projection,
    ReportingRow,
    RunDraft,
    SampleDraft,
    SessionDraft,
    Source,
    TokenUsage,
    TurnDraft,
    UsageEventDraft,
    UsageTotal,
    WriteStats,
)
from tokenmaxxing.privacy import projection_json


_TOKEN_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
    "cache_write_5m": "cache_write_5m_tokens",
    "cache_write_1h": "cache_write_1h_tokens",
    "reasoning": "reasoning_tokens",
    "reported_total": "reported_total_tokens",
    "derived_total": "derived_total_tokens",
}
_COST_COLUMNS = {
    "input_nanos": "input_cost_nanos",
    "output_nanos": "output_cost_nanos",
    "cache_read_nanos": "cache_read_cost_nanos",
    "cache_write_nanos": "cache_write_cost_nanos",
    "total_nanos": "total_cost_nanos",
    "original_decimal": "original_cost_decimal",
    "source": "cost_source",
    "estimated": "cost_estimated",
}
_EVENT_FIELDS = (
    "session_id",
    "run_id",
    "turn_id",
    "provider",
    "api",
    "model",
    "response_model",
    "service_tier",
    "speed",
    "inference_region",
    "effort",
    "stop_reason",
    "error_category",
    "started_at_ns",
    "completed_at_ns",
    "duration_ns",
    "ttft_ns",
    "retries",
    "success",
    "status_code",
    "web_search_count",
    "web_fetch_count",
    "tool_use_count",
)
_REPLACEABLE_EVENT_METADATA_FIELDS = frozenset(
    {
        "provider",
        "api",
        "model",
        "response_model",
        "service_tier",
        "speed",
        "inference_region",
        "effort",
        "stop_reason",
        "error_category",
        "started_at_ns",
        "completed_at_ns",
        "duration_ns",
        "ttft_ns",
        "retries",
        "success",
        "status_code",
        "web_search_count",
        "web_fetch_count",
        "tool_use_count",
    }
)
_IDENTITY_COLUMNS = (
    "response_id",
    "request_id",
    "client_id",
    "trace_id",
    "span_id",
    "source_sequence",
)
_GROUP_COLUMNS = {
    "source": "source",
    "provider": "provider",
    "api": "api",
    "model": "model",
    "response_model": "response_model",
    "service_tier": "service_tier",
    "speed": "speed",
    "inference_region": "inference_region",
    "effort": "effort",
    "status": "status",
    "granularity": "granularity",
}


def _values(record: object, names: Iterable[str]) -> list[object]:
    result: list[object] = []
    for name in names:
        value = getattr(record, name)
        result.append(int(value) if isinstance(value, bool) else value)
    return result


def _row_dict(cursor: sqlite3.Cursor, row: sqlite3.Row | tuple[object, ...]) -> dict[str, object]:
    return {description[0]: row[index] for index, description in enumerate(cursor.description or ())}


class Repository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def connection(self) -> sqlite3.Connection:
        return self._database.connection

    def apply_projection(self, projection: Projection) -> WriteStats:
        with self.transaction() as connection:
            return self.apply_projection_in_transaction(connection, projection)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._database.transaction() as connection:
            yield connection

    def apply_projection_in_transaction(
        self, connection: sqlite3.Connection, projection: Projection
    ) -> WriteStats:
        counters = {
            "observations_inserted": 0,
            "events_inserted": 0,
            "events_updated": 0,
            "links_inserted": 0,
            "samples_inserted": 0,
            "issues_recorded": 0,
        }
        for session in projection.sessions:
            self._upsert_session(connection, session)
        for run in projection.runs:
            self._upsert_run(connection, run)
        for turn in projection.turns:
            self._upsert_turn(connection, turn)
        for observation in projection.observations:
            counters["observations_inserted"] += self._upsert_observation(connection, observation)
        for event in projection.events:
            inserted = self._upsert_event(connection, event)
            counters["events_inserted"] += inserted
            counters["events_updated"] += 1 - inserted
        for link in projection.links:
            counters["links_inserted"] += self._insert_link(connection, link)
        for sample in projection.samples:
            counters["samples_inserted"] += self._insert_sample(connection, sample)
        for issue in projection.issues:
            counters["issues_recorded"] += self._record_issue(connection, issue)
        return WriteStats(**counters)

    def _upsert_session(self, connection: sqlite3.Connection, draft: SessionDraft) -> None:
        columns = (
            "source",
            "source_session_id",
            "root_session_id",
            "parent_session_id",
            "harness_version",
            "schema_version",
            "provider",
            "initial_model",
            "current_model",
            "reasoning_effort",
            "service_tier",
            "started_at_ns",
            "updated_at_ns",
            "completed_at_ns",
            "workspace_hash",
        )
        updates = ", ".join(
            f"{column} = COALESCE(excluded.{column}, sessions.{column})" for column in columns[2:]
        )
        connection.execute(
            f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(source, source_session_id) DO UPDATE SET {updates}",
            _values(draft, columns),
        )

    def _session_id(
        self, connection: sqlite3.Connection, source: Source, source_session_id: str
    ) -> int:
        row = connection.execute(
            "SELECT id FROM sessions WHERE source = ? AND source_session_id = ?",
            (source, source_session_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"session does not exist: {source_session_id}")
        return int(row[0])

    def _upsert_run(self, connection: sqlite3.Connection, draft: RunDraft) -> None:
        session_id = self._session_id(connection, draft.source, draft.source_session_id)
        columns = (
            "session_id",
            "source_run_id",
            "parent_run_id",
            "batch_id",
            "role",
            "status",
            "model",
            "provider",
            "effort",
            "isolation",
            "started_at_ns",
            "completed_at_ns",
            "duration_ns",
        )
        values = [session_id, *_values(draft, columns[1:])]
        updates = ", ".join(
            f"{column} = COALESCE(excluded.{column}, runs.{column})" for column in columns[2:]
        )
        connection.execute(
            f"INSERT INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(session_id, source_run_id) DO UPDATE SET {updates}",
            values,
        )

    def _upsert_turn(self, connection: sqlite3.Connection, draft: TurnDraft) -> None:
        session_id = self._session_id(connection, draft.source, draft.source_session_id)
        run_id: int | None = None
        if draft.source_run_id is not None:
            row = connection.execute(
                "SELECT id FROM runs WHERE session_id = ? AND source_run_id = ?",
                (session_id, draft.source_run_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"run does not exist: {draft.source_run_id}")
            run_id = int(row[0])
        columns = (
            "session_id",
            "run_id",
            "source_turn_id",
            "source_run_id",
            "started_at_ns",
            "completed_at_ns",
            "duration_ns",
            "ttft_ns",
            "status",
        )
        values = [
            session_id,
            run_id,
            draft.source_turn_id,
            draft.source_run_id,
            draft.started_at_ns,
            draft.completed_at_ns,
            draft.duration_ns,
            draft.ttft_ns,
            draft.status,
        ]
        updates = ", ".join(
            f"{column} = COALESCE(excluded.{column}, turns.{column})" for column in columns[1:] if column != "source_turn_id"
        )
        connection.execute(
            f"INSERT INTO turns ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(session_id, source_turn_id) DO UPDATE SET {updates}",
            values,
        )

    def _upsert_observation(
        self, connection: sqlite3.Connection, draft: ObservationDraft
    ) -> int:
        existed = connection.execute(
            "SELECT 1 FROM observations WHERE source = ? AND channel = ? AND stable_key = ?",
            (draft.source, draft.channel, draft.stable_key),
        ).fetchone()
        columns = (
            "source",
            "channel",
            "stable_key",
            "event_type",
            "observed_at_ns",
            "parser_version",
            "source_session_id",
            "source_run_id",
            "source_turn_id",
            "response_id",
            "request_id",
            "client_id",
            "trace_id",
            "span_id",
            "source_sequence",
            "artifact_id",
            "ordinal",
            "projection_json",
        )
        values = [
            *_values(draft, columns[:-1]),
            projection_json(draft.projection),
        ]
        updates = ", ".join(
            f"{column} = COALESCE(excluded.{column}, observations.{column})"
            for column in columns[3:-1]
        )
        updates = f"{updates}, projection_json = excluded.projection_json"
        connection.execute(
            f"INSERT INTO observations ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(source, channel, stable_key) DO UPDATE SET {updates}",
            values,
        )
        return int(existed is None)

    def _upsert_event(self, connection: sqlite3.Connection, draft: UsageEventDraft) -> int:
        unsupported_replace_fields = (
            draft.replace_metadata_fields - _REPLACEABLE_EVENT_METADATA_FIELDS
        )
        if unsupported_replace_fields:
            raise ValueError(
                "unsupported replace_metadata_fields: "
                + ", ".join(sorted(unsupported_replace_fields))
            )
        existed = connection.execute(
            "SELECT 1 FROM usage_events WHERE source = ? AND event_key = ?",
            (draft.source, draft.event_key),
        ).fetchone()
        columns = ["source", "event_key", "granularity", "status", *_EVENT_FIELDS]
        values = [*_values(draft, columns)]
        for attribute, column in _TOKEN_COLUMNS.items():
            columns.append(column)
            values.append(getattr(draft.tokens, attribute))
        for attribute, column in _COST_COLUMNS.items():
            columns.append(column)
            value = getattr(draft.cost, attribute)
            values.append(int(value) if isinstance(value, bool) else value)
        updates = ["granularity = excluded.granularity", "status = excluded.status"]
        usage_columns = frozenset((*_TOKEN_COLUMNS.values(), *_COST_COLUMNS.values()))
        for column in columns[4:]:
            if column in draft.replace_metadata_fields:
                updates.append(f"{column} = excluded.{column}")
            elif draft.replace_usage and column in usage_columns:
                updates.append(f"{column} = excluded.{column}")
            else:
                updates.append(
                    f"{column} = COALESCE(excluded.{column}, usage_events.{column})"
                )
        connection.execute(
            f"INSERT INTO usage_events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(source, event_key) DO UPDATE SET {', '.join(updates)}",
            values,
        )
        return int(existed is None)

    def _insert_link(self, connection: sqlite3.Connection, draft: LinkDraft) -> int:
        row = connection.execute(
            "SELECT o.id, e.id FROM observations o "
            "JOIN usage_events e ON e.source = o.source "
            "WHERE o.source = ? AND o.channel = ? AND o.stable_key = ? AND e.event_key = ?",
            (draft.source, draft.channel, draft.observation_key, draft.event_key),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"link target does not exist: {draft.observation_key} -> {draft.event_key}"
            )
        observation_id, event_id = row
        cursor = connection.execute(
            "INSERT OR IGNORE INTO observation_links "
            "(observation_id, usage_event_id, method, role, confidence) VALUES (?, ?, ?, ?, ?)",
            (observation_id, event_id, draft.method, draft.role, draft.confidence),
        )
        assignments = ", ".join(
            f"{column} = COALESCE({column}, (SELECT {column} FROM observations WHERE id = ?))"
            for column in _IDENTITY_COLUMNS
        )
        connection.execute(
            f"UPDATE usage_events SET {assignments} WHERE id = ?",
            (*([observation_id] * len(_IDENTITY_COLUMNS)), event_id),
        )
        return cursor.rowcount

    def _insert_sample(self, connection: sqlite3.Connection, draft: SampleDraft) -> int:
        existed = connection.execute(
            "SELECT 1 FROM samples WHERE source = ? AND channel = ? AND stable_key = ? AND name = ?",
            (draft.source, draft.channel, draft.stable_key, draft.name),
        ).fetchone()
        if existed is not None:
            return 0
        connection.execute(
            "INSERT INTO samples "
            "(source, channel, stable_key, sample_type, observed_at_ns, name, unit, value_integer, value_real, attributes_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.source,
                draft.channel,
                draft.stable_key,
                draft.sample_type,
                draft.observed_at_ns,
                draft.name,
                draft.unit,
                draft.value_integer,
                draft.value_real,
                projection_json({"usage": draft.attributes}),
            ),
        )
        return 1

    def _record_issue(self, connection: sqlite3.Connection, draft: IssueDraft) -> int:
        row = connection.execute(
            "SELECT id FROM issues WHERE source = ? AND category = ? AND identifier = ? "
            "AND field_path IS ? AND resolved_at_ns IS NULL",
            (draft.source, draft.category, draft.identifier, draft.field_path),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE issues SET severity = ?, observed_type = ? WHERE id = ?",
                (draft.severity, draft.observed_type, row[0]),
            )
            return 0
        connection.execute(
            "INSERT INTO issues (source, category, severity, identifier, field_path, observed_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                draft.source,
                draft.category,
                draft.severity,
                draft.identifier,
                draft.field_path,
                draft.observed_type,
            ),
        )
        return 1

    def merge_events(self, survivor_id: int, absorbed_id: int) -> None:
        with self._database.transaction() as connection:
            self._merge_events(connection, survivor_id, absorbed_id)

    def _merge_events(
        self, connection: sqlite3.Connection, survivor_id: int, absorbed_id: int
    ) -> None:
        if survivor_id == absorbed_id:
            return
        rows = connection.execute(
            "SELECT id, source FROM usage_events WHERE id IN (?, ?)",
            (survivor_id, absorbed_id),
        ).fetchall()
        if len(rows) != 2 or len({row[1] for row in rows}) != 1:
            raise ValueError("events to merge must exist and have the same source")
        fill_columns = [
            *_IDENTITY_COLUMNS,
            *_EVENT_FIELDS,
            *_TOKEN_COLUMNS.values(),
            *_COST_COLUMNS.values(),
        ]
        assignments = ", ".join(
            f"{column} = COALESCE({column}, (SELECT {column} FROM usage_events WHERE id = ?))"
            for column in fill_columns
        )
        connection.execute(
            f"UPDATE usage_events SET {assignments} WHERE id = ?",
            (*([absorbed_id] * len(fill_columns)), survivor_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO observation_links "
            "(observation_id, usage_event_id, method, role, confidence) "
            "SELECT observation_id, ?, method, role, confidence FROM observation_links "
            "WHERE usage_event_id = ?",
            (survivor_id, absorbed_id),
        )
        connection.execute(
            "DELETE FROM observation_links WHERE usage_event_id = ?",
            (absorbed_id,),
        )
        connection.execute("DELETE FROM usage_events WHERE id = ?", (absorbed_id,))

    def totals(self, group_by: str | None = None) -> list[UsageTotal]:
        if group_by is not None and group_by not in _GROUP_COLUMNS:
            raise ValueError(f"unsupported group_by: {group_by}")
        group_column = _GROUP_COLUMNS.get(group_by) if group_by is not None else None
        select_group = f"{group_column} AS group_value, " if group_column else "NULL AS group_value, "
        sums = [f"SUM({column}) AS {column}" for column in _TOKEN_COLUMNS.values()]
        sql = (
            f"SELECT {select_group}{', '.join(sums)}, SUM(total_cost_nanos) AS total_cost_nanos "
            "FROM counted_usage_events"
        )
        if group_column:
            sql += f" GROUP BY {group_column} ORDER BY {group_column}"
        else:
            sql += " HAVING COUNT(*) > 0"
        cursor = self.connection.execute(sql)
        return [self._usage_total(_row_dict(cursor, row)) for row in cursor.fetchall()]

    def source_total(self, source: Source) -> UsageTotal:
        sums = [f"SUM({column}) AS {column}" for column in _TOKEN_COLUMNS.values()]
        cursor = self.connection.execute(
            f"SELECT ? AS group_value, {', '.join(sums)}, SUM(total_cost_nanos) AS total_cost_nanos "
            "FROM counted_usage_events WHERE source = ?",
            (source, source),
        )
        row = cursor.fetchone()
        if row is None:
            return UsageTotal(group=source, tokens=TokenUsage(), cost_nanos=None)
        return self._usage_total(_row_dict(cursor, row))

    def reporting_rows(self) -> list[ReportingRow]:
        cursor = self.connection.execute(
            "SELECT e.source, e.granularity, "
            "COALESCE(e.provider, r.provider, s.provider) AS provider, "
            "COALESCE(e.response_model, e.model, r.model, "
            "s.current_model, s.initial_model, '(unknown)') AS resolved_model, "
            "COALESCE(e.model, r.model, s.current_model, s.initial_model) "
            "AS requested_model, "
            "COALESCE(e.started_at_ns, e.completed_at_ns, r.started_at_ns, "
            "r.completed_at_ns, s.started_at_ns, s.updated_at_ns, s.completed_at_ns) "
            "AS occurred_at_ns, e.input_tokens, e.output_tokens, "
            "e.cache_read_tokens, e.cache_write_tokens, e.cache_write_5m_tokens, "
            "e.cache_write_1h_tokens, e.reasoning_tokens, e.reported_total_tokens, "
            "e.derived_total_tokens, e.total_cost_nanos, "
            "COALESCE(e.service_tier, s.service_tier) AS service_tier, "
            "e.speed, e.inference_region "
            "FROM counted_usage_events e "
            "LEFT JOIN runs r ON r.id = e.run_id "
            "LEFT JOIN sessions s ON s.id = COALESCE(e.session_id, r.session_id) "
            "ORDER BY e.source, resolved_model, occurred_at_ns"
        )
        return [
            ReportingRow(
                source=cast(Source, row[0]),
                granularity=cast(Granularity, row[1]),
                provider=cast(str | None, row[2]),
                resolved_model=cast(str, row[3]),
                requested_model=cast(str | None, row[4]),
                occurred_at_ns=cast(int | None, row[5]),
                input_tokens=cast(int | None, row[6]),
                output_tokens=cast(int | None, row[7]),
                cache_read_tokens=cast(int | None, row[8]),
                cache_write_tokens=cast(int | None, row[9]),
                cache_write_5m_tokens=cast(int | None, row[10]),
                cache_write_1h_tokens=cast(int | None, row[11]),
                reasoning_tokens=cast(int | None, row[12]),
                reported_total_tokens=cast(int | None, row[13]),
                derived_total_tokens=cast(int | None, row[14]),
                total_cost_nanos=cast(int | None, row[15]),
                service_tier=cast(str | None, row[16]),
                speed=cast(str | None, row[17]),
                inference_region=cast(str | None, row[18]),
            )
            for row in cursor.fetchall()
        ]

    def _usage_total(self, row: dict[str, object]) -> UsageTotal:
        tokens = TokenUsage(
            **{attribute: cast(int | None, row[column]) for attribute, column in _TOKEN_COLUMNS.items()}
        )
        return UsageTotal(
            group=cast(str | None, row["group_value"]),
            tokens=tokens,
            cost_nanos=cast(int | None, row["total_cost_nanos"]),
        )

    def get_event(self, event_key: str) -> UsageEventDraft | None:
        cursor = self.connection.execute(
            "SELECT * FROM usage_events WHERE event_key = ? ORDER BY id LIMIT 1",
            (event_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        values = _row_dict(cursor, row)
        tokens = TokenUsage(
            **{attribute: cast(int | None, values[column]) for attribute, column in _TOKEN_COLUMNS.items()}
        )
        cost_values: dict[str, Any] = {
            attribute: values[column] for attribute, column in _COST_COLUMNS.items()
        }
        if cost_values["estimated"] is not None:
            cost_values["estimated"] = bool(cost_values["estimated"])
        event_values = {field: values[field] for field in _EVENT_FIELDS}
        for field in ("success",):
            if event_values[field] is not None:
                event_values[field] = bool(event_values[field])
        return UsageEventDraft(
            source=cast(Source, values["source"]),
            event_key=cast(str, values["event_key"]),
            granularity=cast(Any, values["granularity"]),
            status=cast(Any, values["status"]),
            tokens=tokens,
            cost=CostUsage(**cost_values),
            **event_values,
        )

    def list_event_keys(self, source: Source) -> set[str]:
        rows = self.connection.execute(
            "SELECT event_key FROM usage_events WHERE source = ?", (source,)
        ).fetchall()
        return {str(row[0]) for row in rows}

    def channels_for_event(self, event_key: str) -> set[Channel]:
        rows = self.connection.execute(
            "SELECT DISTINCT o.channel FROM observations o "
            "JOIN observation_links l ON l.observation_id = o.id "
            "JOIN usage_events e ON e.id = l.usage_event_id WHERE e.event_key = ?",
            (event_key,),
        ).fetchall()
        return {cast(Channel, row[0]) for row in rows}

    def observation_count(self, source: Source) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM observations WHERE source = ?", (source,)
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def event_count(self, source: Source) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE source = ?", (source,)
        ).fetchone()
        return int(row[0]) if row is not None else 0
