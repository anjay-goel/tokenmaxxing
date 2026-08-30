from pathlib import Path

import pytest

from tokenmaxxing import sync
from tokenmaxxing.ingest.opencode import OpenCodeRoots
from tokenmaxxing.models import SyncStats
from tokenmaxxing.repository import Repository


def _roots(root: Path) -> sync.SourceRoots:
    return sync.SourceRoots(
        codex=root / "codex",
        claude=root / "claude",
        pi=root / "pi",
        opencode_db=root / "opencode" / "opencode.db",
    )


def _create_roots(roots: sync.SourceRoots) -> None:
    roots.codex.mkdir(parents=True)
    roots.claude.mkdir()
    roots.pi.mkdir()
    roots.opencode_db.parent.mkdir()
    roots.opencode_db.touch()


def test_defaults_use_standard_local_history_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert sync.SourceRoots.defaults(tmp_path) == sync.SourceRoots(
        codex=tmp_path / ".codex",
        claude=tmp_path / ".claude" / "projects",
        pi=tmp_path / ".pi" / "agent" / "sessions",
        opencode_db=tmp_path / ".local" / "share" / "opencode" / "opencode.db",
    )


def test_windows_opencode_default_remains_under_user_profile(tmp_path: Path) -> None:
    roots = sync.SourceRoots.defaults(home=tmp_path, environ={}, platform="win32")

    assert roots.opencode_db == (
        tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    )


def test_windows_opencode_default_ignores_xdg_data_home(tmp_path: Path) -> None:
    roots = sync.SourceRoots.defaults(
        home=tmp_path,
        environ={"XDG_DATA_HOME": str(tmp_path / "xdg")},
        platform="win32",
    )

    assert roots.opencode_db == (
        tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    )


def test_defaults_use_xdg_data_location_for_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert sync.SourceRoots.defaults(tmp_path, platform="linux").opencode_db == (
        tmp_path / "data" / "opencode" / "opencode.db"
    )


def test_syncs_requested_sources_in_order(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    _create_roots(roots)
    calls: list[str] = []

    def importer(source: str) -> SyncStats:
        calls.append(source)
        return SyncStats(artifacts_seen=1)

    monkeypatch.setattr(sync, "sync_codex", lambda repository, roots: importer("codex"))
    monkeypatch.setattr(
        sync, "sync_claude", lambda repository, root: importer("claude")
    )
    monkeypatch.setattr(sync, "sync_pi", lambda repository, root: importer("pi"))
    monkeypatch.setattr(
        sync, "sync_opencode", lambda repository, roots: importer("opencode")
    )

    results = sync.sync_sources(
        repository, roots, ("codex", "claude", "pi", "opencode")
    )

    assert calls == ["codex", "claude", "pi", "opencode"]
    assert [result.source for result in results] == [
        "codex",
        "claude",
        "pi",
        "opencode",
    ]
    assert all(result.status == "ok" for result in results)
    assert all(result.stats == SyncStats(artifacts_seen=1) for result in results)


def test_missing_roots_are_skipped(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)

    def unexpected_call(*args: object) -> SyncStats:
        raise AssertionError("missing roots must not be imported")

    monkeypatch.setattr(sync, "sync_codex", unexpected_call)
    monkeypatch.setattr(sync, "sync_claude", unexpected_call)
    monkeypatch.setattr(sync, "sync_pi", unexpected_call)
    monkeypatch.setattr(sync, "sync_opencode", unexpected_call)

    results = sync.sync_sources(
        repository, roots, ("codex", "claude", "pi", "opencode")
    )

    assert [(result.source, result.status) for result in results] == [
        ("codex", "skipped"),
        ("claude", "skipped"),
        ("pi", "skipped"),
        ("opencode", "skipped"),
    ]
    assert all(result.stats == SyncStats() for result in results)
    assert all(result.error_category is None for result in results)


def test_opencode_uses_the_configured_database_path(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = sync.SourceRoots(
        codex=tmp_path / "codex",
        claude=tmp_path / "claude",
        pi=tmp_path / "pi",
        opencode_db=tmp_path / "custom" / "history.sqlite",
    )
    roots.opencode_db.parent.mkdir()
    roots.opencode_db.touch()
    databases: list[Path] = []

    def importer(repository: Repository, importer_roots: OpenCodeRoots) -> SyncStats:
        databases.append(importer_roots.database)
        return SyncStats()

    monkeypatch.setattr(sync, "sync_opencode", importer)

    results = sync.sync_sources(repository, roots, ("opencode",))

    assert results == (sync.SourceSyncResult(source="opencode", status="ok"),)
    assert databases == [roots.opencode_db]


def test_sync_continues_after_content_free_import_error(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    _create_roots(roots)
    calls: list[str] = []

    def success(source: str) -> SyncStats:
        calls.append(source)
        return SyncStats(lines_read=1)

    def failing_import(repository: Repository, root: Path) -> SyncStats:
        calls.append("claude")
        raise ValueError("PRIVATE_CLAUDE_HISTORY_CONTENT")

    monkeypatch.setattr(sync, "sync_codex", lambda repository, roots: success("codex"))
    monkeypatch.setattr(sync, "sync_claude", failing_import)
    monkeypatch.setattr(sync, "sync_pi", lambda repository, root: success("pi"))
    monkeypatch.setattr(
        sync, "sync_opencode", lambda repository, roots: success("opencode")
    )

    results = sync.sync_sources(
        repository, roots, ("codex", "claude", "pi", "opencode")
    )

    assert calls == ["codex", "claude", "pi", "opencode"]
    assert [(result.source, result.status) for result in results] == [
        ("codex", "ok"),
        ("claude", "error"),
        ("pi", "ok"),
        ("opencode", "ok"),
    ]
    error = results[1]
    assert error.stats == SyncStats()
    assert error.error_category == "ValueError"
    assert "PRIVATE_CLAUDE_HISTORY_CONTENT" not in repr(error)


def test_sync_debug_mode_raises_the_original_import_error(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    roots.codex.mkdir()
    expected = ValueError("PRIVATE_CODEX_HISTORY_CONTENT")

    def failing_import(*args: object) -> SyncStats:
        raise expected

    monkeypatch.setattr(sync, "sync_codex", failing_import)

    with pytest.raises(ValueError) as raised:
        sync.sync_sources(repository, roots, ("codex",), raise_errors=True)

    assert raised.value is expected
