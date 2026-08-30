import sqlite3
from pathlib import Path

import pytest

from tokenmaxxing import db as db_module
from tokenmaxxing.db import Database


def test_fresh_database_has_complete_schema(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "usage.sqlite3")
    try:
        names = db.object_names()
        assert {
            "sessions",
            "runs",
            "turns",
            "usage_events",
            "observations",
            "observation_links",
            "samples",
            "artifacts",
            "ingest_runs",
            "issues",
            "counted_usage_events",
        } <= names
        assert db.pragma("journal_mode") == "wal"
        assert db.pragma("foreign_keys") == 1
        assert db.pragma("user_version") == 7
        observation_indexes = db.connection.execute(
            "PRAGMA index_list(observations)"
        ).fetchall()
        assert any(
            row[1] == "idx_observations_claude_turn_artifact"
            for row in observation_indexes
        )
    finally:
        db.close()


def test_opening_an_already_migrated_database_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    first = Database.open(path)
    first.close()

    second = Database.open(path)
    try:
        assert second.pragma("user_version") == 7
        assert "counted_usage_events" in second.object_names()
    finally:
        second.close()


def test_active_issue_identity_lookups_use_an_index(database: Database) -> None:
    connection = database.connection
    for identifier, field_path in (("message-null", None), ("message-field", "usage.iterations")):
        connection.execute(
            "INSERT INTO issues (source, category, severity, identifier, field_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("claude", "iteration_usage_conflict", "error", identifier, field_path),
        )
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM issues "
                "WHERE source = ? AND category = ? AND identifier = ? "
                "AND field_path IS ? AND resolved_at_ns IS NULL",
                ("claude", "iteration_usage_conflict", identifier, field_path),
            ).fetchall()
        ]

        assert any(
            "SEARCH issues USING INDEX idx_issues_active_identity "
            "(source=? AND category=? AND identifier=? AND field_path=?)" in detail
            for detail in details
        )


def test_opening_a_pre_release_database_adds_reconciliation_indexes(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    connection = sqlite3.connect(path)
    try:
        migrations = Path(db_module.__file__).with_name("migrations")
        connection.executescript((migrations / "0001_initial.sql").read_text(encoding="utf-8"))
        connection.executescript(
            (migrations / "0002_artifact_ordinal.sql").read_text(encoding="utf-8")
        )
        connection.execute("PRAGMA user_version = 2")
    finally:
        connection.close()

    database = Database.open(path)
    try:
        assert database.pragma("user_version") == 7
        observation_indexes = database.connection.execute(
            "PRAGMA index_list(observations)"
        ).fetchall()
        assert any(row[1] == "idx_observations_source_turn" for row in observation_indexes)
        assert any(
            row[1] == "idx_observations_codex_owner_artifact"
            for row in observation_indexes
        )
        assert any(
            row[1] == "idx_observations_codex_owner_link" for row in observation_indexes
        )
        assert any(
            row[1] == "idx_observations_claude_turn_artifact"
            for row in observation_indexes
        )
        artifact_indexes = database.connection.execute("PRAGMA index_list(artifacts)").fetchall()
        assert any(
            row[1] == "idx_artifacts_source_path_generation" for row in artifact_indexes
        )
        issue_indexes = database.connection.execute("PRAGMA index_list(issues)").fetchall()
        assert any(row[1] == "idx_issues_active_identity" for row in issue_indexes)
    finally:
        database.close()


def test_transactions_roll_back_and_enforce_foreign_keys(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "usage.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO runs (session_id, source_run_id) VALUES (?, ?)",
                    (999, "missing-session"),
                )

        with pytest.raises(RuntimeError, match="abort"):
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO sessions (source, source_session_id) VALUES (?, ?)",
                    ("codex", "rolled-back"),
                )
                raise RuntimeError("abort")

        rows = db.connection.execute(
            "SELECT source_session_id FROM sessions WHERE source_session_id = ?",
            ("rolled-back",),
        ).fetchall()
        assert rows == []
    finally:
        db.close()


def test_failed_migration_does_not_advance_database_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "usage.sqlite3"
    migration = tmp_path / "0001_broken.sql"
    migration.write_text("CREATE TABLE partial_table (id INTEGER); INVALID SQL;", encoding="utf-8")
    monkeypatch.setattr(db_module, "_migration_paths", lambda: [migration])

    with pytest.raises(sqlite3.OperationalError):
        Database.open(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'partial_table'"
        ).fetchone() is None
    finally:
        connection.close()
