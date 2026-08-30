import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tokenmaxxing.config import default_paths
from tokenmaxxing.db import Database
from tokenmaxxing.models import Source, UsageStat
from tokenmaxxing.reporting import export_payload, usage_stats
from tokenmaxxing.repository import Repository
from tokenmaxxing.sync import SourceRoots, SourceSyncResult, sync_sources

_ALL_SOURCES: tuple[Source, ...] = ("codex", "claude", "pi", "opencode")


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise argparse.ArgumentTypeError("timezone must be an IANA name") from error


def _timezone_from_localtime(path: Path) -> ZoneInfo | None:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    _, marker, key = resolved.as_posix().partition("/zoneinfo/")
    if not marker:
        return None
    try:
        return ZoneInfo(key)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _offset_name(offset: timedelta) -> str:
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    hours, remainder = divmod(abs(total_seconds), 3_600)
    return f"UTC{sign}{hours:02d}:{remainder // 60:02d}"


def _local_timezone(localtime_path: Path = Path("/etc/localtime")) -> tzinfo:
    configured = os.environ.get("TZ", "").removeprefix(":")
    if configured:
        try:
            return ZoneInfo(configured)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    if system_timezone := _timezone_from_localtime(localtime_path):
        return system_timezone
    local = datetime.now().astimezone()
    offset = local.utcoffset() or timedelta()
    return timezone(offset, _offset_name(offset))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tokenmaxxing")
    parser.add_argument("--db", type=Path, default=default_paths().db_path)
    parser.add_argument("--debug", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync")
    sync.add_argument("--source", choices=(*_ALL_SOURCES, "all"), default="all")
    sync.add_argument("--codex-root", type=Path)
    sync.add_argument("--claude-root", type=Path)
    sync.add_argument("--pi-root", type=Path)
    sync.add_argument("--opencode-db", type=Path)
    sync.add_argument("--json", action="store_true")

    stats = commands.add_parser("stats")
    stats.add_argument(
        "--group-by", choices=("source", "model", "day"), default="source"
    )
    timezone_help = "IANA timezone; default is local or an explicit UTC offset"
    stats.add_argument(
        "--timezone", type=_timezone, default=_local_timezone(), help=timezone_help
    )
    stats.add_argument("--json", action="store_true")

    export = commands.add_parser("export")
    export.add_argument("path", type=Path)
    export.add_argument(
        "--timezone", type=_timezone, default=_local_timezone(), help=timezone_help
    )
    return parser


def _roots(arguments: argparse.Namespace) -> SourceRoots:
    defaults = SourceRoots.defaults()
    return SourceRoots(
        codex=arguments.codex_root or defaults.codex,
        claude=arguments.claude_root or defaults.claude,
        pi=arguments.pi_root or defaults.pi,
        opencode_db=arguments.opencode_db or defaults.opencode_db,
    )


def _sync_payload(results: tuple[SourceSyncResult, ...]) -> dict[str, object]:
    return {"results": [asdict(result) for result in results]}


def _stats_payload(group_by: str, stats: tuple[UsageStat, ...]) -> dict[str, object]:
    return {"group_by": group_by, "stats": [asdict(stat) for stat in stats]}


def _print_stats(stats: tuple[UsageStat, ...]) -> None:
    headers = (
        "Group",
        "Events",
        "Input",
        "Output",
        "Cache R",
        "Cache W",
        "Reason",
        "Total",
        "Cost",
    )
    rows = [
        (
            stat.group,
            str(stat.event_count),
            str(stat.input_tokens),
            str(stat.output_tokens),
            str(stat.cache_read_tokens),
            str(stat.cache_write_tokens),
            str(stat.reasoning_tokens),
            str(stat.total_tokens),
            _cost(stat),
        )
        for stat in stats
    ]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]
    print(" ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        print(" ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _cost(stat: UsageStat) -> str:
    if stat.cost_nanos is None or stat.cost_covered_events != stat.event_count:
        return "unavailable"
    return f"${stat.cost_nanos / 1_000_000_000:.6f}"


def _sync(arguments: argparse.Namespace) -> int:
    selected = _ALL_SOURCES if arguments.source == "all" else (arguments.source,)
    database = Database.open(arguments.db)
    try:
        results = sync_sources(
            Repository(database),
            _roots(arguments),
            selected,
            raise_errors=arguments.debug,
        )
    finally:
        database.close()
    if arguments.json:
        print(json.dumps(_sync_payload(results), sort_keys=True))
    for result in results:
        if result.status == "error":
            print(f"sync: {result.source}: {result.error_category}", file=sys.stderr)
        elif not arguments.json:
            print(f"{result.source}: {result.status}")
    return 1 if any(result.status == "error" for result in results) else 0


def _stats(arguments: argparse.Namespace) -> int:
    database = Database.open(arguments.db)
    try:
        stats = usage_stats(
            Repository(database), arguments.group_by, arguments.timezone
        )
    finally:
        database.close()
    if arguments.json:
        print(json.dumps(_stats_payload(arguments.group_by, stats), sort_keys=True))
    else:
        _print_stats(stats)
    return 0


def _export(arguments: argparse.Namespace) -> int:
    database = Database.open(arguments.db)
    try:
        payload = export_payload(
            Repository(database), arguments.timezone, datetime.now(arguments.timezone)
        )
    finally:
        database.close()
    arguments.path.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "sync":
            return _sync(arguments)
        if arguments.command == "stats":
            return _stats(arguments)
        return _export(arguments)
    except Exception as error:
        if arguments.debug:
            raise
        print(f"error: {type(error).__name__}", file=sys.stderr)
        return 1
