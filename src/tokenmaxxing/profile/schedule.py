from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from tokenmaxxing.profile.build import validate_site
from tokenmaxxing.profile.config import ProfileConfig
from tokenmaxxing.profile.deploy import is_approved, make_deploy_plan
from tokenmaxxing.profile.project import ProfilePaths


Runner = Callable[..., subprocess.CompletedProcess[str]]

_OWNER_KEY = "TokenmaxxingProfileRoot"
_SYSTEMD_OWNER = "# X-Tokenmaxxing-Profile-Root="
_SYSTEMD_COMMAND = "# X-Tokenmaxxing-Command="
_SYSTEMD_WARNING = (
    "User timers may stop after logout unless lingering is already enabled; "
    "Tokenmaxxing never enables linger."
)
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_HISTORY = "View run history in Windows Task Scheduler History."


@dataclass(frozen=True, slots=True)
class ScheduleStatus:
    enabled: bool
    backend: str
    job_path: Path | None
    command: tuple[str, ...]
    next_step: str | None = None


def schedule_identifier(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"tokenmaxxing.profile.{digest}"


def _scheduled_command(
    paths: ProfilePaths, executable: Path, db_path: Path
) -> tuple[str, ...]:
    return (
        str(executable.resolve()),
        "--db",
        str(db_path.resolve()),
        "profile",
        "--config",
        str(paths.config.resolve()),
        "publish",
        "--sync",
        "--non-interactive",
    )


def _scheduler_executable(executable: Path, platform: str) -> Path:
    resolved = executable.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"scheduler executable does not exist: {executable}")
    if platform == "win32":
        if resolved.suffix.casefold() != ".exe":
            raise ValueError("Windows schedules require a native .exe executable")
    elif not os.access(resolved, os.X_OK):
        raise PermissionError(f"scheduler executable is not executable: {resolved}")
    return resolved


def _call(
    runner: Runner,
    argv: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(argv),
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )


def _require_success(
    runner: Runner,
    argv: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    result = _call(runner, argv)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"scheduler command failed with status {result.returncode}{suffix}"
        )
    return result


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _home(environ: Mapping[str, str]) -> Path:
    value = environ.get("HOME")
    if not value:
        raise ValueError("HOME is required to manage this schedule")
    return Path(value).resolve()


def _launch_path(paths: ProfilePaths, environ: Mapping[str, str]) -> Path:
    return (
        _home(environ)
        / "Library"
        / "LaunchAgents"
        / f"{schedule_identifier(paths.root)}.plist"
    )


def _launch_domain() -> str:
    getuid = getattr(os, "getuid", None)
    uid = getuid() if getuid is not None else 0
    return f"gui/{uid}"


def _launch_document(
    paths: ProfilePaths,
    config: ProfileConfig,
    command: tuple[str, ...],
    scheduled_path: str,
) -> bytes:
    document = {
        "Label": schedule_identifier(paths.root),
        _OWNER_KEY: str(paths.root.resolve()),
        "ProgramArguments": list(command),
        "EnvironmentVariables": {"PATH": scheduled_path},
        "WorkingDirectory": str(paths.root.resolve()),
        "StartCalendarInterval": {
            "Hour": config.schedule.time.hour,
            "Minute": config.schedule.time.minute,
        },
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def _read_launch(path: Path, root: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        document = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ValueError(
            f"schedule job is not owned by this profile: {path}"
        ) from error
    if not isinstance(document, dict) or document.get(_OWNER_KEY) != str(
        root.resolve()
    ):
        raise ValueError(f"schedule job is not owned by this profile: {path}")
    return document


def _launch_command(document: dict[str, object] | None) -> tuple[str, ...]:
    if document is None:
        return ()
    values = document.get("ProgramArguments")
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        return ()
    return tuple(values)


def _launch_loaded(identifier: str, runner: Runner) -> bool:
    target = f"{_launch_domain()}/{identifier}"
    return _call(runner, ("launchctl", "print", target)).returncode == 0


def _systemd_paths(
    paths: ProfilePaths, environ: Mapping[str, str]
) -> tuple[Path, Path]:
    config_home = environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).resolve() if config_home else _home(environ) / ".config"
    directory = base / "systemd" / "user"
    identifier = schedule_identifier(paths.root)
    return directory / f"{identifier}.service", directory / f"{identifier}.timer"


def _safe_schedule_value(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("schedule values must not contain control characters")
    return value


def _scheduled_path(environ: Mapping[str, str]) -> str:
    return _safe_schedule_value(environ.get("PATH") or os.defpath)


def _systemd_quote(value: str) -> str:
    value = _safe_schedule_value(value)
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _systemd_environment(name: str, value: str) -> str:
    assignment = _safe_schedule_value(f"{name}={value}")
    escaped = (
        assignment.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _systemd_documents(
    paths: ProfilePaths,
    config: ProfileConfig,
    command: tuple[str, ...],
    scheduled_path: str,
) -> tuple[bytes, bytes]:
    owner = _safe_schedule_value(str(paths.root.resolve()))
    command_marker = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    service = (
        f"{_SYSTEMD_OWNER}{owner}\n"
        f"{_SYSTEMD_COMMAND}{command_marker}\n"
        "[Unit]\n"
        "Description=Publish Tokenmaxxing profile\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment={_systemd_environment('PATH', scheduled_path)}\n"
        f"WorkingDirectory={_systemd_quote(owner)}\n"
        f"ExecStart={' '.join(_systemd_quote(value) for value in command)}\n"
    )
    identifier = schedule_identifier(paths.root)
    timer = (
        f"{_SYSTEMD_OWNER}{owner}\n"
        "[Unit]\n"
        "Description=Publish Tokenmaxxing profile daily\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {config.schedule.time.strftime('%H:%M')}:00\n"
        "Persistent=true\n"
        f"Unit={identifier}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service.encode("utf-8"), timer.encode("utf-8")


def _read_systemd(path: Path, root: Path) -> str | None:
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(
            f"schedule job is not owned by this profile: {path}"
        ) from error
    marker = f"{_SYSTEMD_OWNER}{root.resolve()}\n"
    if not content.startswith(marker):
        raise ValueError(f"schedule job is not owned by this profile: {path}")
    return content


def _systemd_command(content: str | None) -> tuple[str, ...]:
    if content is None:
        return ()
    for line in content.splitlines():
        if not line.startswith(_SYSTEMD_COMMAND):
            continue
        try:
            values = json.loads(line.removeprefix(_SYSTEMD_COMMAND))
        except json.JSONDecodeError:
            return ()
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            return tuple(values)
    return ()


def _systemd_available(runner: Runner) -> bool:
    try:
        result = _call(runner, ("systemctl", "--user", "show-environment"))
    except OSError:
        return False
    return result.returncode == 0


def _cron_recipe(
    config: ProfileConfig, command: tuple[str, ...], scheduled_path: str
) -> str:
    shell_command = (
        f"PATH={shlex.quote(_safe_schedule_value(scheduled_path))} "
        f"{shlex.join(tuple(_safe_schedule_value(value) for value in command))}"
    )
    return (
        f"{config.schedule.time.minute} {config.schedule.time.hour} * * * "
        f"{shell_command.replace('%', r'\%')}"
    )


def _task_name(paths: ProfilePaths) -> str:
    digest = schedule_identifier(paths.root).rsplit(".", 1)[-1]
    return f"\\Tokenmaxxing\\{digest}"


def _task_description(paths: ProfilePaths, command: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "command": command,
            "owner": str(paths.root.resolve()),
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _task_element(parent: ElementTree.Element, name: str, text: str) -> None:
    ElementTree.SubElement(parent, f"{{{_TASK_NAMESPACE}}}{name}").text = text


def _windows_document(
    paths: ProfilePaths,
    config: ProfileConfig,
    command: tuple[str, ...],
    environ: Mapping[str, str],
) -> bytes:
    username = environ.get("USERNAME")
    if not username:
        raise ValueError("USERNAME is required to manage a Windows schedule")
    ElementTree.register_namespace("", _TASK_NAMESPACE)
    task = ElementTree.Element(f"{{{_TASK_NAMESPACE}}}Task", {"version": "1.4"})
    registration = ElementTree.SubElement(
        task, f"{{{_TASK_NAMESPACE}}}RegistrationInfo"
    )
    _task_element(registration, "Description", _task_description(paths, command))

    triggers = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Triggers")
    trigger = ElementTree.SubElement(triggers, f"{{{_TASK_NAMESPACE}}}CalendarTrigger")
    _task_element(
        trigger,
        "StartBoundary",
        f"2000-01-01T{config.schedule.time.strftime('%H:%M')}:00",
    )
    _task_element(trigger, "Enabled", "true")
    daily = ElementTree.SubElement(trigger, f"{{{_TASK_NAMESPACE}}}ScheduleByDay")
    _task_element(daily, "DaysInterval", "1")

    principals = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Principals")
    principal = ElementTree.SubElement(
        principals, f"{{{_TASK_NAMESPACE}}}Principal", {"id": "Author"}
    )
    _task_element(principal, "UserId", username)
    _task_element(principal, "LogonType", "InteractiveToken")
    _task_element(principal, "RunLevel", "LeastPrivilege")

    settings = ElementTree.SubElement(task, f"{{{_TASK_NAMESPACE}}}Settings")
    _task_element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _task_element(settings, "ExecutionTimeLimit", "PT15M")
    _task_element(settings, "Enabled", "true")

    actions = ElementTree.SubElement(
        task, f"{{{_TASK_NAMESPACE}}}Actions", {"Context": "Author"}
    )
    execute = ElementTree.SubElement(actions, f"{{{_TASK_NAMESPACE}}}Exec")
    _task_element(execute, "Command", command[0])
    _task_element(execute, "Arguments", subprocess.list2cmdline(list(command[1:])))
    _task_element(execute, "WorkingDirectory", str(paths.root.resolve()))
    return ElementTree.tostring(task, encoding="utf-8", xml_declaration=True)


def _task_metadata_from_xml(content: str) -> tuple[dict[str, object], bool]:
    try:
        document = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise ValueError("schedule task is not owned by this profile") from error
    description = document.findtext(
        f".//{{{_TASK_NAMESPACE}}}RegistrationInfo/{{{_TASK_NAMESPACE}}}Description"
    )
    if description is None:
        description = document.findtext(".//RegistrationInfo/Description")
    try:
        value = json.loads(description or "")
    except json.JSONDecodeError as error:
        raise ValueError("schedule task is not owned by this profile") from error
    if not isinstance(value, dict):
        raise ValueError("schedule task is not owned by this profile")
    enabled = document.findtext(
        f".//{{{_TASK_NAMESPACE}}}Settings/{{{_TASK_NAMESPACE}}}Enabled"
    )
    return value, enabled is None or enabled.casefold() != "false"


def _windows_task(
    paths: ProfilePaths,
    runner: Runner,
) -> tuple[
    subprocess.CompletedProcess[str],
    dict[str, object] | None,
    bool,
]:
    result = _call(
        runner,
        ("schtasks.exe", "/query", "/tn", _task_name(paths), "/xml"),
    )
    if result.returncode != 0:
        return result, None, False
    description, enabled = _task_metadata_from_xml(result.stdout)
    if description.get("owner") != str(paths.root.resolve()):
        raise ValueError("schedule task is not owned by this profile")
    return result, description, enabled


def _description_command(description: dict[str, object] | None) -> tuple[str, ...]:
    if description is None:
        return ()
    values = description.get("command")
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        return ()
    return tuple(values)


def _enable_launchd(
    paths: ProfilePaths,
    config: ProfileConfig,
    command: tuple[str, ...],
    scheduled_path: str,
    environ: Mapping[str, str],
    runner: Runner,
) -> ScheduleStatus:
    job = _launch_path(paths, environ)
    current = _read_launch(job, paths.root)
    content = _launch_document(paths, config, command, scheduled_path)
    desired = plistlib.loads(content)
    identifier = schedule_identifier(paths.root)
    loaded = _launch_loaded(identifier, runner)
    if loaded and current is None:
        raise ValueError("loaded schedule job is not owned by this profile")
    if loaded and current == desired:
        return ScheduleStatus(True, "launchd", job, command)
    if loaded:
        _require_success(
            runner,
            ("launchctl", "bootout", f"{_launch_domain()}/{identifier}"),
        )
    if current != desired:
        _write_atomic(job, content)
    _require_success(runner, ("launchctl", "bootstrap", _launch_domain(), str(job)))
    return ScheduleStatus(True, "launchd", job, command)


def _enable_systemd(
    paths: ProfilePaths,
    config: ProfileConfig,
    command: tuple[str, ...],
    scheduled_path: str,
    environ: Mapping[str, str],
    runner: Runner,
) -> ScheduleStatus:
    if not _systemd_available(runner):
        return ScheduleStatus(
            False,
            "cron",
            None,
            command,
            _cron_recipe(config, command, scheduled_path),
        )
    service, timer = _systemd_paths(paths, environ)
    _read_systemd(service, paths.root)
    _read_systemd(timer, paths.root)
    service_content, timer_content = _systemd_documents(
        paths, config, command, scheduled_path
    )
    _write_atomic(service, service_content)
    _write_atomic(timer, timer_content)
    timer_name = timer.name
    _require_success(runner, ("systemctl", "--user", "daemon-reload"))
    _require_success(runner, ("systemctl", "--user", "enable", "--now", timer_name))
    return ScheduleStatus(True, "systemd", timer, command, _SYSTEMD_WARNING)


def _enable_windows(
    paths: ProfilePaths,
    config: ProfileConfig,
    command: tuple[str, ...],
    environ: Mapping[str, str],
    runner: Runner,
) -> ScheduleStatus:
    _, existing, _ = _windows_task(paths, runner)
    document = _windows_document(paths, config, command, environ)
    paths.generated.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".schedule.", suffix=".xml", dir=paths.generated
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(document)
        argv = [
            "schtasks.exe",
            "/create",
            "/tn",
            _task_name(paths),
            "/xml",
            str(temporary),
        ]
        if existing is not None:
            argv.append("/f")
        _require_success(runner, argv)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
    return ScheduleStatus(True, "windows", None, command, _TASK_HISTORY)


def enable_schedule(
    paths: ProfilePaths,
    config: ProfileConfig,
    *,
    executable: Path,
    db_path: Path,
    platform: str,
    environ: Mapping[str, str],
    runner: Runner = subprocess.run,
) -> ScheduleStatus:
    validate_site(paths.site, noindex=not config.site.indexable)
    deploy_plan = make_deploy_plan(config, paths)
    if not is_approved(deploy_plan, paths.deploy_approval):
        raise ValueError("deploy command is not approved")
    resolved_executable = _scheduler_executable(executable, platform)
    command = _scheduled_command(paths, resolved_executable, db_path)
    for value in command:
        _safe_schedule_value(value)
    scheduled_path = _scheduled_path(environ)
    if platform == "darwin":
        return _enable_launchd(
            paths, config, command, scheduled_path, environ, runner
        )
    if platform.startswith("linux"):
        return _enable_systemd(
            paths, config, command, scheduled_path, environ, runner
        )
    if platform == "win32":
        return _enable_windows(paths, config, command, environ, runner)
    raise ValueError(f"profile scheduling is not supported on {platform}")


def _launch_status(
    paths: ProfilePaths, environ: Mapping[str, str], runner: Runner
) -> ScheduleStatus:
    job = _launch_path(paths, environ)
    document = _read_launch(job, paths.root)
    command = _launch_command(document)
    enabled = document is not None and _launch_loaded(
        schedule_identifier(paths.root), runner
    )
    return ScheduleStatus(
        enabled, "launchd", job if document is not None else None, command
    )


def _systemd_status(
    paths: ProfilePaths, environ: Mapping[str, str], runner: Runner
) -> ScheduleStatus:
    service, timer = _systemd_paths(paths, environ)
    service_content = _read_systemd(service, paths.root)
    timer_content = _read_systemd(timer, paths.root)
    command = _systemd_command(service_content)
    if (
        service_content is None
        and timer_content is None
        and not _systemd_available(runner)
    ):
        return ScheduleStatus(
            False,
            "cron",
            None,
            (),
            "User systemd is unavailable; enable scheduling to get a cron recipe.",
        )
    available = _systemd_available(runner)
    enabled = False
    if timer_content is not None and available:
        enabled = (
            _call(
                runner,
                ("systemctl", "--user", "is-enabled", "--quiet", timer.name),
            ).returncode
            == 0
        )
    return ScheduleStatus(
        enabled,
        "systemd",
        timer if timer_content is not None else None,
        command,
        _SYSTEMD_WARNING,
    )


def _windows_status(paths: ProfilePaths, runner: Runner) -> ScheduleStatus:
    _, description, enabled = _windows_task(paths, runner)
    return ScheduleStatus(
        enabled,
        "windows",
        None,
        _description_command(description),
        _TASK_HISTORY,
    )


def schedule_status(
    paths: ProfilePaths,
    *,
    platform: str,
    environ: Mapping[str, str],
    runner: Runner = subprocess.run,
) -> ScheduleStatus:
    if platform == "darwin":
        return _launch_status(paths, environ, runner)
    if platform.startswith("linux"):
        return _systemd_status(paths, environ, runner)
    if platform == "win32":
        return _windows_status(paths, runner)
    raise ValueError(f"profile scheduling is not supported on {platform}")


def _disable_launchd(
    paths: ProfilePaths, environ: Mapping[str, str], runner: Runner
) -> ScheduleStatus:
    job = _launch_path(paths, environ)
    document = _read_launch(job, paths.root)
    if document is None:
        return ScheduleStatus(False, "launchd", None, ())
    command = _launch_command(document)
    identifier = schedule_identifier(paths.root)
    if _launch_loaded(identifier, runner):
        _require_success(
            runner,
            ("launchctl", "bootout", f"{_launch_domain()}/{identifier}"),
        )
    job.unlink()
    return ScheduleStatus(False, "launchd", None, command)


def _disable_systemd(
    paths: ProfilePaths, environ: Mapping[str, str], runner: Runner
) -> ScheduleStatus:
    service, timer = _systemd_paths(paths, environ)
    service_content = _read_systemd(service, paths.root)
    timer_content = _read_systemd(timer, paths.root)
    if service_content is None and timer_content is None:
        backend = "systemd" if _systemd_available(runner) else "cron"
        return ScheduleStatus(False, backend, None, ())
    if not _systemd_available(runner):
        raise RuntimeError(
            "user systemd is unavailable; cannot safely disable schedule"
        )
    command = _systemd_command(service_content)
    _require_success(
        runner,
        ("systemctl", "--user", "disable", "--now", timer.name),
    )
    service.unlink(missing_ok=True)
    timer.unlink(missing_ok=True)
    _require_success(runner, ("systemctl", "--user", "daemon-reload"))
    return ScheduleStatus(False, "systemd", None, command, _SYSTEMD_WARNING)


def _disable_windows(paths: ProfilePaths, runner: Runner) -> ScheduleStatus:
    _, description, _ = _windows_task(paths, runner)
    if description is None:
        return ScheduleStatus(False, "windows", None, (), _TASK_HISTORY)
    command = _description_command(description)
    _require_success(
        runner,
        ("schtasks.exe", "/delete", "/tn", _task_name(paths), "/f"),
    )
    return ScheduleStatus(False, "windows", None, command, _TASK_HISTORY)


def disable_schedule(
    paths: ProfilePaths,
    *,
    platform: str,
    environ: Mapping[str, str],
    runner: Runner = subprocess.run,
) -> ScheduleStatus:
    if platform == "darwin":
        return _disable_launchd(paths, environ, runner)
    if platform.startswith("linux"):
        return _disable_systemd(paths, environ, runner)
    if platform == "win32":
        return _disable_windows(paths, runner)
    raise ValueError(f"profile scheduling is not supported on {platform}")
