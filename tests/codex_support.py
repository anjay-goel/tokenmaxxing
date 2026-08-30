import json
import sqlite3
from pathlib import Path

from tokenmaxxing.ingest.codex import CodexRoots


def _roots(root: Path) -> CodexRoots:
    return CodexRoots(
        sessions=root / "sessions",
        archived_sessions=root / "archived_sessions",
        state_db=root / "state_5.sqlite",
        thread_history_db=root / "thread_history_1.sqlite",
    )


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _session_meta(session_id: str) -> dict[str, object]:
    return {
        "timestamp": "2026-08-28T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": session_id, "model_provider": "openai"},
    }


def _token_count(
    total: int, last: int, *, output: int | None = None
) -> dict[str, object]:
    total_usage = {"input_tokens": total}
    last_usage = {"input_tokens": last}
    if output is not None:
        total_usage["output_tokens"] = output
        last_usage["output_tokens"] = output
    return {
        "timestamp": "2026-08-28T00:00:01Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": total_usage,
                "last_token_usage": last_usage,
            },
        },
    }


def _create_state_db(path: Path, *, tokens_used: int = 0) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            model_provider TEXT NOT NULL,
            tokens_used INTEGER NOT NULL,
            cli_version TEXT,
            model TEXT,
            reasoning_effort TEXT,
            agent_role TEXT,
            archived_at INTEGER
        );
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT NOT NULL,
            child_thread_id TEXT NOT NULL PRIMARY KEY,
            status TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                "parent-session",
                1_700_000_000,
                1_700_000_010,
                "openai",
                tokens_used,
                "1.0.0",
                "gpt-parent",
                "high",
                "root",
                None,
            ),
            (
                "child-session",
                1_700_000_020,
                1_700_000_030,
                "openai",
                tokens_used,
                "1.0.0",
                "gpt-child",
                "medium",
                "worker",
                None,
            ),
        ),
    )
    connection.execute(
        "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
        ("parent-session", "child-session", "completed"),
    )
    connection.commit()
    connection.close()


def _create_thread_history_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE thread_turns (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            duration_ms INTEGER,
            PRIMARY KEY (thread_id, turn_id)
        )
        """
    )
    connection.execute(
        "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "parent-session",
            "turn-1",
            0,
            "completed",
            1_700_000_001,
            1_700_000_003,
            2_000,
        ),
    )
    connection.commit()
    connection.close()
