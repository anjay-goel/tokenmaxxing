from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tokenmaxxing.profile.config import ProfileConfig, load_config
from tokenmaxxing.profile.project import ProfilePaths, profile_paths
from tokenmaxxing.profile.schedule import (
    _safe_schedule_value,
    _systemd_quote,
    disable_schedule,
    enable_schedule,
    schedule_identifier,
    schedule_status,
)


class SchedulerRunner:
    def __init__(self, *, systemd_available: bool = True) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.launch_jobs: set[str] = set()
        self.launch_commands: dict[str, tuple[str, ...]] = {}
        self.systemd_available = systemd_available
        self.systemd_timers: set[str] = set()
        self.windows_tasks: dict[str, str] = {}

    def __call__(
        self, argv: Sequence[str], **kwargs
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(value) for value in argv)
        self.calls.append(command)
        assert kwargs.get("shell") is False
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

        if command[:2] == ("launchctl", "print"):
            identifier = command[2].rsplit("/", 1)[-1]
            return self._result(command, 0 if identifier in self.launch_jobs else 1)
        if command[:2] == ("launchctl", "bootstrap"):
            document = plistlib.loads(Path(command[3]).read_bytes())
            self.launch_jobs.add(document["Label"])
            self.launch_commands[document["Label"]] = tuple(
                document["ProgramArguments"]
            )
            return self._result(command)
        if command[:2] == ("launchctl", "bootout"):
            identifier = command[2].rsplit("/", 1)[-1]
            self.launch_jobs.discard(identifier)
            self.launch_commands.pop(identifier, None)
            return self._result(command)

        if command[:3] == ("systemctl", "--user", "show-environment"):
            return self._result(command, 0 if self.systemd_available else 1)
        if command[:3] == ("systemctl", "--user", "is-enabled"):
            return self._result(command, 0 if command[-1] in self.systemd_timers else 1)
        if command[:3] == ("systemctl", "--user", "enable"):
            self.systemd_timers.add(command[-1])
            return self._result(command)
        if command[:3] == ("systemctl", "--user", "disable"):
            self.systemd_timers.discard(command[-1])
            return self._result(command)
        if command[:3] == ("systemctl", "--user", "daemon-reload"):
            return self._result(command)

        if command[:2] == ("schtasks.exe", "/query"):
            name = command[command.index("/tn") + 1]
            document = self.windows_tasks.get(name)
            return self._result(
                command, 0 if document is not None else 1, document or ""
            )
        if command[:2] == ("schtasks.exe", "/create"):
            name = command[command.index("/tn") + 1]
            xml_path = Path(command[command.index("/xml") + 1])
            if name in self.windows_tasks and "/f" not in command:
                return self._result(command, 1, stderr="task exists")
            self.windows_tasks[name] = xml_path.read_text(encoding="utf-8")
            return self._result(command)
        if command[:2] == ("schtasks.exe", "/delete"):
            name = command[command.index("/tn") + 1]
            self.windows_tasks.pop(name, None)
            return self._result(command)
        raise AssertionError(f"unexpected scheduler command: {command}")

    @staticmethod
    def _result(
        argv: tuple[str, ...],
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.fixture(autouse=True)
def valid_site_and_deploy_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenmaxxing.profile import schedule

    monkeypatch.setattr(schedule, "validate_site", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(schedule, "make_deploy_plan", lambda *_args: object())


def _project(
    tmp_path: Path, minimal_config: str, *, name: str = "My Profile ü"
) -> tuple[ProfilePaths, ProfileConfig, Path, Path]:
    root = tmp_path / name
    root.mkdir()
    config_path = root / "config.yaml"
    config_path.write_text(minimal_config, encoding="utf-8")
    (root / "avatar.webp").write_bytes(b"avatar")
    executable = root / "bin" / "tokenmaxxing.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    database = root / "data" / "usage.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"sqlite")
    return profile_paths(config_path), load_config(config_path), executable, database


def _enable(
    paths: ProfilePaths,
    config: ProfileConfig,
    executable: Path,
    database: Path,
    *,
    platform: str,
    environ: dict[str, str],
    runner: SchedulerRunner,
):
    return enable_schedule(
        paths,
        config,
        executable=executable,
        db_path=database,
        platform=platform,
        environ=environ,
        runner=runner,
    )


def test_schedule_identifier_is_stable_and_project_specific(tmp_path: Path) -> None:
    first = schedule_identifier(tmp_path / "one")

    assert first == schedule_identifier((tmp_path / "one").resolve())
    assert first.startswith("tokenmaxxing.profile.")
    assert len(first.rsplit(".", 1)[-1]) == 12
    assert first != schedule_identifier(tmp_path / "two")


def test_launch_agent_uses_owned_absolute_noninteractive_command(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()

    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="darwin",
        environ={"HOME": str(tmp_path)},
        runner=runner,
    )

    assert status.enabled
    assert status.backend == "launchd"
    assert status.job_path is not None
    document = plistlib.loads(status.job_path.read_bytes())
    assert document["Label"] == schedule_identifier(paths.root)
    assert document["TokenmaxxingProfileRoot"] == str(paths.root.resolve())
    assert document["ProgramArguments"] == list(status.command)
    assert document["EnvironmentVariables"] == {"PATH": os.defpath}
    assert status.command == (
        str(executable.resolve()),
        "--db",
        str(database.resolve()),
        "profile",
        "--config",
        str(paths.config.resolve()),
        "publish",
        "--sync",
        "--non-interactive",
    )
    assert document["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert "StandardOutPath" not in document
    assert "StandardErrorPath" not in document


def test_launch_agent_enable_and_disable_are_idempotent(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    kwargs = {
        "platform": "darwin",
        "environ": {"HOME": str(tmp_path)},
        "runner": runner,
    }

    first = _enable(paths, config, executable, database, **kwargs)
    second = _enable(paths, config, executable, database, **kwargs)
    assert first.job_path == second.job_path
    assert sum(call[:2] == ("launchctl", "bootstrap") for call in runner.calls) == 1
    assert not any(call[:2] == ("launchctl", "bootout") for call in runner.calls)

    disabled = disable_schedule(paths, **kwargs)
    again = disable_schedule(paths, **kwargs)
    assert not disabled.enabled and not again.enabled
    assert first.job_path is not None and not first.job_path.exists()
    assert sum(call[:2] == ("launchctl", "bootout") for call in runner.calls) == 1


def test_launch_agent_reloads_when_the_owned_command_changes(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    environ = {"HOME": str(tmp_path)}
    _enable(
        paths,
        config,
        executable,
        database,
        platform="darwin",
        environ=environ,
        runner=runner,
    )
    replacement = database.with_name("new usage.sqlite3")
    replacement.write_bytes(b"sqlite")

    updated = _enable(
        paths,
        config,
        executable,
        replacement,
        platform="darwin",
        environ=environ,
        runner=runner,
    )

    identifier = schedule_identifier(paths.root)
    lifecycle = [
        call[1]
        for call in runner.calls
        if call[0] == "launchctl" and call[1] in {"bootstrap", "bootout"}
    ]
    assert lifecycle == ["bootstrap", "bootout", "bootstrap"]
    assert runner.launch_commands[identifier] == updated.command


def test_launch_agent_reloads_when_the_scheduled_path_changes(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    first_path = "/opt/Token Tools/bin:/usr/bin"
    second_path = "/opt/Other Tools/bin:/bin"

    first = _enable(
        paths,
        config,
        executable,
        database,
        platform="darwin",
        environ={"HOME": str(tmp_path), "PATH": first_path},
        runner=runner,
    )
    assert first.job_path is not None
    first_document = plistlib.loads(first.job_path.read_bytes())
    assert first_document["EnvironmentVariables"] == {"PATH": first_path}

    updated = _enable(
        paths,
        config,
        executable,
        database,
        platform="darwin",
        environ={"HOME": str(tmp_path), "PATH": second_path},
        runner=runner,
    )

    lifecycle = [
        call[1]
        for call in runner.calls
        if call[0] == "launchctl" and call[1] in {"bootstrap", "bootout"}
    ]
    assert lifecycle == ["bootstrap", "bootout", "bootstrap"]
    assert updated.job_path is not None
    updated_document = plistlib.loads(updated.job_path.read_bytes())
    assert updated_document["EnvironmentVariables"] == {"PATH": second_path}


def test_launch_agent_refuses_a_loaded_job_without_an_owned_document(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    runner.launch_jobs.add(schedule_identifier(paths.root))

    with pytest.raises(ValueError, match="not owned"):
        _enable(
            paths,
            config,
            executable,
            database,
            platform="darwin",
            environ={"HOME": str(tmp_path)},
            runner=runner,
        )

    assert not any(
        call[:2] in {("launchctl", "bootstrap"), ("launchctl", "bootout")}
        for call in runner.calls
    )


def test_launch_agent_refuses_a_foreign_job(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    job = (
        tmp_path
        / "Library"
        / "LaunchAgents"
        / f"{schedule_identifier(paths.root)}.plist"
    )
    job.parent.mkdir(parents=True)
    job.write_bytes(plistlib.dumps({"TokenmaxxingProfileRoot": "someone else"}))

    with pytest.raises(ValueError, match="not owned"):
        _enable(
            paths,
            config,
            executable,
            database,
            platform="darwin",
            environ={"HOME": str(tmp_path)},
            runner=runner,
        )
    with pytest.raises(ValueError, match="not owned"):
        disable_schedule(
            paths,
            platform="darwin",
            environ={"HOME": str(tmp_path)},
            runner=runner,
        )


def test_systemd_units_are_owned_quoted_and_warn_about_logout(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    environ = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="linux",
        environ=environ,
        runner=runner,
    )

    identifier = schedule_identifier(paths.root)
    service = tmp_path / "cfg" / "systemd" / "user" / f"{identifier}.service"
    timer = tmp_path / "cfg" / "systemd" / "user" / f"{identifier}.timer"
    service_text = service.read_text(encoding="utf-8")
    timer_text = timer.read_text(encoding="utf-8")
    assert status.enabled and status.backend == "systemd" and status.job_path == timer
    assert f"X-Tokenmaxxing-Profile-Root={paths.root.resolve()}" in service_text
    assert f"WorkingDirectory={_systemd_quote(str(paths.root.resolve()))}" in service_text
    assert _systemd_quote(str(executable.resolve())) in service_text
    assert "OnCalendar=*-*-* 09:00:00" in timer_text
    assert "Persistent=true" in timer_text
    assert status.next_step is not None and "logout" in status.next_step
    assert not any("linger" in " ".join(call) for call in runner.calls)


def test_systemd_exec_preserves_spaces_unicode_dollars_and_percent_signs(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(
        tmp_path,
        minimal_config,
        name="My $PROFILE 100% ü",
    )
    runner = SchedulerRunner()
    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="linux",
        environ={"HOME": str(tmp_path)},
        runner=runner,
    )

    assert status.job_path is not None
    service = status.job_path.with_suffix(".service")
    exec_start = next(
        line
        for line in service.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    )
    assert "My $$PROFILE 100%% ü" in exec_start
    assert "My $PROFILE 100% ü" not in exec_start


def test_systemd_service_captures_path_without_expanding_literals(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    scheduled_path = "/opt/My Tools/bin:/tmp/$literal:/tmp/100%/bin"

    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="linux",
        environ={"HOME": str(tmp_path), "PATH": scheduled_path},
        runner=runner,
    )

    assert status.job_path is not None
    service = status.job_path.with_suffix(".service")
    assert (
        'Environment="PATH=/opt/My Tools/bin:/tmp/$literal:/tmp/100%%/bin"'
        in service.read_text(encoding="utf-8").splitlines()
    )


def test_linux_without_user_systemd_returns_cron_recipe_only(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner(systemd_available=False)

    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="linux",
        environ={"HOME": str(tmp_path)},
        runner=runner,
    )

    assert not status.enabled and status.backend == "cron"
    assert status.job_path is None
    assert status.next_step is not None
    assert status.next_step.startswith("0 9 * * * ")
    assert "My Profile ü" in status.next_step
    assert not list(tmp_path.rglob("*.service"))
    assert not any(call[0] in {"crontab", "loginctl"} for call in runner.calls)


def test_cron_recipe_sets_path_and_escapes_percent_before_the_shell(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(
        tmp_path, minimal_config, name="My 100% Profile"
    )
    runner = SchedulerRunner(systemd_available=False)
    scheduled_path = "/opt/My Tools/bin:/tmp/100%/bin"

    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="linux",
        environ={"HOME": str(tmp_path), "PATH": scheduled_path},
        runner=runner,
    )

    assert status.next_step is not None
    command = status.next_step.removeprefix("0 9 * * * ")
    assert command.startswith("PATH=")
    assert "/opt/My Tools/bin:/tmp/100\\%/bin" in command
    assert "My 100\\% Profile" in command
    assert re.search(r"(?<!\\)%", command) is None
    assert not any(call[0] in {"crontab", "loginctl"} for call in runner.calls)


@pytest.mark.parametrize("control", ["\n", "\r", "\0"])
def test_schedule_rejects_control_characters_in_path_environment(
    tmp_path: Path, minimal_config: str, control: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner(systemd_available=False)

    with pytest.raises(ValueError, match="control characters"):
        _enable(
            paths,
            config,
            executable,
            database,
            platform="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": f"/usr/bin{control}* * * * * unwanted",
            },
            runner=runner,
        )

    assert not runner.calls


@pytest.mark.parametrize("control", ["\n", "\r"])
def test_schedule_rejects_control_characters_in_command_values(
    tmp_path: Path, minimal_config: str, control: str
) -> None:
    with pytest.raises(ValueError, match="control characters"):
        _safe_schedule_value(f"Profile{control}* * * * * unwanted")


def test_systemd_status_and_disable_preserve_only_owned_units(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    environ = {"HOME": str(tmp_path)}
    enabled = _enable(
        paths,
        config,
        executable,
        database,
        platform="linux",
        environ=environ,
        runner=runner,
    )

    assert schedule_status(
        paths, platform="linux", environ=environ, runner=runner
    ).enabled
    disabled = disable_schedule(paths, platform="linux", environ=environ, runner=runner)
    assert not disabled.enabled
    assert enabled.job_path is not None and not enabled.job_path.exists()
    assert not schedule_status(
        paths, platform="linux", environ=environ, runner=runner
    ).enabled


def test_windows_xml_is_owned_safe_and_quotes_structured_arguments(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()

    status = _enable(
        paths,
        config,
        executable,
        database,
        platform="win32",
        environ={"USERPROFILE": str(tmp_path), "USERNAME": "Ada Ü"},
        runner=runner,
    )

    task_name = f"\\Tokenmaxxing\\{schedule_identifier(paths.root).rsplit('.', 1)[-1]}"
    document = ElementTree.fromstring(runner.windows_tasks[task_name])
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    description = json.loads(
        document.findtext("t:RegistrationInfo/t:Description", namespaces=namespace)
    )
    assert description["owner"] == str(paths.root.resolve())
    assert tuple(description["command"]) == status.command
    assert (
        document.findtext("t:Principals/t:Principal/t:LogonType", namespaces=namespace)
        == "InteractiveToken"
    )
    assert (
        document.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=namespace)
        == "LeastPrivilege"
    )
    assert (
        document.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=namespace)
        == "PT15M"
    )
    assert (
        document.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=namespace)
        == "IgnoreNew"
    )
    assert document.findtext("t:Actions/t:Exec/t:Command", namespaces=namespace) == str(
        executable.resolve()
    )
    arguments = document.findtext("t:Actions/t:Exec/t:Arguments", namespaces=namespace)
    assert arguments == subprocess.list2cmdline(list(status.command[1:]))
    assert (
        document.findtext(
            "t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=namespace
        )
        == "2000-01-01T09:00:00"
    )
    create = next(
        call for call in runner.calls if call[:2] == ("schtasks.exe", "/create")
    )
    assert "/f" not in create
    assert status.next_step is not None and "Task Scheduler History" in status.next_step


def test_windows_verifies_ownership_before_replace_and_delete(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    environ = {"USERPROFILE": str(tmp_path), "USERNAME": "Ada"}
    first = _enable(
        paths,
        config,
        executable,
        database,
        platform="win32",
        environ=environ,
        runner=runner,
    )
    second = _enable(
        paths,
        config,
        executable,
        database,
        platform="win32",
        environ=environ,
        runner=runner,
    )
    creates = [call for call in runner.calls if call[:2] == ("schtasks.exe", "/create")]
    assert first.enabled and second.enabled and "/f" in creates[-1]
    assert schedule_status(
        paths, platform="win32", environ=environ, runner=runner
    ).enabled

    assert not disable_schedule(
        paths, platform="win32", environ=environ, runner=runner
    ).enabled


def test_windows_status_reports_an_existing_disabled_task(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    environ = {"USERPROFILE": str(tmp_path), "USERNAME": "Ada"}
    _enable(
        paths,
        config,
        executable,
        database,
        platform="win32",
        environ=environ,
        runner=runner,
    )
    task_name = f"\\Tokenmaxxing\\{schedule_identifier(paths.root).rsplit('.', 1)[-1]}"
    document = ElementTree.fromstring(runner.windows_tasks[task_name])
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    enabled = document.find(f".//{{{namespace}}}Settings/{{{namespace}}}Enabled")
    assert enabled is not None
    enabled.text = "false"
    runner.windows_tasks[task_name] = ElementTree.tostring(document, encoding="unicode")

    status = schedule_status(paths, platform="win32", environ=environ, runner=runner)

    assert not status.enabled
    assert status.command
    assert not disable_schedule(
        paths, platform="win32", environ=environ, runner=runner
    ).enabled


def test_windows_refuses_foreign_task_without_overwrite_or_delete(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    task_name = f"\\Tokenmaxxing\\{schedule_identifier(paths.root).rsplit('.', 1)[-1]}"
    runner.windows_tasks[task_name] = (
        "<Task><RegistrationInfo><Description>foreign</Description></RegistrationInfo></Task>"
    )
    environ = {"USERPROFILE": str(tmp_path), "USERNAME": "Ada"}

    with pytest.raises(ValueError, match="not owned"):
        _enable(
            paths,
            config,
            executable,
            database,
            platform="win32",
            environ=environ,
            runner=runner,
        )
    with pytest.raises(ValueError, match="not owned"):
        disable_schedule(paths, platform="win32", environ=environ, runner=runner)
    assert not any(call[:2] == ("schtasks.exe", "/create") for call in runner.calls)
    assert not any(call[:2] == ("schtasks.exe", "/delete") for call in runner.calls)


def test_enable_requires_a_valid_site(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing.profile import schedule

    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()
    monkeypatch.setattr(
        schedule,
        "validate_site",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad site")),
    )
    with pytest.raises(ValueError, match="bad site"):
        _enable(
            paths,
            config,
            executable,
            database,
            platform="darwin",
            environ={"HOME": str(tmp_path)},
            runner=runner,
        )
    assert not runner.calls



def test_enable_rejects_a_missing_or_nonexecutable_scheduler_command(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    runner = SchedulerRunner()

    with pytest.raises(FileNotFoundError, match="scheduler executable"):
        _enable(
            paths,
            config,
            executable.with_name("missing"),
            database,
            platform="linux",
            environ={"HOME": str(tmp_path)},
            runner=runner,
        )
    if os.name != "nt":
        executable.chmod(0o644)
        with pytest.raises(PermissionError, match="scheduler executable"):
            _enable(
                paths,
                config,
                executable,
                database,
                platform="linux",
                environ={"HOME": str(tmp_path)},
                runner=runner,
            )

    assert not runner.calls


def test_windows_requires_a_native_scheduler_executable(
    tmp_path: Path, minimal_config: str
) -> None:
    paths, config, executable, database = _project(tmp_path, minimal_config)
    script = executable.with_suffix(".py")
    script.write_text("print('tokenmaxxing')\n", encoding="utf-8")
    runner = SchedulerRunner()

    with pytest.raises(ValueError, match="native .exe"):
        _enable(
            paths,
            config,
            script,
            database,
            platform="win32",
            environ={"USERPROFILE": str(tmp_path), "USERNAME": "Ada"},
            runner=runner,
        )

    assert not runner.calls
