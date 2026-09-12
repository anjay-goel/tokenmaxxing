from pathlib import Path

from tokenmaxxing.ingest.codex import sync_codex
from tokenmaxxing.ingest.pi import sync_pi
from tokenmaxxing.ingest.claude import sync_claude
from tokenmaxxing.repository import Repository
from codex_support import _roots, _session_meta, _token_count, _write_jsonl
from claude_support import _assistant, _usage
from pi_support import _header, _assistant as _pi_assistant


def test_codex_collected_history_survives_cleanup(
    repository: Repository, tmp_path: Path
):
    roots = _roots(tmp_path)
    path = roots.sessions / "history.jsonl"
    original = [_session_meta("history"), _token_count(10, 10), _token_count(30, 20)]
    _write_jsonl(path, original)
    sync_codex(repository, roots)
    before = repository.profile_reporting_rows()
    _write_jsonl(path, original[:1])
    sync_codex(repository, roots)
    assert repository.profile_reporting_rows() == before
    path.unlink()
    sync_codex(repository, roots)
    assert repository.profile_reporting_rows() == before
    _write_jsonl(path, original)
    sync_codex(repository, roots)
    assert repository.profile_reporting_rows() == before
    assert sync_codex(repository, roots).lines_read == 0


def test_pi_collected_history_survives_cleanup(repository: Repository, tmp_path: Path):
    path = tmp_path / "sessions" / "history.jsonl"
    original = [_header("history"), _pi_assistant("first", 10, 2)]
    _write_jsonl(path, original)
    sync_pi(repository, path.parent)
    before = repository.profile_reporting_rows()
    _write_jsonl(path, original[:1])
    sync_pi(repository, path.parent)
    assert repository.profile_reporting_rows() == before
    path.unlink()
    sync_pi(repository, path.parent)
    assert repository.profile_reporting_rows() == before
    _write_jsonl(path, original)
    sync_pi(repository, path.parent)
    assert repository.profile_reporting_rows() == before


def test_claude_collected_history_survives_replacement(
    repository: Repository, tmp_path: Path
):
    path = tmp_path / "history.jsonl"
    _write_jsonl(path, [_assistant("old", "old", usage=_usage(output_tokens=10))])
    sync_claude(repository, path)
    _write_jsonl(path, [_assistant("new", "new", usage=_usage(output_tokens=2))])
    sync_claude(repository, path)
    assert repository.source_total("claude").tokens.output == 12


def test_later_projection_cannot_clear_collected_metadata(repository: Repository):
    from tokenmaxxing.models import Projection, UsageEventDraft, TokenUsage

    repository.apply_projection(
        Projection(
            events=(
                UsageEventDraft(
                    source="claude",
                    event_key="metadata",
                    granularity="model_call",
                    status="canonical",
                    tokens=TokenUsage(input=10),
                    provider="anthropic",
                    model="known-model",
                    completed_at_ns=123,
                    service_tier="priority",
                ),
            )
        )
    )
    repository.apply_projection(
        Projection(
            events=(
                UsageEventDraft(
                    source="claude",
                    event_key="metadata",
                    granularity="model_call",
                    status="canonical",
                    tokens=TokenUsage(input=10),
                    replace_metadata_fields=frozenset(
                        {"provider", "model", "completed_at_ns", "service_tier"}
                    ),
                ),
            )
        )
    )
    event = repository.get_event("metadata")
    assert event is not None
    assert (event.provider, event.model, event.completed_at_ns, event.service_tier) == (
        "anthropic",
        "known-model",
        123,
        "priority",
    )


def test_rebuild_recovers_history_without_source_files(
    repository: Repository, tmp_path: Path
):
    from tokenmaxxing.history import rebuild_history

    path = tmp_path / "history.jsonl"
    _write_jsonl(path, [_header("history"), _pi_assistant("first", 10, 2)])
    sync_pi(repository, path)
    before = repository.profile_reporting_rows()
    path.unlink()
    repository.connection.execute(
        "UPDATE usage_events SET status='excluded' WHERE source='pi'"
    )
    rebuild_history(repository, ("pi",))
    assert repository.profile_reporting_rows() == before
    rebuild_history(repository, ("pi",))
    assert repository.profile_reporting_rows() == before


def test_rebuild_rolls_back_every_source_on_failure(
    repository: Repository, tmp_path: Path, monkeypatch
):
    import pytest
    import tokenmaxxing.history as history

    path = tmp_path / "history.jsonl"
    _write_jsonl(path, [_header("history"), _pi_assistant("first", 10, 2)])
    sync_pi(repository, path)
    repository.connection.execute(
        "UPDATE usage_events SET status='excluded' WHERE source='pi'"
    )

    def fail(_):
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(history, "recover_opencode_history", fail)
    with pytest.raises(RuntimeError, match="recovery failed"):
        history.rebuild_history(repository, ("pi", "opencode"))
    assert repository.profile_reporting_rows() == []


def test_claude_revised_timestamp_keeps_original_activity_day(
    repository: Repository, tmp_path: Path
):
    path = tmp_path / "claude.jsonl"
    original = _assistant("dated", "entry", usage=_usage(output_tokens=10))
    _write_jsonl(path, [original])
    sync_claude(repository, path)
    before = repository.profile_reporting_rows()
    for timestamp in ("2026-08-29T00:00:00Z", "2026-08-27T00:00:00Z"):
        _write_jsonl(path, [{**original, "timestamp": timestamp}])
        sync_claude(repository, path)
        assert repository.profile_reporting_rows() == before


def test_pi_revised_timestamp_keeps_original_activity_day(
    repository: Repository, tmp_path: Path
):
    path = tmp_path / "pi.jsonl"
    original = _pi_assistant("dated", 10, 2)
    _write_jsonl(path, [_header("history"), original])
    sync_pi(repository, path)
    before = repository.profile_reporting_rows()
    for timestamp, milliseconds in (
        ("2026-08-29T00:00:00Z", 1787961600000),
        ("2026-08-27T00:00:00Z", 1787788800000),
    ):
        revised = {
            **original,
            "timestamp": timestamp,
            "message": {**original["message"], "timestamp": milliseconds},
        }
        _write_jsonl(path, [_header("history"), revised])
        sync_pi(repository, path)
        assert repository.profile_reporting_rows() == before


def test_pi_reported_total_revision_cannot_shrink_collected_usage(
    repository: Repository, tmp_path: Path
):
    from tokenmaxxing.reporting import event_total

    path = tmp_path / "pi-revision.jsonl"
    original = _pi_assistant("stable", 100, 0)
    original["message"]["usage"].pop("totalTokens")
    _write_jsonl(path, [_header("history"), original])
    sync_pi(repository, path)
    assert (
        sum(event_total(row.usage) for row in repository.profile_reporting_rows())
        == 100
    )
    revised = _pi_assistant("stable", 110, 0)
    revised["message"]["usage"]["totalTokens"] = 10
    _write_jsonl(path, [_header("history"), revised])
    sync_pi(repository, path)
    assert (
        sum(event_total(row.usage) for row in repository.profile_reporting_rows())
        == 100
    )
