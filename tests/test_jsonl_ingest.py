from collections.abc import Callable
from pathlib import Path

import pytest

from tokenmaxxing.ingest.jsonl import SourceLine, scan_jsonl
from tokenmaxxing.models import ObservationDraft, Projection
from tokenmaxxing.repository import Repository


def project_line(line: SourceLine, _: object) -> Projection:
    return Projection(
        observations=(
            ObservationDraft(
                source="codex",
                channel="disk",
                stable_key=f"line:{line.artifact_id}:{line.generation}:{line.ordinal}",
                event_type=str(line.value["type"]),
                observed_at_ns=1,
                parser_version="test",
                projection={"usage": {"ordinal": line.ordinal}},
                artifact_id=line.artifact_id,
                ordinal=line.ordinal,
            ),
        )
    )


@pytest.fixture
def scanner(repository: Repository) -> Callable[[Path, list[SourceLine]], object]:
    def scan(path: Path, lines: list[SourceLine]) -> object:
        return scan_jsonl(
            repository,
            "codex",
            path,
            lambda line, state: (lines.append(line), project_line(line, state))[1],
            object(),
        )

    return scan


def test_fresh_read_repeat_and_append_are_incremental(
    tmp_path: Path, scanner: Callable[[Path, list[SourceLine]], object]
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\n')
    first_lines: list[SourceLine] = []

    first = scanner(path, first_lines)
    assert [line.ordinal for line in first_lines] == [0]
    assert first.artifacts_seen == 1
    assert first.lines_read == 1

    repeated_lines: list[SourceLine] = []
    repeated = scanner(path, repeated_lines)
    assert repeated_lines == []
    assert repeated.lines_read == 0

    path.write_bytes(path.read_bytes() + b'{"type":"two"}\n')
    appended_lines: list[SourceLine] = []
    appended = scanner(path, appended_lines)
    assert [line.ordinal for line in appended_lines] == [1]
    assert appended.lines_read == 1


def test_partial_line_waits_for_newline(
    tmp_path: Path, scanner: Callable[[Path, list[SourceLine]], object]
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\n{"type":"two"}')

    first_lines: list[SourceLine] = []
    scanner(path, first_lines)
    assert [line.ordinal for line in first_lines] == [0]

    path.write_bytes(path.read_bytes() + b"\n")
    second_lines: list[SourceLine] = []
    scanner(path, second_lines)
    assert [line.ordinal for line in second_lines] == [1]


def test_shrink_after_a_long_partial_tail_starts_a_new_generation(
    tmp_path: Path, scanner: Callable[[Path, list[SourceLine]], object]
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\n' + b"x" * 5000)
    first_lines: list[SourceLine] = []
    scanner(path, first_lines)

    path.write_bytes(path.read_bytes()[:-500])
    replacement_lines: list[SourceLine] = []
    scanner(path, replacement_lines)

    assert [line.generation for line in replacement_lines] == [1]
    assert [line.ordinal for line in replacement_lines] == [0]


def test_missing_header_after_a_long_unchanged_prefix_starts_a_new_generation(
    tmp_path: Path, scanner: Callable[[Path, list[SourceLine]], object]
) -> None:
    path = tmp_path / "session.jsonl"
    prefix = b'{"type":"session_meta","payload":{"padding":"' + b"x" * 5000
    path.write_bytes(prefix + b'","id":"first"}}\n')
    first_lines: list[SourceLine] = []
    scanner(path, first_lines)

    path.write_bytes(prefix + b'","replacement":"longer-than-first"}}\n')
    replacement_lines: list[SourceLine] = []
    scanner(path, replacement_lines)

    assert [line.generation for line in replacement_lines] == [1]
    assert [line.ordinal for line in replacement_lines] == [0]


@pytest.mark.parametrize(
    ("replacement", "expected_generation"),
    [
        (b'{"type":"reset"}\n', 1),
        (b'{"type":"changed"}\n{"type":"more"}\n', 1),
    ],
    ids=["shrink", "prefix_replacement"],
)
def test_shrink_or_prefix_replacement_starts_a_new_generation(
    tmp_path: Path,
    scanner: Callable[[Path, list[SourceLine]], object],
    replacement: bytes,
    expected_generation: int,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\n{"type":"two"}\n')
    initial_lines: list[SourceLine] = []
    scanner(path, initial_lines)

    path.write_bytes(replacement)
    replacement_lines: list[SourceLine] = []
    scanner(path, replacement_lines)

    assert [line.generation for line in replacement_lines] == [expected_generation] * replacement.count(b"\n")
    assert [line.ordinal for line in replacement_lines] == list(range(replacement.count(b"\n")))
    assert replacement_lines[0].artifact_id != initial_lines[0].artifact_id


def test_header_and_inode_replacement_start_a_new_generation(
    tmp_path: Path, scanner: Callable[[Path, list[SourceLine]], object]
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"session_meta","payload":{"id":"first"}}\n')
    first_lines: list[SourceLine] = []
    scanner(path, first_lines)

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b'{"type":"session_meta","payload":{"id":"second"}}\n')
    replacement.replace(path)
    replacement_lines: list[SourceLine] = []
    scanner(path, replacement_lines)

    assert [line.generation for line in replacement_lines] == [1]
    assert replacement_lines[0].artifact_id != first_lines[0].artifact_id


def test_rename_and_copy_reparse_as_distinct_artifacts_without_duplicate_semantic_projection(
    tmp_path: Path, repository: Repository
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\n')

    def project_semantic(line: SourceLine, _: object) -> Projection:
        return Projection(
            observations=(
                ObservationDraft(
                    source="codex",
                    channel="disk",
                    stable_key=f"semantic:{line.value['type']}",
                    event_type="usage",
                    observed_at_ns=1,
                    parser_version="test",
                    projection={"usage": {"value": 1}},
                    artifact_id=line.artifact_id,
                    ordinal=line.ordinal,
                ),
            )
        )

    scan_jsonl(repository, "codex", path, project_semantic, object())
    renamed = tmp_path / "renamed.jsonl"
    path.rename(renamed)
    scan_jsonl(repository, "codex", renamed, project_semantic, object())
    copied = tmp_path / "copied.jsonl"
    copied.write_bytes(renamed.read_bytes())
    scan_jsonl(repository, "codex", copied, project_semantic, object())

    assert repository.observation_count("codex") == 1
    artifacts = repository.connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()
    assert artifacts == (3,)


def test_bad_json_records_a_content_free_issue_and_keeps_its_cursor(
    tmp_path: Path, repository: Repository
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\nnot-json\n{"type":"three"}\n')
    first_lines: list[SourceLine] = []

    scan_jsonl(
        repository,
        "codex",
        path,
        lambda line, state: (first_lines.append(line), project_line(line, state))[1],
        object(),
    )

    assert [line.ordinal for line in first_lines] == [0]
    issue = repository.connection.execute(
        "SELECT category, severity, identifier, field_path, observed_type FROM issues"
    ).fetchone()
    assert issue == (
        "invalid_jsonl",
        "error",
        "artifact:1:generation:0:ordinal:1",
        "1",
        "JSONDecodeError",
    )
    artifact = repository.connection.execute(
        "SELECT byte_offset FROM artifacts"
    ).fetchone()
    assert artifact == (len(b'{"type":"one"}\n'),)

    second_lines: list[SourceLine] = []
    scan_jsonl(
        repository,
        "codex",
        path,
        lambda line, state: (second_lines.append(line), project_line(line, state))[1],
        object(),
    )
    assert second_lines == []


def test_projection_and_cursor_commit_together(
    tmp_path: Path, repository: Repository
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"one"}\n{"type":"two"}\n')

    def fail_on_second(line: SourceLine, state: object) -> Projection:
        if line.ordinal == 1:
            raise ValueError("projection failed")
        return project_line(line, state)

    with pytest.raises(ValueError, match="projection failed"):
        scan_jsonl(repository, "codex", path, fail_on_second, object())

    assert repository.observation_count("codex") == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (0,)
