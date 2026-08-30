import argparse
import json
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, tzinfo
from importlib.metadata import version
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rich.console import Console
from rich.table import Table
from rich.text import Text
import tzlocal

from tokenmaxxing.config import default_paths
from tokenmaxxing.db import Database
from tokenmaxxing.models import Source, UsageStat
from tokenmaxxing.pricing import ApiValueEstimate, estimate_api_value_rows
from tokenmaxxing.presentation import api_value_text, compact_tokens, compact_usd, usage_quip
from tokenmaxxing.profile import cli as profile_cli
from tokenmaxxing.reporting import ReportWindow, export_payload, usage_stats_rows
from tokenmaxxing.repository import Repository
from tokenmaxxing.sync import SourceRoots, SourceSyncResult, sync_sources

_ALL_SOURCES: tuple[Source, ...] = ("codex", "claude", "pi", "opencode")
_SYNC_MESSAGES = (
    "Reading your local token trail",
    "Untangling the agent family tree",
    "Making sure nothing counted twice",
    "Persuading the numbers to sit still",
)
_SYNC_MESSAGE_INTERVAL = 3.0


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise argparse.ArgumentTypeError("timezone must be an IANA name") from error


def _export_path(value: str) -> Path:
    path = Path(value)
    if path.is_dir():
        return path / "tokenmaxxing-export.json"
    if path.suffix.lower() != ".json":
        raise argparse.ArgumentTypeError(
            "export writes JSON; choose a .json file or an existing directory"
        )
    return path


def _timezone_from_localtime(path: Path) -> ZoneInfo | None:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    _, marker, key = resolved.as_posix().partition("/zoneinfo/")
    if marker:
        try:
            return ZoneInfo(key)
        except (ValueError, ZoneInfoNotFoundError):
            return None
    try:
        with resolved.open("rb") as localtime:
            return ZoneInfo.from_file(localtime, key="localtime")
    except (OSError, ValueError, ZoneInfoNotFoundError):
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
    try:
        return tzlocal.get_localzone()
    except (OSError, ValueError, ZoneInfoNotFoundError):
        pass
    local = datetime.now().astimezone()
    offset = local.utcoffset() or timedelta()
    return timezone(offset, _offset_name(offset))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tokenmaxxing")
    parser.add_argument(
        "--version", action="version", version=version("tokenmaxxing")
    )
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
        "--group-by", choices=("model", "harness", "day"), default="model"
    )
    stats.add_argument("--period", choices=("7d", "28d", "all"), default="28d")
    timezone_help = "IANA timezone; default is local or an explicit UTC offset"
    stats.add_argument(
        "--timezone", type=_timezone, default=_local_timezone(), help=timezone_help
    )
    stats.add_argument("--json", action="store_true")

    export = commands.add_parser(
        "export",
        help="write an aggregate JSON snapshot",
        description="Write a privacy-safe aggregate JSON snapshot.",
    )
    export.add_argument(
        "path",
        nargs="?",
        type=_export_path,
        default="tokenmaxxing-export.json",
        help=(
            "JSON file or existing directory "
            "(default: ./tokenmaxxing-export.json)"
        ),
    )
    export.add_argument(
        "--timezone", type=_timezone, default=_local_timezone(), help=timezone_help
    )
    profile_cli.add_profile_parser(commands)
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


def _stats_payload(
    group_by: str,
    period: str,
    stats: tuple[UsageStat, ...],
    api_equivalent: ApiValueEstimate,
) -> dict[str, object]:
    return {
        "api_equivalent": asdict(api_equivalent),
        "group_by": group_by,
        "period": period,
        "stats": [asdict(stat) for stat in stats],
    }


def _now(timezone: tzinfo) -> datetime:
    return datetime.now(timezone)


def _compact_tokens(tokens: int) -> str:
    return compact_tokens(tokens)


def _compact_usd(cost_nanos: int) -> str:
    return compact_usd(cost_nanos)


def _api_value_copy(api_equivalent: ApiValueEstimate) -> str | None:
    value = api_value_text(api_equivalent)
    if value is None:
        return None
    prefix = "" if value.startswith("<") or api_equivalent.cost_nanos == 0 else "≈"
    return f"API equivalent: {prefix}{value}"

def _usage_hint(tokens: int) -> str:
    return usage_quip(tokens)

def _stats_title(group_by: str) -> str:
    return {"model": "Models", "harness": "Harnesses", "day": "Days"}[group_by]


def _period_title(period: str) -> str:
    return {"7d": "Last 7 days", "28d": "Last 28 days", "all": "All time"}[period]


def _period_copy(period: str) -> str:
    return {"7d": "the last 7 days", "28d": "the last 28 days", "all": "all time"}[period]


@contextmanager
def _rotating_status(
    console: Console,
    messages: tuple[str, ...] = _SYNC_MESSAGES,
    *,
    interval: float = _SYNC_MESSAGE_INTERVAL,
) -> Iterator[None]:
    status = console.status(messages[0], spinner="dots")
    stop = Event()

    def rotate() -> None:
        index = 1
        while not stop.wait(interval):
            status.update(messages[index % len(messages)])
            index += 1

    with status:
        worker = Thread(target=rotate, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join()


def _escape_controls(value: str) -> str:
    return "".join(
        f"\\x{ord(character):02x}"
        if ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        else character
        for character in value
    )


def _display_group(group: str, group_by: str) -> str:
    if group_by == "model" and group == "(unknown)":
        group = "unknown models"
    return _escape_controls(group)


def _visible_stats(stats: tuple[UsageStat, ...]) -> tuple[UsageStat, ...]:
    return tuple(
        sorted(
            (stat for stat in stats if stat.total_tokens > 0),
            key=lambda stat: (-stat.total_tokens, stat.group),
        )
    )


def _print_stats(
    stats: tuple[UsageStat, ...],
    group_by: str,
    period: str,
    api_equivalent: ApiValueEstimate,
) -> None:
    total_tokens = sum(stat.total_tokens for stat in stats)
    header = f"{_period_title(period)}: {_compact_tokens(total_tokens)} tokens"
    hint = _usage_hint(total_tokens)
    visible_stats = _visible_stats(stats)
    if not sys.stdout.isatty():
        print(header)
        api_value_copy = _api_value_copy(api_equivalent)
        if api_value_copy is not None:
            print(api_value_copy)
        print(hint)
        if not visible_stats:
            print(
                f"No tokens in {_period_copy(period)} yet. Run `tokenmaxxing sync` to bring them in."
            )
            return
        print(_stats_title(group_by))
        for stat in visible_stats:
            print(f"{_display_group(stat.group, group_by)}  {_compact_tokens(stat.total_tokens)}")
        return
    console = Console(markup=False, highlight=False)
    console.print(header)
    api_value_copy = _api_value_copy(api_equivalent)
    if api_value_copy is not None:
        console.print(api_value_copy)
    console.print(hint)
    if not visible_stats:
        console.print(
            f"No tokens in {_period_copy(period)} yet. Run `tokenmaxxing sync` to bring them in."
        )
        return
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column(justify="right")
    for stat in visible_stats:
        table.add_row(
            Text(_display_group(stat.group, group_by)), _compact_tokens(stat.total_tokens)
        )
    console.print(_stats_title(group_by))
    console.print(table)


def _sync(arguments: argparse.Namespace) -> int:
    selected = _ALL_SOURCES if arguments.source == "all" else (arguments.source,)
    console = Console()
    status = (
        _rotating_status(console)
        if console.is_terminal and not arguments.json
        else nullcontext()
    )
    database = Database.open(arguments.db)
    try:
        with status:
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
        return 1 if any(result.status == "error" for result in results) else 0
    for result in results:
        if result.status == "error":
            Console(stderr=True).print(
                f"{result.source}: error ({result.error_category})"
            )
        elif result.status == "ok":
            console.print(f"{result.source}: synced")
        else:
            console.print(f"{result.source}: skipped")
    console.print("Try `tokenmaxxing stats` to see your model leaderboard.")
    return 1 if any(result.status == "error" for result in results) else 0


def _stats(arguments: argparse.Namespace) -> int:
    reporting_group = "source" if arguments.group_by == "harness" else arguments.group_by
    window = ReportWindow.from_period(
        arguments.period, arguments.timezone, _now(arguments.timezone)
    )
    database = Database.open(arguments.db)
    try:
        repository = Repository(database)
        rows = [row for row in repository.reporting_rows() if window.includes(row)]
        stats = usage_stats_rows(rows, reporting_group, arguments.timezone)
        api_equivalent = estimate_api_value_rows(rows)
    finally:
        database.close()
    if arguments.json:
        print(
            json.dumps(
                _stats_payload(
                    arguments.group_by, arguments.period, stats, api_equivalent
                ),
                sort_keys=True,
            )
        )
    else:
        _print_stats(stats, arguments.group_by, arguments.period, api_equivalent)
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
    print(f"Exported aggregate JSON → {_escape_controls(str(arguments.path))}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "sync":
            return _sync(arguments)
        if arguments.command == "stats":
            return _stats(arguments)
        if arguments.command == "export":
            return _export(arguments)
        if arguments.command == "profile":
            return profile_cli.run_profile(arguments)
        raise ValueError(f"unknown command: {arguments.command}")
    except Exception as error:
        if arguments.debug:
            raise
        print(f"error: {error}", file=sys.stderr)
        return 1
