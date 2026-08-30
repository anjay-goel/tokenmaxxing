import json
from pathlib import Path

import pytest

from tokenmaxxing.ingest.pi import sync_pi
from tokenmaxxing.models import TokenUsage
from tokenmaxxing.repository import Repository
from pi_support import (
    _assistant,
    _header,
    _write_jsonl,
)


@pytest.fixture
def pi_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "pi"


def _database_bytes(db_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
        if path.exists()
    )


def test_direct_usage_keeps_all_safe_metadata_without_retained_content(
    repository: Repository, db_path: Path, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "tree.jsonl")

    assert repository.list_event_keys("pi") == {
        "pi:root:assistant-1:assistant",
        "pi:root:tool-1:tool_result",
        "pi:root:compact-1:compaction",
        "pi:root:summary-1:branch_summary",
    }
    assert repository.source_total("pi").tokens == TokenUsage(
        input=25,
        output=15,
        cache_read=7,
        cache_write=4,
        cache_write_5m=2,
        cache_write_1h=1,
        reasoning=5,
        reported_total=51,
        derived_total=51,
    )
    assistant = repository.get_event("pi:root:assistant-1:assistant")
    assert assistant is not None
    assert assistant.provider == "test-provider"
    assert assistant.api == "responses"
    assert assistant.model == "test-model"
    assert assistant.response_model == "resolved-model"
    assert assistant.stop_reason == "stop"
    assert assistant.cost.total_nanos == 29_000_000
    assert repository.connection.execute(
        "SELECT response_id FROM usage_events WHERE event_key = ?",
        (assistant.event_key,),
    ).fetchone() == ("response-1",)
    assert repository.connection.execute(
        "SELECT value_integer FROM samples WHERE source = 'pi' AND name = 'tokens_before'"
    ).fetchone() == (1234,)
    assert repository.connection.execute(
        "SELECT current_model, reasoning_effort FROM sessions "
        "WHERE source_session_id = 'root'"
    ).fetchone() == ("test-model", "high")
    projection = repository.connection.execute(
        "SELECT projection_json FROM observations WHERE source = 'pi' "
        "AND event_type = 'assistant'"
    ).fetchone()
    assert projection is not None
    assert '"reasoning":2' in projection[0]
    assert '"futureCounter":7' in projection[0]
    assert '"futureFlag":true' in projection[0]
    database_bytes = _database_bytes(db_path)
    for sentinel in (
        b"PRIVATE_ASSISTANT_SENTINEL",
        b"PRIVATE_TOOL_RESULT_SENTINEL",
        b"PRIVATE_COMPACTION_SENTINEL",
        b"PRIVATE_BRANCH_SENTINEL",
        b"PRIVATE_USAGE_STRING_SENTINEL",
        b"RETAINED_SENTINEL",
        b"PRIVATE_CWD_SENTINEL",
    ):
        assert sentinel not in database_bytes
    assert str(pi_fixtures).encode() not in database_bytes


def test_pi_float_costs_are_rounded_to_the_nearest_nanodollar(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "precise-cost.jsonl"
    assistant = _assistant("precise-cost", 1, 1)
    message = assistant["message"]
    assert isinstance(message, dict)
    usage = message["usage"]
    assert isinstance(usage, dict)
    usage["cost"] = {
        "input": 0.0000000006,
        "output": 0.0000000004,
        "cacheRead": 0,
        "cacheWrite": 0,
        "total": 0.0000000006,
    }
    _write_jsonl(path, [_header("precise-cost"), assistant])

    sync_pi(repository, path)

    event = repository.get_event("pi:precise-cost:precise-cost:assistant")
    assert event is not None
    assert event.cost.input_nanos == 1
    assert event.cost.output_nanos == 0
    assert event.cost.total_nanos == 1
    assert event.cost.original_decimal == "6e-10"


def test_session_preserves_initial_model_across_later_model_changes(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "models.jsonl"
    _write_jsonl(
        path,
        [
            _header("models"),
            {
                "type": "model_change",
                "id": "model-first",
                "parentId": None,
                "timestamp": "2026-08-28T00:00:01Z",
                "provider": "provider-one",
                "modelId": "model-one",
            },
            {
                "type": "model_change",
                "id": "model-second",
                "parentId": "model-first",
                "timestamp": "2026-08-28T00:00:02Z",
                "provider": "provider-two",
                "modelId": "model-two",
            },
        ],
    )

    sync_pi(repository, path)

    assert repository.connection.execute(
        "SELECT initial_model, current_model, provider FROM sessions "
        "WHERE source_session_id = 'models'"
    ).fetchone() == ("model-one", "model-two", "provider-two")


def test_incremental_model_change_keeps_previously_imported_initial_model(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "incremental-model.jsonl"
    first_model = {
        "type": "model_change",
        "id": "model-first",
        "parentId": None,
        "timestamp": "2026-08-28T00:00:01Z",
        "provider": "provider-one",
        "modelId": "model-one",
    }
    second_model = {
        "type": "model_change",
        "id": "model-second",
        "parentId": "model-first",
        "timestamp": "2026-08-28T00:00:02Z",
        "provider": "provider-two",
        "modelId": "model-two",
    }
    _write_jsonl(path, [_header("incremental-model"), first_model])
    sync_pi(repository, path)
    with path.open("a", encoding="utf-8") as session_file:
        session_file.write(json.dumps(second_model, separators=(",", ":")) + "\n")

    sync_pi(repository, path)

    assert repository.connection.execute(
        "SELECT initial_model, current_model, provider FROM sessions "
        "WHERE source_session_id = 'incremental-model'"
    ).fetchone() == ("model-one", "model-two", "provider-two")
