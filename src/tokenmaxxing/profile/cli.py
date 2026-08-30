from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzlocal
from rich.console import Console
from rich.prompt import Confirm, Prompt

from tokenmaxxing.config import default_paths
from tokenmaxxing.db import Database
from tokenmaxxing.profile.build import BuildResult, build_profile, validate_site
from tokenmaxxing.profile.config import (
    DeployConfig,
    ProfileConfig,
    ProfileInfo,
    ProfileLink,
    ScheduleConfig,
    SiteConfig,
    discover_config,
    load_config,
    load_starter_config,
    normalize_canonical_url,
    remember_config,
    validate_config,
    validate_public_link_url,
    write_initial_config,
)
from tokenmaxxing.profile.deploy import (
    DeployError,
    DeployPlan,
    DeployResult,
    make_deploy_plan,
    run_deploy,
)
from tokenmaxxing.profile.project import (
    ProfilePaths,
    initialize_project,
    open_editor,
    profile_paths,
)
from tokenmaxxing.repository import Repository
from tokenmaxxing.sync import SourceRoots, sync_sources


_ALL_SOURCES = ("codex", "claude", "pi", "opencode")


@dataclass(frozen=True, slots=True)
class _OnboardingResult:
    config: ProfileConfig
    avatar_source: Path | None = None


def add_profile_parser(commands: argparse._SubParsersAction) -> None:
    profile = commands.add_parser(
        "profile",
        help="build and publish your token profile",
        description="Create, preview, publish, and schedule a static token profile.",
        epilog="Example: tokenmaxxing profile preview",
    )
    profile.add_argument(
        "--config",
        type=Path,
        help="configuration file; defaults to the nearest or last initialized profile",
    )
    profile_commands = profile.add_subparsers(
        dest="profile_command", required=True
    )

    init = profile_commands.add_parser(
        "init",
        help="Create a profile project",
        description="Create a profile project without overwriting editable files.",
        epilog="Example: tokenmaxxing profile init ~/my-token-profile",
    )
    init.add_argument(
        "directory",
        nargs="?",
        type=Path,
        help="project directory (prompts when omitted; default: ./tokenmaxxing-profile)",
    )
    init.add_argument(
        "--no-setup", action="store_true", help="write starter files without prompting"
    )
    init.add_argument(
        "--editable-template",
        action="store_true",
        help="copy packaged HTML templates into the project",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="never overwrites; add missing files to a non-empty directory",
    )

    edit = profile_commands.add_parser(
        "edit",
        help="Open and validate profile configuration",
        description="Open config.yaml in the configured editor, then validate it.",
        epilog="Example: tokenmaxxing profile edit --publish",
    )
    edit.add_argument(
        "--publish", action="store_true", help="Publish after successful validation"
    )

    preview = profile_commands.add_parser(
        "preview",
        help="Build and serve a private local preview",
        description="Build a temporary noindex site and serve it over local HTTP.",
        epilog="Example: tokenmaxxing profile preview --no-open",
    )
    preview.add_argument(
        "--host", default="127.0.0.1", help="server host (default: 127.0.0.1)"
    )
    preview.add_argument(
        "--port", type=int, help="server port (default: an available local port)"
    )
    preview.add_argument(
        "--no-open", action="store_true", help="do not open the preview in a browser"
    )

    build = profile_commands.add_parser(
        "build",
        help="Build and validate the static site",
        description="Build and validate the portable static profile without deploying.",
        epilog="Example: tokenmaxxing profile build --json",
    )
    build.add_argument(
        "--output",
        type=Path,
        help="site directory (default: dist)",
    )
    build.add_argument(
        "--json", action="store_true", help="emit one machine-readable JSON result"
    )

    publish = profile_commands.add_parser(
        "publish",
        help="Build and run the deploy command",
        description="Build, validate, and publish using the configured deploy command.",
        epilog="Example: tokenmaxxing profile publish --sync",
    )
    publish.add_argument(
        "--sync", action="store_true", help="Sync local histories before building"
    )
    publish.add_argument(
        "--non-interactive",
        action="store_true",
        help="publish without prompting (used by scheduled updates)",
    )
    publish.add_argument(
        "--json", action="store_true", help="emit one machine-readable JSON result"
    )

    status = profile_commands.add_parser(
        "status",
        help="Show profile, deploy, and schedule status",
        description="Show project paths and schedule state.",
        epilog="Example: tokenmaxxing profile status --json",
    )
    status.add_argument(
        "--json", action="store_true", help="emit one machine-readable JSON result"
    )

    schedule = profile_commands.add_parser(
        "schedule",
        help="Manage daily publishing",
        description="Enable, disable, or inspect operating-system daily publishing.",
        epilog="Example: tokenmaxxing profile schedule enable",
    )
    schedule.add_argument(
        "action",
        nargs="?",
        choices=("enable", "disable", "status"),
        help="schedule action (default: status)",
    )


def _resolve_config(arguments: argparse.Namespace) -> Path:
    configured = getattr(arguments, "config", None)
    return (
        configured
        if configured is not None
        else discover_config(Path.cwd(), default_paths().profile_path)
    )


def _build_payload(result: BuildResult) -> dict[str, object]:
    return {
        "file_count": result.file_count,
        "generated_at": result.generated_at.isoformat(),
        "site_dir": str(result.site_dir),
        "total_bytes": result.total_bytes,
    }


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _prompt_text(label: str, default: str) -> str:
    return Prompt.ask(label, default=default, show_default=bool(default))


def _prompt_link(label: str) -> ProfileLink | None:
    while True:
        url = _prompt_text(f"{label} URL (optional)", "")
        if not url:
            return None
        try:
            url = validate_public_link_url(url, f"{label} URL")
            if urlsplit(url).scheme != "https":
                raise ValueError(f"{label} URL must use https")
        except ValueError as error:
            print(f"{error}. Try again.", file=sys.stderr)
            continue
        parsed = urlsplit(url)
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        if label == "Website":
            value = parsed.netloc.removeprefix("www.")
        elif path_parts:
            value = path_parts[-1]
        else:
            value = parsed.netloc
        return ProfileLink(label=label, value=value, url=url)


def _prompt_links() -> tuple[ProfileLink, ...]:
    links: list[ProfileLink] = []
    for label in ("LinkedIn", "GitHub", "Website"):
        if link := _prompt_link(label):
            links.append(link)
    return tuple(links)


def _system_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(tzlocal.get_localzone_name())
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def _prompt_schedule_time(default: str) -> time:
    while True:
        value = _prompt_text("Daily publish time", default)
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and not parsed.second and not parsed.microsecond:
            return parsed
        print("Daily publish time must use HH:MM. Try again.", file=sys.stderr)


def _prompt_deploy_command() -> tuple[str, ...]:
    while True:
        value = _prompt_text(
            "Deployment command (optional; {site_dir} is available when needed)",
            "",
        )
        if not value:
            return ()
        try:
            command = tuple(shlex.split(value))
        except ValueError:
            print("Deployment command has invalid quoting. Try again.", file=sys.stderr)
            continue
        if command:
            return command


def _prompt_canonical_url(default: str) -> str:
    while True:
        value = _prompt_text("Public profile URL", default)
        try:
            return normalize_canonical_url(value, "Public profile URL")
        except ValueError as error:
            print(f"{error}. Try again.", file=sys.stderr)


def _prompt_avatar() -> Path | None:
    while True:
        value = _prompt_text("Avatar path (optional)", "")
        if not value:
            return None
        avatar = Path(value).expanduser().resolve()
        if avatar.is_file():
            return avatar
        print("Avatar must be a readable file. Try again.", file=sys.stderr)


def _avatar_target(project: Path, source: Path | None) -> Path | None:
    if source is None:
        return None
    suffix = source.suffix.lower()
    if not suffix or not suffix[1:].isalnum() or len(suffix) > 9:
        suffix = ".image"
    target = project / f"avatar{suffix}"
    number = 2
    while target.exists():
        target = project / f"avatar-{number}{suffix}"
        number += 1
    return target.resolve()


def _interactive_config(path: Path, starter: ProfileConfig) -> _OnboardingResult:
    name = _prompt_text("Name", starter.profile.name)
    bio = _prompt_text("Bio", starter.profile.bio)
    avatar_source = _prompt_avatar()
    avatar = _avatar_target(path.parent, avatar_source)
    links = _prompt_links()
    canonical_url = _prompt_canonical_url(starter.site.canonical_url)
    timezone = _system_timezone()
    configured = replace(
        starter,
        profile=ProfileInfo(
            name=name,
            bio=bio,
            avatar=avatar,
            links=links,
        ),
        site=SiteConfig(
            title=f"{name} | Token Trail",
            description=starter.site.description,
            canonical_url=canonical_url,
            indexable=True,
            timezone=timezone,
            theme=starter.site.theme,
            accent=starter.site.accent,
        ),
        deploy=DeployConfig(command=()),
    )
    return _OnboardingResult(
        config=validate_config(path, configured),
        avatar_source=avatar_source,
    )


def _check_init_destination(directory: Path, *, force: bool) -> None:
    if not directory.exists():
        return
    if not directory.is_dir():
        raise NotADirectoryError(f"profile project path is not a directory: {directory}")
    if not force and next(directory.iterdir(), None) is not None:
        raise FileExistsError(
            "profile project directory is non-empty; use force to add missing files"
        )


@contextmanager
def _onboarding_preview(
    site_dir: Path,
    *,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    browser_open: Callable[[str], bool] = webbrowser.open,
) -> Iterator[None]:
    handler = partial(_QuietHandler, directory=str(site_dir))
    with server_factory(("127.0.0.1", 0), handler) as server:
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
        )
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Preview: {url}")
        browser_open(url)
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def _configure_deployment(config_path: Path, config: ProfileConfig) -> ProfileConfig:
    while True:
        configured = replace(
            config,
            deploy=DeployConfig(command=_prompt_deploy_command()),
        )
        try:
            return validate_config(config_path, configured)
        except ValueError as error:
            print(f"Deployment configuration is invalid: {error}. Try again.", file=sys.stderr)


def _finish_onboarding(
    arguments: argparse.Namespace,
    config_path: Path,
    config: ProfileConfig,
) -> None:
    if not Confirm.ask("Sync, build, and preview now?", default=True):
        print("Run `tokenmaxxing profile preview` when ready.")
        return
    _sync_profile_sources(arguments.db, debug=arguments.debug)
    result = build_profile(config_path, db_path=arguments.db)
    print(f"Built profile: {result.site_dir}")
    with _onboarding_preview(result.site_dir):
        config = _configure_deployment(config_path, config)
        write_initial_config(config_path, config)
        publish_now = bool(config.deploy.command) and Confirm.ask(
            "Publish this profile?", default=True
        )
    if not config.deploy.command:
        return
    if not publish_now:
        print("Run `tokenmaxxing profile publish` when ready.")
        return
    publish_arguments = argparse.Namespace(
        **{
            **vars(arguments),
            "sync": False,
            "non_interactive": True,
            "json": False,
        }
    )
    _publish(publish_arguments, config_path)
    if Confirm.ask("Enable daily publishing?", default=True):
        schedule_time = _prompt_schedule_time(
            config.schedule.time.strftime("%H:%M")
        )
        config = validate_config(
            config_path,
            replace(config, schedule=ScheduleConfig(time=schedule_time)),
        )
        write_initial_config(config_path, config)
        schedule_arguments = argparse.Namespace(
            **{**vars(arguments), "action": "enable"}
        )
        _schedule(schedule_arguments, config_path)
    else:
        print("Run `tokenmaxxing profile schedule enable` when ready.")


def _initialize(arguments: argparse.Namespace) -> int:
    if getattr(arguments, "config", None) is not None:
        raise ValueError("--config cannot be used with profile init")
    directory = arguments.directory
    if directory is None:
        directory = Path(
            Prompt.ask("Profile directory", default="./tokenmaxxing-profile")
        ).expanduser()
    config_path = directory / "config.yaml"
    config_existed = config_path.exists()
    _check_init_destination(directory, force=arguments.force)
    onboarding: _OnboardingResult | None = None
    if not arguments.no_setup and not config_existed:
        onboarding = _interactive_config(
            config_path, load_starter_config(config_path)
        )
    config_path = initialize_project(
        directory,
        editable_template=arguments.editable_template,
        force=arguments.force,
    )
    if onboarding is not None:
        avatar = onboarding.config.profile.avatar
        if onboarding.avatar_source is not None and avatar is not None:
            with onboarding.avatar_source.open("rb") as source, avatar.open("xb") as target:
                shutil.copyfileobj(source, target)
        write_initial_config(config_path, onboarding.config)
    load_config(config_path)
    remember_config(config_path, default_paths().profile_path)
    print(f"Profile created: {directory.resolve()}")
    print(f"Config: {config_path.resolve()}")
    if onboarding is not None:
        _finish_onboarding(arguments, config_path, onboarding.config)
    return 0


def _sync_profile_sources(db_path: Path, *, debug: bool) -> None:
    print("Syncing local histories…", file=sys.stderr)
    database = Database.open(db_path)
    try:
        results = sync_sources(
            Repository(database),
            SourceRoots.defaults(),
            _ALL_SOURCES,
            raise_errors=debug,
        )
    finally:
        database.close()
    if any(result.status == "error" for result in results):
        raise RuntimeError("one or more local sources could not be synced")


def _confirm_deploy(plan: DeployPlan) -> bool:
    console = Console(stderr=True, markup=False, highlight=False)
    console.print("Command arguments:")
    for index, argument in enumerate(plan.argv):
        console.print(
            f"  [{index}] {json.dumps(argument, ensure_ascii=True)}", soft_wrap=True
        )
    console.print(
        f"Working directory: {json.dumps(str(plan.cwd), ensure_ascii=True)}",
        soft_wrap=True,
    )
    console.print(
        f"Public URL: {json.dumps(plan.canonical_url, ensure_ascii=True)}",
        soft_wrap=True,
    )
    return Confirm.ask("Publish this profile?", default=False, console=console)


def _write_deploy_details(result: DeployResult) -> None:
    if result.stdout:
        print(
            result.stdout,
            end="" if result.stdout.endswith("\n") else "\n",
            file=sys.stderr,
        )
    if result.stderr:
        print(
            result.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )


def _publish(arguments: argparse.Namespace, config_path: Path | None = None) -> int:
    config_path = config_path or _resolve_config(arguments)
    if arguments.sync:
        _sync_profile_sources(arguments.db, debug=arguments.debug)
    result = build_profile(config_path, db_path=arguments.db)
    config = load_config(config_path)
    paths = profile_paths(config_path)
    plan = make_deploy_plan(config, paths)
    try:
        deployed = run_deploy(
            plan,
            non_interactive=arguments.non_interactive,
            confirm=_confirm_deploy,
        )
    except DeployError as error:
        if error.result is not None:
            _write_deploy_details(error.result)
        raise
    _write_deploy_details(deployed)
    if arguments.json:
        _print_json(
            {
                "build": _build_payload(result),
                "deploy": {"returncode": deployed.returncode},
            }
        )
    else:
        print(f"Published → {config.site.canonical_url}")
    return 0


def _edit(arguments: argparse.Namespace, config_path: Path) -> int:
    returncode = open_editor(config_path, os.environ)
    if returncode:
        raise RuntimeError(f"editor exited with status {returncode}")
    load_config(config_path)
    if arguments.publish:
        publish_arguments = argparse.Namespace(
            **{
                **vars(arguments),
                "sync": False,
                "non_interactive": False,
                "json": False,
            }
        )
        return _publish(publish_arguments, config_path)
    print(f"Profile configuration updated → {config_path}")
    return 0


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


def _serve_site(
    site_dir: Path,
    *,
    host: str,
    port: int | None,
    open_browser: bool,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    browser_open: Callable[[str], bool] = webbrowser.open,
) -> None:
    handler = partial(_QuietHandler, directory=str(site_dir))
    with server_factory((host, 0 if port is None else port), handler) as server:
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        url = f"http://{display_host}:{server.server_port}/"
        print(f"Preview → {url}")
        if open_browser:
            browser_open(url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


def _preview(arguments: argparse.Namespace, config_path: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="tokenmaxxing-preview-") as temporary:
        output = Path(temporary) / "site"
        result = build_profile(
            config_path,
            db_path=arguments.db,
            output=output,
            noindex=True,
        )
        validate_site(result.site_dir, noindex=True)
        _serve_site(
            result.site_dir,
            host=arguments.host,
            port=arguments.port,
            open_browser=not arguments.no_open,
        )
    return 0


def _schedule_module():
    from tokenmaxxing.profile import schedule

    return schedule


def _schedule_status(paths: ProfilePaths):
    return _schedule_module().schedule_status(
        paths, platform=sys.platform, environ=os.environ
    )


def _tokenmaxxing_executable() -> Path:
    if installed := shutil.which("tokenmaxxing"):
        return Path(installed).resolve()
    return Path(sys.argv[0]).resolve()


def _enable_schedule(
    paths: ProfilePaths, config: ProfileConfig, db_path: Path
):
    return _schedule_module().enable_schedule(
        paths,
        config,
        executable=_tokenmaxxing_executable(),
        db_path=db_path,
        platform=sys.platform,
        environ=os.environ,
    )


def _disable_schedule(paths: ProfilePaths):
    return _schedule_module().disable_schedule(
        paths, platform=sys.platform, environ=os.environ
    )


def _schedule_payload(status: object, configured_time: time) -> dict[str, object]:
    return {
        "backend": status.backend,
        "command": list(status.command),
        "enabled": status.enabled,
        "job_path": str(status.job_path) if status.job_path is not None else None,
        "next_step": status.next_step,
        "time": configured_time.strftime("%H:%M"),
    }


def _scheduler_status_or_unavailable(paths: ProfilePaths) -> object:
    try:
        return _schedule_status(paths)
    except ImportError:
        return SimpleNamespace(
            enabled=False,
            backend="unavailable",
            job_path=None,
            command=(),
            next_step="Scheduling support is unavailable in this installation.",
        )


def _status_payload(config_path: Path, db_path: Path) -> dict[str, object]:
    del db_path
    config = load_config(config_path)
    paths = profile_paths(config_path)
    schedule = _scheduler_status_or_unavailable(paths)
    return {
        "canonical_url": config.site.canonical_url,
        "config": str(paths.config),
        "schedule": _schedule_payload(schedule, config.schedule.time),
        "site": str(paths.site) if paths.site.is_dir() else None,
    }


def _status(arguments: argparse.Namespace, config_path: Path) -> int:
    payload = _status_payload(config_path, arguments.db)
    if arguments.json:
        _print_json(payload)
    else:
        print(f"Profile → {payload['canonical_url']}")
        print(f"Config: {payload['config']}")
        if payload["site"]:
            print(f"Site: {payload['site']}")
        else:
            print(f"Site: not built (expected: {profile_paths(config_path).site})")
        schedule = payload["schedule"]
        assert isinstance(schedule, dict)
        if schedule["enabled"]:
            print(
                f"Daily publishing: enabled at {schedule['time']} "
                f"({schedule['backend']})"
            )
        else:
            print(f"Daily publishing: disabled ({schedule['backend']})")
            print(f"Preferred time: {schedule['time']}")
        if schedule["next_step"]:
            print(schedule["next_step"])
        elif not schedule["enabled"]:
            print("Enable: tokenmaxxing profile schedule enable")
    return 0


def _schedule(arguments: argparse.Namespace, config_path: Path) -> int:
    paths = profile_paths(config_path)
    action = arguments.action or "status"
    if action == "enable":
        status = _enable_schedule(paths, load_config(config_path), arguments.db)
    elif action == "disable":
        status = _disable_schedule(paths)
    else:
        status = _schedule_status(paths)
    print(
        f"Schedule → {'enabled' if status.enabled else 'disabled'} ({status.backend})"
    )
    if status.next_step:
        print(status.next_step)
    return 0


def run_profile(arguments: argparse.Namespace) -> int:
    command = arguments.profile_command
    if command == "init":
        return _initialize(arguments)
    config_path = _resolve_config(arguments)
    if command == "edit":
        return _edit(arguments, config_path)
    if command == "preview":
        return _preview(arguments, config_path)
    if command == "build":
        result = build_profile(
            config_path,
            db_path=arguments.db,
            output=arguments.output,
        )
        if arguments.json:
            _print_json(_build_payload(result))
        else:
            print(f"Built profile → {result.site_dir}")
        return 0
    if command == "publish":
        return _publish(arguments, config_path)
    if command == "status":
        return _status(arguments, config_path)
    if command == "schedule":
        return _schedule(arguments, config_path)
    raise ValueError(f"unknown profile command: {command}")
