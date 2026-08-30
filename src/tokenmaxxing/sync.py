import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from tokenmaxxing.ingest.claude import sync_claude
from tokenmaxxing.ingest.codex import CodexRoots, sync_codex
from tokenmaxxing.ingest.opencode import OpenCodeRoots, sync_opencode
from tokenmaxxing.ingest.pi import sync_pi
from tokenmaxxing.models import Source, SyncStats
from tokenmaxxing.repository import Repository


@dataclass(frozen=True, slots=True)
class SourceRoots:
    codex: Path
    claude: Path
    pi: Path
    opencode_db: Path

    @classmethod
    def defaults(
        cls,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
        platform: str | None = None,
    ) -> "SourceRoots":
        home = Path.home() if home is None else home
        environ = os.environ if environ is None else environ
        platform = sys.platform if platform is None else platform
        opencode_data = (
            Path(environ["XDG_DATA_HOME"]) / "opencode"
            if platform != "win32" and environ.get("XDG_DATA_HOME")
            else home / ".local" / "share" / "opencode"
        )
        return cls(
            codex=home / ".codex",
            claude=home / ".claude" / "projects",
            pi=home / ".pi" / "agent" / "sessions",
            opencode_db=opencode_data / "opencode.db",
        )


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    source: Source
    status: Literal["ok", "skipped", "error"]
    stats: SyncStats = field(default_factory=SyncStats)
    error_category: str | None = None


def sync_sources(
    repository: Repository,
    roots: SourceRoots,
    sources: tuple[Source, ...],
    *,
    raise_errors: bool = False,
) -> tuple[SourceSyncResult, ...]:
    results: list[SourceSyncResult] = []
    for source in sources:
        try:
            if source == "codex":
                if not roots.codex.is_dir():
                    results.append(SourceSyncResult(source=source, status="skipped"))
                    continue
                stats = sync_codex(repository, CodexRoots.from_path(roots.codex))
            elif source == "claude":
                if not roots.claude.is_dir():
                    results.append(SourceSyncResult(source=source, status="skipped"))
                    continue
                stats = sync_claude(repository, roots.claude)
            elif source == "pi":
                if not roots.pi.is_dir():
                    results.append(SourceSyncResult(source=source, status="skipped"))
                    continue
                stats = sync_pi(repository, roots.pi)
            else:
                if not roots.opencode_db.is_file():
                    results.append(SourceSyncResult(source=source, status="skipped"))
                    continue
                opencode_roots = replace(
                    OpenCodeRoots.from_data_dir(roots.opencode_db.parent),
                    database=roots.opencode_db,
                )
                stats = sync_opencode(repository, opencode_roots)
            results.append(SourceSyncResult(source=source, status="ok", stats=stats))
        except Exception as error:
            if raise_errors:
                raise
            results.append(
                SourceSyncResult(
                    source=source,
                    status="error",
                    error_category=type(error).__name__,
                )
            )
    return tuple(results)
