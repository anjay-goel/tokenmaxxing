from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tokenmaxxing.profile.config import DeployConfig, load_config
from tokenmaxxing.profile.deploy import (
    DeployError,
    DeployPlan,
    make_deploy_plan,
    run_deploy,
)
from tokenmaxxing.profile.project import profile_paths


def _plan(tmp_path: Path) -> DeployPlan:
    return DeployPlan(
        argv=(str(Path(sys.executable).resolve()), "deploy"),
        cwd=tmp_path.resolve(),
        canonical_url="https://example.com/profile/",
    )


def _configured_plan(profile_config_path: Path, command: tuple[str, ...]) -> DeployPlan:
    config = replace(
        load_config(profile_config_path),
        deploy=DeployConfig(command=command),
    )
    paths = profile_paths(profile_config_path)
    paths.site.mkdir(parents=True, exist_ok=True)
    return make_deploy_plan(config, paths)


class _Process:
    def __init__(
        self,
        argv: list[str],
        *,
        stdout,
        stderr,
        returncode: int = 0,
        wait_error: BaseException | None = None,
        wait_errors: list[BaseException] | None = None,
        stdout_bytes: bytes = b"published\n",
        stderr_bytes: bytes = b"",
        **kwargs,
    ) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = returncode
        self.wait_error = wait_error
        self.wait_errors = list(wait_errors or [])
        self.pid = 42_424
        self.terminated = False
        self.killed = False
        stdout.write(stdout_bytes)
        stderr.write(stderr_bytes)

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_errors:
            raise self.wait_errors.pop(0)
        if self.wait_error is not None:
            error = self.wait_error
            self.wait_error = None
            raise error
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_make_plan_resolves_executable_and_expands_site(
    profile_config_path: Path,
) -> None:
    plan = _configured_plan(
        profile_config_path,
        (sys.executable, "--site", "{site_dir}"),
    )
    paths = profile_paths(profile_config_path)

    assert plan.argv == (
        str(Path(sys.executable).resolve()),
        "--site",
        str(paths.site),
    )
    assert plan.cwd == paths.root
    assert plan.canonical_url == "https://example.com/tokens/"


def test_make_plan_rejects_an_absent_command(profile_config_path: Path) -> None:
    config = replace(load_config(profile_config_path), deploy=DeployConfig())

    with pytest.raises(ValueError, match="deploy command is not configured"):
        make_deploy_plan(config, profile_paths(profile_config_path))


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ((sys.executable, "{unknown}"), "unknown placeholder"),
        ((sys.executable, "{site_dir}{site_dir}"), "more than one placeholder"),
        ((sys.executable, "bad\0argument"), "NUL"),
    ],
)
def test_make_plan_rejects_unsafe_arguments(
    profile_config_path: Path,
    command: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _configured_plan(profile_config_path, command)


def test_make_plan_requires_an_existing_site(profile_config_path: Path) -> None:
    config = replace(
        load_config(profile_config_path),
        deploy=DeployConfig(command=(sys.executable,)),
    )

    with pytest.raises(FileNotFoundError, match="built profile site"):
        make_deploy_plan(config, profile_paths(profile_config_path))


def test_make_plan_rejects_a_missing_executable(profile_config_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="deploy executable"):
        _configured_plan(profile_config_path, ("definitely-missing-tokenmaxxing",))


@pytest.mark.parametrize("suffix", [".cmd", ".CMD", ".bat", ".BaT"])
def test_windows_rejects_batch_wrappers(
    profile_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    from tokenmaxxing.profile import deploy

    wrapper = profile_config_path.parent / f"npx{suffix}"
    wrapper.write_bytes(b"")
    monkeypatch.setattr(deploy.sys, "platform", "win32")
    monkeypatch.setattr(deploy.shutil, "which", lambda _: str(wrapper))

    with pytest.raises(ValueError, match="native executable"):
        _configured_plan(profile_config_path, (wrapper.name, "deploy"))


def test_windows_allows_node_exe_with_javascript_cli(
    profile_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing.profile import deploy

    node = profile_config_path.parent / "node.exe"
    node.write_bytes(b"")
    cli = profile_config_path.parent / "wrangler.js"
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(deploy.sys, "platform", "win32")
    monkeypatch.setattr(deploy.shutil, "which", lambda _: str(node))

    plan = _configured_plan(profile_config_path, ("node.exe", str(cli), "deploy"))

    assert plan.argv[:2] == (str(node.resolve()), str(cli))


def test_deploy_executes_argv_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def popen(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return _Process(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    plan = _plan(tmp_path)

    result = run_deploy(
        plan,
        non_interactive=False,
        confirm=lambda _: True,
    )

    assert seen["argv"] == list(plan.argv)
    assert seen["kwargs"]["shell"] is False  # type: ignore[index]
    assert seen["kwargs"]["cwd"] == plan.cwd  # type: ignore[index]
    if sys.platform == "win32":
        assert seen["kwargs"]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[index]
    else:
        assert seen["kwargs"]["start_new_session"] is True  # type: ignore[index]
    assert result.stdout == "published\n"


def test_interactive_cancellation_does_not_start_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("deploy process should not start"),
    )
    with pytest.raises(DeployError, match="cancelled"):
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: False,
        )


def test_noninteractive_deploy_runs_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: _Process(argv, **kwargs),
    )

    result = run_deploy(
        plan,
        non_interactive=True,
        confirm=lambda _: pytest.fail("must not prompt"),
    )

    assert result.returncode == 0


def test_deploy_captures_real_process_output(tmp_path: Path) -> None:
    plan = DeployPlan(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import sys; print('ready'); print('note', file=sys.stderr)",
        ),
        cwd=tmp_path,
        canonical_url="https://example.com/profile/",
    )

    result = run_deploy(
        plan,
        non_interactive=False,
        confirm=lambda _: True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["ready"]
    assert result.stderr.splitlines() == ["note"]


def test_nonzero_exit_raises_typed_error_with_bounded_tails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: _Process(
            argv,
            **kwargs,
            returncode=7,
            stdout_bytes=b"x" * 70_000,
            stderr_bytes=b"y" * 70_000,
        ),
    )

    with pytest.raises(DeployError, match="status 7") as caught:
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert caught.value.result is not None
    assert len(caught.value.result.stdout.encode()) == 65_536
    assert len(caught.value.result.stderr.encode()) == 65_536


def test_timeout_terminates_process_and_raises_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import deploy

    process: _Process | None = None
    group_signals: list[tuple[int, signal.Signals]] = []

    def popen(argv, **kwargs):
        nonlocal process
        process = _Process(
            argv,
            **kwargs,
            wait_error=subprocess.TimeoutExpired(argv, 900),
        )
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sent_signal: group_signals.append((pid, sent_signal)),
        raising=False,
    )

    with pytest.raises(DeployError, match="timed out") as caught:
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert caught.value.timed_out
    assert process is not None
    assert group_signals == [(process.pid, signal.SIGTERM)]


@pytest.mark.parametrize(
    "wait_error",
    [KeyboardInterrupt(), RuntimeError("wait failed")],
    ids=["keyboard-interrupt", "unexpected-wait-error"],
)
def test_wait_exceptions_terminate_the_process_group_and_propagate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_error: BaseException,
) -> None:
    from tokenmaxxing.profile import deploy

    process: _Process | None = None
    group_signals: list[tuple[int, signal.Signals]] = []

    def popen(argv, **kwargs):
        nonlocal process
        process = _Process(argv, **kwargs, wait_error=wait_error)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sent_signal: group_signals.append((pid, sent_signal)),
        raising=False,
    )

    with pytest.raises(type(wait_error), match=str(wait_error) or None):
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert process is not None
    assert group_signals == [(process.pid, signal.SIGTERM)]


def test_stubborn_process_group_is_force_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import deploy

    process: _Process | None = None
    group_signals: list[tuple[int, signal.Signals]] = []

    def popen(argv, **kwargs):
        nonlocal process
        process = _Process(
            argv,
            **kwargs,
            wait_errors=[
                subprocess.TimeoutExpired(argv, 900),
                subprocess.TimeoutExpired(argv, 5),
            ],
        )
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    sigkill = getattr(signal, "SIGKILL", 9)
    monkeypatch.setattr(deploy.signal, "SIGKILL", sigkill, raising=False)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sent_signal: group_signals.append((pid, sent_signal)),
        raising=False,
    )

    with pytest.raises(DeployError, match="timed out"):
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert process is not None
    assert group_signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, sigkill),
    ]


def test_windows_process_group_is_isolated_and_tree_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import deploy

    process: _Process | None = None
    sent_signals: list[tuple[int, int]] = []
    taskkill_argv: list[list[str]] = []

    def popen(argv, **kwargs):
        nonlocal process
        assert kwargs["creationflags"] == 0x00000200
        assert "start_new_session" not in kwargs
        process = _Process(
            argv,
            **kwargs,
            wait_errors=[
                subprocess.TimeoutExpired(argv, 900),
                subprocess.TimeoutExpired(argv, 5),
            ],
        )
        return process

    monkeypatch.setattr(deploy.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(
        deploy.os,
        "kill",
        lambda pid, sent_signal: sent_signals.append((pid, sent_signal)),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: (
            taskkill_argv.append(argv) or subprocess.CompletedProcess(argv, 0)
        ),
    )

    with pytest.raises(DeployError, match="timed out"):
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert process is not None
    assert sent_signals == [(process.pid, 1)]
    assert taskkill_argv == [
        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"]
    ]


def test_windows_failed_tree_kill_falls_back_to_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import deploy

    process: _Process | None = None

    def popen(argv, **kwargs):
        nonlocal process
        process = _Process(
            argv,
            **kwargs,
            wait_errors=[
                subprocess.TimeoutExpired(argv, 900),
                subprocess.TimeoutExpired(argv, 5),
            ],
        )
        return process

    monkeypatch.setattr(deploy.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(deploy.os, "kill", lambda *_: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1),
    )

    with pytest.raises(DeployError, match="timed out"):
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert process is not None and process.killed


def test_utf8_tail_remains_within_encoded_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\xff" * 70_000
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: _Process(
            argv,
            **kwargs,
            returncode=2,
            stdout_bytes=payload,
        ),
    )

    with pytest.raises(DeployError) as caught:
        run_deploy(
            _plan(tmp_path),
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert caught.value.result is not None
    stdout = caught.value.result.stdout
    assert len(stdout.encode("utf-8")) <= 65_536
    assert stdout.encode("utf-8").decode("utf-8") == stdout
