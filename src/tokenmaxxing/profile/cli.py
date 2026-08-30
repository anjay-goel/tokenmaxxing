from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rich.console import Console
from rich.prompt import Confirm, Prompt

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
    validate_config,
    validate_public_link_url,
    write_initial_config,
)
from tokenmaxxing.profile.deploy import (
    DeployError,
    DeployPlan,
    DeployResult,
    is_approved,
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
    cloudflare: bool


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
        help="configuration file; defaults to tokenmaxxing.yaml in this directory or a parent",
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
        default=Path("tokenmaxxing-profile"),
        help="project directory (default: ./tokenmaxxing-profile)",
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
        description="Open tokenmaxxing.yaml in the configured editor, then validate it.",
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
        help="site directory (default: .tokenmaxxing/site)",
    )
    build.add_argument(
        "--json", action="store_true", help="emit one machine-readable JSON result"
    )

    publish = profile_commands.add_parser(
        "publish",
        help="Build and run the approved deploy command",
        description="Build, validate, and publish using the configured deploy command.",
        epilog="Example: tokenmaxxing profile publish --sync",
    )
    publish.add_argument(
        "--sync", action="store_true", help="Sync local histories before building"
    )
    publish.add_argument(
        "--non-interactive",
        action="store_true",
        help="refuse unless the deploy command has current approval",
    )
    publish.add_argument(
        "--json", action="store_true", help="emit one machine-readable JSON result"
    )

    status = profile_commands.add_parser(
        "status",
        help="Show profile, deploy, and schedule status",
        description="Show project paths, deploy approval, and schedule state.",
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
    return configured if configured is not None else discover_config(Path.cwd())


def _build_payload(result: BuildResult) -> dict[str, object]:
    return {
        "file_count": result.file_count,
        "generated_at": result.generated_at.isoformat(),
        "site_dir": str(result.site_dir),
        "total_bytes": result.total_bytes,
    }


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _cloudflare_command(
    *, platform: str = sys.platform, environ: Mapping[str, str] = os.environ
) -> tuple[str, ...]:
    if platform != "win32":
        return ("npx", "wrangler", "deploy", "--assets", "{site_dir}")
    node = shutil.which("node.exe")
    launcher = shutil.which("wrangler") or shutil.which("wrangler.cmd")
    candidates: list[Path] = []
    if launcher:
        launcher_path = Path(launcher).resolve()
        candidates.extend(
            (
                launcher_path.parent / "node_modules" / "wrangler" / "bin" / "wrangler.js",
                launcher_path.parent.parent
                / "node_modules"
                / "wrangler"
                / "bin"
                / "wrangler.js",
            )
        )
    if appdata := environ.get("APPDATA"):
        candidates.append(
            Path(appdata) / "npm" / "node_modules" / "wrangler" / "bin" / "wrangler.js"
        )
    script = next((candidate for candidate in candidates if candidate.is_file()), None)
    if node is None or script is None:
        return ()
    return (
        str(Path(node).resolve()),
        str(script.resolve()),
        "deploy",
        "--assets",
        "{site_dir}",
    )


def _prompt_links() -> tuple[ProfileLink, ...]:
    links: list[ProfileLink] = []
    while label := Prompt.ask("Link label (blank to finish)", default=""):
        value = Prompt.ask("Link text")
        while True:
            url = Prompt.ask("Link URL")
            try:
                url = validate_public_link_url(url, "Link URL")
            except ValueError as error:
                print(f"{error}. Try again.", file=sys.stderr)
                continue
            break
        links.append(
            ProfileLink(
                label=label,
                value=value,
                url=url,
            )
        )
    return tuple(links)


def _prompt_timezone(default: str) -> ZoneInfo:
    while True:
        value = Prompt.ask("Timezone", default=default)
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError:
            print("Timezone must be an IANA name. Try again.", file=sys.stderr)


def _prompt_schedule_time(default: str) -> time:
    while True:
        value = Prompt.ask("Daily publish time", default=default)
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and not parsed.second and not parsed.microsecond:
            return parsed
        print("Daily publish time must use HH:MM. Try again.", file=sys.stderr)


def _prompt_canonical_url(default: str) -> str:
    while True:
        value = Prompt.ask("Public profile URL", default=default)
        try:
            return normalize_canonical_url(value, "Public profile URL")
        except ValueError as error:
            print(f"{error}. Try again.", file=sys.stderr)


def _prompt_avatar(path: Path) -> Path | None:
    while True:
        value = Prompt.ask("Avatar path inside this project", default="")
        if not value:
            return None
        avatar = (path.parent / value).resolve()
        if avatar.is_relative_to(path.parent.resolve()):
            return avatar
        print("Avatar path must stay inside this project. Try again.", file=sys.stderr)


def _interactive_config(path: Path, starter: ProfileConfig) -> _OnboardingResult:
    name = Prompt.ask("Name", default=starter.profile.name)
    role = Prompt.ask("Role", default=starter.profile.role)
    bio = Prompt.ask("Bio", default=starter.profile.bio)
    avatar = _prompt_avatar(path)
    links = _prompt_links()
    canonical_url = _prompt_canonical_url(starter.site.canonical_url)
    timezone = _prompt_timezone(starter.site.timezone.key)
    schedule_time = _prompt_schedule_time(
        starter.schedule.time.strftime("%H:%M")
    )
    while True:
        mode = Prompt.ask(
            "Deployment",
            choices=("cloudflare", "custom", "none"),
            default="none",
        )
        command: tuple[str, ...] = ()
        if mode == "cloudflare":
            command = _cloudflare_command()
            if not command:
                print(
                    "Cloudflare needs native node.exe and Wrangler JS paths on Windows; deployment left unset.",
                    file=sys.stderr,
                )
        elif mode == "custom":
            values: list[str] = []
            while value := Prompt.ask("Command argument (blank to finish)", default=""):
                values.append(value)
            command = tuple(values)
        configured = replace(
            starter,
            profile=ProfileInfo(
                name=name,
                role=role,
                bio=bio,
                avatar=avatar,
                links=links,
            ),
            site=SiteConfig(
                title=f"{name}'s token trail",
                description=starter.site.description,
                canonical_url=canonical_url,
                indexable=starter.site.indexable,
                timezone=timezone,
                theme=starter.site.theme,
                accent=starter.site.accent,
            ),
            deploy=DeployConfig(command=command),
            schedule=ScheduleConfig(time=schedule_time),
        )
        try:
            configured = validate_config(path, configured)
        except ValueError as error:
            print(f"Deployment configuration is invalid: {error}. Try again.", file=sys.stderr)
            continue
        return _OnboardingResult(configured, mode == "cloudflare" and bool(command))


def _write_cloudflare_config(path: Path) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="") as output:
            output.write(
                '{\n  "name": "tokenmaxxing-profile",\n'
                '  "assets": {"directory": ".tokenmaxxing/site"}\n}\n'
            )
    except FileExistsError:
        pass


def _check_init_destination(directory: Path, *, force: bool) -> None:
    if not directory.exists():
        return
    if not directory.is_dir():
        raise NotADirectoryError(f"profile project path is not a directory: {directory}")
    if not force and next(directory.iterdir(), None) is not None:
        raise FileExistsError(
            "profile project directory is non-empty; use force to add missing files"
        )


def _initialize(arguments: argparse.Namespace) -> int:
    if getattr(arguments, "config", None) is not None:
        raise ValueError("--config cannot be used with profile init")
    config_path = arguments.directory / "tokenmaxxing.yaml"
    config_existed = config_path.exists()
    _check_init_destination(arguments.directory, force=arguments.force)
    onboarding: _OnboardingResult | None = None
    if not arguments.no_setup and not config_existed:
        onboarding = _interactive_config(
            config_path, load_starter_config(config_path)
        )
    config_path = initialize_project(
        arguments.directory,
        editable_template=arguments.editable_template,
        force=arguments.force,
    )
    if onboarding is not None:
        write_initial_config(config_path, onboarding.config)
        if onboarding.cloudflare:
            _write_cloudflare_config(config_path.parent / "wrangler.jsonc")
        load_config(config_path)
        print("Run `tokenmaxxing profile schedule enable` after your first approved publish.")
    print(f"Profile project → {config_path}")
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
            approval_path=paths.deploy_approval,
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


def _schedule_payload(status: object) -> dict[str, object]:
    return {
        "backend": status.backend,
        "command": list(status.command),
        "enabled": status.enabled,
        "job_path": str(status.job_path) if status.job_path is not None else None,
        "next_step": status.next_step,
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
    if not config.deploy.command:
        approval = "unconfigured"
    elif not paths.deploy_approval.is_file():
        approval = "missing"
    else:
        try:
            plan = make_deploy_plan(config, paths)
        except (FileNotFoundError, ValueError):
            approval = "stale"
        else:
            approval = "current" if is_approved(plan, paths.deploy_approval) else "stale"
    schedule = _scheduler_status_or_unavailable(paths)
    return {
        "approval": approval,
        "canonical_url": config.site.canonical_url,
        "config": str(paths.config),
        "schedule": _schedule_payload(schedule),
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
        print(f"Deploy approval: {payload['approval']}")
        schedule = payload["schedule"]
        assert isinstance(schedule, dict)
        print(
            f"Schedule: {'enabled' if schedule['enabled'] else 'disabled'} "
            f"({schedule['backend']})"
        )
        if schedule["next_step"]:
            print(schedule["next_step"])
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
