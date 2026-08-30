import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tokenmaxxing.config import hash_workspace, load_or_create_salt
from tokenmaxxing.models import IssueDraft, Projection, Source, SyncStats, WriteStats
from tokenmaxxing.repository import Repository


_PREFIX_BYTES = 4096
_SQLITE_INT_MAX = 2**63 - 1
_UINT64_MODULUS = 2**64


@dataclass(frozen=True, slots=True)
class SourceLine:
    artifact_id: int
    generation: int
    ordinal: int
    byte_start: int
    byte_end: int
    value: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Artifact:
    id: int
    generation: int
    device: int | None
    inode: int | None
    size_bytes: int
    byte_offset: int
    prefix_fingerprint: str | None
    header_session_id: str | None
    last_complete_ordinal: int
    is_missing: bool


def _sqlite_file_id(value: int) -> int:
    if -2**63 <= value <= _SQLITE_INT_MAX:
        return value
    if 0 <= value < _UINT64_MODULUS:
        return value - _UINT64_MODULUS
    raise OverflowError("file identity does not fit in 64 bits")


def _database_path(connection: sqlite3.Connection) -> Path:
    rows = connection.execute("PRAGMA database_list").fetchall()
    for _, name, filename in rows:
        if name == "main" and filename:
            return Path(cast(str, filename))
    raise ValueError("repository database must be file-backed")


def _path_hash(connection: sqlite3.Connection, path: Path) -> str:
    salt = load_or_create_salt(_database_path(connection).parent / "salt")
    return hash_workspace(str(path.resolve()), salt)


def _prefix_fingerprint(path: Path, size_bytes: int) -> str:
    with path.open("rb") as artifact_file:
        prefix = artifact_file.read(min(size_bytes, _PREFIX_BYTES))
    return hashlib.blake2b(prefix, digest_size=32).hexdigest()


def _header_session_id(path: Path) -> str | None:
    with path.open("rb") as artifact_file:
        header = artifact_file.readline()
    if not header.endswith(b"\n"):
        return None
    try:
        value = json.loads(header)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("type") != "session_meta":
        return None
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id")
    return session_id if isinstance(session_id, str) else None


def _latest_artifact(
    connection: sqlite3.Connection, source: Source, path_hash: str
) -> _Artifact | None:
    row = connection.execute(
        "SELECT id, generation, device, inode, size_bytes, byte_offset, prefix_fingerprint, "
        "header_session_id, last_complete_ordinal, is_missing FROM artifacts "
        "WHERE source = ? AND path_hash = ? ORDER BY generation DESC LIMIT 1",
        (source, path_hash),
    ).fetchone()
    if row is None:
        return None
    return _Artifact(
        id=int(row[0]),
        generation=int(row[1]),
        device=cast(int | None, row[2]),
        inode=cast(int | None, row[3]),
        size_bytes=int(row[4] or 0),
        byte_offset=int(row[5] or 0),
        prefix_fingerprint=cast(str | None, row[6]),
        header_session_id=cast(str | None, row[7]),
        last_complete_ordinal=int(row[8] if row[8] is not None else -1),
        is_missing=bool(row[9]),
    )


def _needs_new_generation(
    artifact: _Artifact,
    stat: os.stat_result,
    prefix_fingerprint: str,
    header_session_id: str | None,
) -> bool:
    return (
        artifact.is_missing
        or stat.st_size < artifact.size_bytes
        or _sqlite_file_id(stat.st_dev) != artifact.device
        or _sqlite_file_id(stat.st_ino) != artifact.inode
        or prefix_fingerprint != artifact.prefix_fingerprint
        or header_session_id != artifact.header_session_id
    )


def _insert_artifact(
    connection: sqlite3.Connection,
    source: Source,
    path_hash: str,
    generation: int,
    stat: os.stat_result,
    prefix_fingerprint: str,
    header_session_id: str | None,
) -> _Artifact:
    device = _sqlite_file_id(stat.st_dev)
    inode = _sqlite_file_id(stat.st_ino)
    cursor = connection.execute(
        "INSERT INTO artifacts "
        "(source, path_hash, device, inode, generation, size_bytes, mtime_ns, byte_offset, "
        "prefix_fingerprint, header_session_id, last_complete_ordinal, last_seen_at_ns) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, -1, ?)",
        (
            source,
            path_hash,
            device,
            inode,
            generation,
            stat.st_size,
            stat.st_mtime_ns,
            prefix_fingerprint,
            header_session_id,
            time.time_ns(),
        ),
    )
    return _Artifact(
        id=int(cursor.lastrowid),
        generation=generation,
        device=device,
        inode=inode,
        size_bytes=stat.st_size,
        byte_offset=0,
        prefix_fingerprint=prefix_fingerprint,
        header_session_id=header_session_id,
        last_complete_ordinal=-1,
        is_missing=False,
    )


def _write_stats(total: WriteStats, added: WriteStats) -> WriteStats:
    return WriteStats(
        observations_inserted=total.observations_inserted + added.observations_inserted,
        events_inserted=total.events_inserted + added.events_inserted,
        events_updated=total.events_updated + added.events_updated,
        links_inserted=total.links_inserted + added.links_inserted,
        samples_inserted=total.samples_inserted + added.samples_inserted,
        issues_recorded=total.issues_recorded + added.issues_recorded,
    )


def _invalid_line_projection(source: Source, line: _Artifact, ordinal: int, error: Exception) -> Projection:
    return Projection(
        issues=(
            IssueDraft(
                source=source,
                category="invalid_jsonl",
                severity="error",
                identifier=f"artifact:{line.id}:generation:{line.generation}:ordinal:{ordinal}",
                field_path=str(ordinal),
                observed_type=type(error).__name__,
            ),
        )
    )


def scan_jsonl(
    repository: Repository,
    source: Source,
    path: Path,
    project: Callable[[SourceLine, object], Projection],
    state: object,
) -> SyncStats:
    stat = path.stat()
    header_session_id = _header_session_id(path)
    writes = WriteStats()
    lines_read = 0

    with repository.transaction() as connection:
        path_hash = _path_hash(connection, path)
        artifact = _latest_artifact(connection, source, path_hash)
        if artifact is None:
            prefix_fingerprint = _prefix_fingerprint(path, stat.st_size)
            artifact = _insert_artifact(
                connection,
                source,
                path_hash,
                0,
                stat,
                prefix_fingerprint,
                header_session_id,
            )
        else:
            prefix_fingerprint = _prefix_fingerprint(path, artifact.size_bytes)
        if artifact is not None and _needs_new_generation(artifact, stat, prefix_fingerprint, header_session_id):
            prefix_fingerprint = _prefix_fingerprint(path, stat.st_size)
            artifact = _insert_artifact(
                connection,
                source,
                path_hash,
                artifact.generation + 1,
                stat,
                prefix_fingerprint,
                header_session_id,
            )

        stored_prefix_fingerprint = _prefix_fingerprint(path, stat.st_size)

        byte_offset = artifact.byte_offset
        ordinal = artifact.last_complete_ordinal + 1
        with path.open("rb") as artifact_file:
            artifact_file.seek(byte_offset)
            while raw_line := artifact_file.readline():
                byte_end = artifact_file.tell()
                if not raw_line.endswith(b"\n"):
                    break
                try:
                    value = json.loads(raw_line)
                    if not isinstance(value, dict):
                        raise ValueError("JSONL record must be an object")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    writes = _write_stats(
                        writes,
                        repository.apply_projection_in_transaction(
                            connection, _invalid_line_projection(source, artifact, ordinal, error)
                        ),
                    )
                    break
                source_line = SourceLine(
                    artifact_id=artifact.id,
                    generation=artifact.generation,
                    ordinal=ordinal,
                    byte_start=byte_offset,
                    byte_end=byte_end,
                    value=cast(Mapping[str, object], value),
                )
                writes = _write_stats(
                    writes, repository.apply_projection_in_transaction(connection, project(source_line, state))
                )
                lines_read += 1
                byte_offset = byte_end
                ordinal += 1

        connection.execute(
            "UPDATE artifacts SET device = ?, inode = ?, size_bytes = ?, mtime_ns = ?, byte_offset = ?, "
            "prefix_fingerprint = ?, header_session_id = ?, last_complete_ordinal = ?, last_seen_at_ns = ? "
            "WHERE id = ?",
            (
                _sqlite_file_id(stat.st_dev),
                _sqlite_file_id(stat.st_ino),
                stat.st_size,
                stat.st_mtime_ns,
                byte_offset,
                stored_prefix_fingerprint,
                header_session_id,
                ordinal - 1,
                time.time_ns(),
                artifact.id,
            ),
        )

    return SyncStats(
        artifacts_seen=1,
        lines_read=lines_read,
        observations_inserted=writes.observations_inserted,
        events_inserted=writes.events_inserted,
        events_updated=writes.events_updated,
        issues_recorded=writes.issues_recorded,
    )
