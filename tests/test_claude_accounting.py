from pathlib import Path

import pytest

from tokenmaxxing.ingest.claude import sync_claude
from tokenmaxxing.models import (
    TokenUsage,
)
from tokenmaxxing.repository import Repository
from claude_support import (
    _assistant,
    _copy,
    _usage,
    _write_jsonl,
)


@pytest.fixture
def claude_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "claude"


def test_progressive_snapshots_count_component_max_once(
    repository: Repository, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "progressive.jsonl")

    assert repository.observation_count("claude") == 3
    assert repository.event_count("claude") == 1
    assert repository.source_total("claude").tokens == TokenUsage(
        input=2,
        output=21,
        cache_read=20,
        cache_write=3,
        cache_write_5m=1,
        cache_write_1h=2,
    )


def test_iterations_replace_outer_and_keep_advisor_model(
    repository: Repository, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "iterations.jsonl")

    assert repository.list_event_keys("claude") == {
        "claude:msg_1:iteration:0",
        "claude:msg_1:iteration:1",
    }
    advisor = repository.get_event("claude:msg_1:iteration:1")
    assert advisor is not None
    assert advisor.model == "advisor-model"
    assert repository.source_total("claude").tokens.output == 19


def test_residual_counts_only_unexplained_positive_outer_usage(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "residual.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                "msg_residual",
                "entry-residual",
                usage=_usage(input_tokens=2, output_tokens=15),
                iterations=[
                    {"type": "message", **_usage(input_tokens=2, output_tokens=10)},
                    {
                        "type": "advisor_message",
                        "model": "advisor-model",
                        **_usage(input_tokens=20, output_tokens=7),
                    },
                ],
            )
        ],
    )

    sync_claude(repository, path)

    residual = repository.get_event("claude:msg_residual:residual")
    assert residual is not None
    assert residual.tokens == TokenUsage(
        input=0,
        output=5,
        cache_read=0,
        cache_write=0,
        cache_write_5m=0,
        cache_write_1h=0,
    )
    assert repository.source_total("claude").tokens.output == 22


def test_negative_outer_minus_normal_iteration_is_conflicted(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "conflict.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                "msg_conflict",
                "entry-conflict",
                usage=_usage(input_tokens=1, output_tokens=5),
                iterations=[
                    {"type": "message", **_usage(input_tokens=1, output_tokens=10)},
                    {
                        "type": "advisor_message",
                        "model": "advisor-model",
                        **_usage(input_tokens=20, output_tokens=3),
                    },
                ],
            )
        ],
    )

    sync_claude(repository, path)

    normal = repository.get_event("claude:msg_conflict:iteration:0")
    advisor = repository.get_event("claude:msg_conflict:iteration:1")
    assert normal is not None and normal.status == "conflicted"
    assert advisor is not None and advisor.status == "canonical"
    assert repository.source_total("claude").tokens.output == 3
    assert repository.connection.execute(
        "SELECT category FROM issues WHERE source = 'claude'"
    ).fetchone() == ("iteration_usage_conflict",)


def test_usage_metadata_is_preserved_without_private_source_fields(
    repository: Repository, db_path: Path, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "progressive.jsonl")

    event = repository.get_event("claude:msg_progressive")
    assert event is not None
    assert event.service_tier == "standard"
    assert event.speed == "fast"
    assert event.inference_region == "us"
    assert event.effort == "high"
    assert event.web_search_count == 1
    assert event.web_fetch_count == 2
    assert repository.connection.execute(
        "SELECT request_id FROM usage_events WHERE source = 'claude'"
    ).fetchone() == ("req-progressive",)
    assert repository.connection.execute(
        "SELECT harness_version FROM sessions WHERE source = 'claude'"
    ).fetchone() == ("2.1.232",)
    assert repository.connection.execute(
        "SELECT event_type, client_id FROM observations WHERE source = 'claude' "
        "ORDER BY ordinal DESC LIMIT 1"
    ).fetchone() == ("assistant", "cli")
    database_files = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )
    database_bytes = b"".join(
        path.read_bytes() for path in database_files if path.exists()
    )
    assert b"PRIVATE_PROGRESSIVE_SENTINEL" not in database_bytes
    assert str(claude_fixtures).encode() not in database_bytes


def test_unknown_numeric_usage_metadata_is_kept_but_strings_are_dropped(
    repository: Repository, db_path: Path, tmp_path: Path
) -> None:
    path = tmp_path / "extended-usage.jsonl"
    record = _assistant("msg_extended", "entry-extended")
    assert isinstance(record["message"], dict)
    assert isinstance(record["message"]["usage"], dict)
    record["message"]["usage"].update(
        {
            "new_counter": 7,
            "new_flags": {"accelerated": True},
            "new_label": "PRIVATE_ARBITRARY_STRING",
        }
    )
    _write_jsonl(path, [record])

    sync_claude(repository, path)

    projection = repository.connection.execute(
        "SELECT projection_json FROM observations WHERE source = 'claude'"
    ).fetchone()
    assert projection is not None
    assert '"new_counter":7' in projection[0]
    assert '"accelerated":true' in projection[0]
    database_files = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )
    database_bytes = b"".join(
        database_file.read_bytes()
        for database_file in database_files
        if database_file.exists()
    )
    assert b"PRIVATE_ARBITRARY_STRING" not in database_bytes


def test_root_and_subagent_copies_canonicalize_globally(
    repository: Repository, tmp_path: Path, claude_fixtures: Path
) -> None:
    root = tmp_path / "project"
    _copy(claude_fixtures / "root.jsonl", root / "root.jsonl")
    _copy(
        claude_fixtures / "subagents" / "agent-a.jsonl",
        root / "subagents" / "agent-a.jsonl",
    )

    sync_claude(repository, root)

    assert repository.observation_count("claude") == 3
    assert repository.list_event_keys("claude") == {
        "claude:msg_shared",
        "claude:msg_agent",
    }
    assert repository.source_total("claude").tokens.output == 15
    assert repository.connection.execute(
        "SELECT event_type, client_id FROM observations "
        "WHERE source = 'claude' AND response_id = 'msg_agent'"
    ).fetchone() == ("assistant_sidechain", "cli")


def test_missing_message_id_falls_back_to_session_and_transcript_uuid(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "fallback.jsonl"
    _write_jsonl(
        path,
        [_assistant(None, "entry-fallback", session_id="fallback-session")],
    )

    sync_claude(repository, path)

    assert repository.list_event_keys("claude") == {
        "claude:fallback-session:entry-fallback"
    }


def test_malformed_usage_records_issue_and_does_not_block_later_lines(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "malformed.jsonl"
    malformed = _assistant("msg_bad", "entry-bad")
    assert isinstance(malformed["message"], dict)
    malformed["message"]["usage"] = "PRIVATE_MALFORMED_SENTINEL"
    _write_jsonl(
        path,
        [
            _assistant("msg_before", "entry-before"),
            malformed,
            _assistant("msg_after", "entry-after"),
        ],
    )

    sync_claude(repository, path)

    assert repository.list_event_keys("claude") == {
        "claude:msg_before",
        "claude:msg_after",
    }
    assert repository.connection.execute(
        "SELECT category, observed_type FROM issues WHERE source = 'claude'"
    ).fetchone() == ("invalid_usage", "str")
