from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tokenmaxxing.profile.config import DeployConfig, load_config
from tokenmaxxing.profile.deploy import (
    DeployError,
    DeployPlan,
    approve,
    is_approved,
    make_deploy_plan,
    run_deploy,
)
from tokenmaxxing.profile.project import profile_paths


def _plan(tmp_path: Path, fingerprint: str = "abc") -> DeployPlan:
    return DeployPlan(
        command_template=(sys.executable, "deploy"),
        argv=(str(Path(sys.executable).resolve()), "deploy"),
        cwd=tmp_path.resolve(),
        canonical_url="https://example.com/profile/",
        fingerprint=fingerprint,
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


def test_expanded_argv_is_part_of_the_fingerprint(profile_config_path: Path) -> None:
    command = (sys.executable, "{site_dir}")
    first = _configured_plan(profile_config_path, command)
    second_paths = profile_paths(profile_config_path)
    alternate_site = second_paths.generated / "alternate-site"
    alternate_site.mkdir()
    alternate_paths = replace(second_paths, site=alternate_site)
    config = replace(
        load_config(profile_config_path),
        deploy=DeployConfig(command=command),
    )
    second = make_deploy_plan(config, alternate_paths)

    assert first.fingerprint != second.fingerprint


def test_configured_command_template_is_part_of_the_approval(
    profile_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = str(Path(sys.executable).resolve())
    monkeypatch.setattr(
        "tokenmaxxing.profile.deploy.shutil.which",
        lambda _: executable,
    )
    first = _configured_plan(profile_config_path, ("python-one", "deploy"))
    second = _configured_plan(profile_config_path, ("python-two", "deploy"))
    approval_path = profile_config_path.parent / "approval.json"

    assert first.argv == second.argv
    assert first.command_template != second.command_template
    assert first.fingerprint != second.fingerprint
    approve(first, approval_path)
    assert not is_approved(second, approval_path)


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


def test_approval_is_atomic_private_and_strict(tmp_path: Path) -> None:
    approval_path = tmp_path / "generated" / "approval.json"
    plan = _plan(tmp_path)

    approve(plan, approval_path)

    document = json.loads(approval_path.read_text(encoding="utf-8"))
    assert set(document) == {
        "version",
        "fingerprint",
        "command_template",
        "argv",
        "cwd",
        "canonical_url",
        "approved_at",
    }
    assert document["command_template"] == list(plan.command_template)
    assert document["argv"] == list(plan.argv)
    assert stat.S_IMODE(approval_path.stat().st_mode) == 0o600
    assert not list(approval_path.parent.glob(".approval.json.*"))
    assert is_approved(plan, approval_path)


def test_changed_command_invalidates_approval(tmp_path: Path) -> None:
    path = tmp_path / "approval.json"
    approve(_plan(tmp_path, "one"), path)

    assert is_approved(_plan(tmp_path, "one"), path)
    assert not is_approved(_plan(tmp_path, "two"), path)


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        "[]",
        '{"version": 1}',
        json.dumps(
            {
                "version": 1,
                "fingerprint": "abc",
                "command_template": [],
                "argv": [],
                "cwd": "/tmp",
                "canonical_url": "https://example.com/",
                "approved_at": "now",
                "extra": True,
            }
        ),
    ],
)
def test_corrupt_or_noncanonical_approval_is_rejected(
    tmp_path: Path, document: str
) -> None:
    path = tmp_path / "approval.json"
    path.write_text(document, encoding="utf-8")

    assert not is_approved(_plan(tmp_path), path)


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
        approval_path=tmp_path / "approval.json",
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
    assert is_approved(plan, tmp_path / "approval.json")


def test_interactive_cancellation_does_not_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("deploy process should not start"),
    )
    approval_path = tmp_path / "approval.json"

    with pytest.raises(DeployError, match="cancelled"):
        run_deploy(
            _plan(tmp_path),
            approval_path=approval_path,
            non_interactive=False,
            confirm=lambda _: False,
        )

    assert not approval_path.exists()


def test_noninteractive_deploy_requires_approval_and_never_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("deploy process should not start"),
    )

    with pytest.raises(DeployError, match="not approved"):
        run_deploy(
            _plan(tmp_path),
            approval_path=tmp_path / "approval.json",
            non_interactive=True,
            confirm=lambda _: pytest.fail("must not prompt"),
        )


def test_noninteractive_deploy_uses_matching_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    approval_path = tmp_path / "approval.json"
    approve(plan, approval_path)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: _Process(argv, **kwargs),
    )

    result = run_deploy(
        plan,
        approval_path=approval_path,
        non_interactive=True,
        confirm=lambda _: pytest.fail("must not prompt"),
    )

    assert result.returncode == 0


def test_deploy_captures_real_process_output(tmp_path: Path) -> None:
    plan = DeployPlan(
        command_template=(sys.executable, "-c", "print output"),
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import sys; print('ready'); print('note', file=sys.stderr)",
        ),
        cwd=tmp_path,
        canonical_url="https://example.com/profile/",
        fingerprint="real-process",
    )

    result = run_deploy(
        plan,
        approval_path=tmp_path / "approval.json",
        non_interactive=False,
        confirm=lambda _: True,
    )

    assert result.returncode == 0
    assert result.stdout == "ready\n"
    assert result.stderr == "note\n"


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
            approval_path=tmp_path / "approval.json",
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
    )

    with pytest.raises(DeployError, match="timed out") as caught:
        run_deploy(
            _plan(tmp_path),
            approval_path=tmp_path / "approval.json",
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
    )

    with pytest.raises(type(wait_error), match=str(wait_error) or None):
        run_deploy(
            _plan(tmp_path),
            approval_path=tmp_path / "approval.json",
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
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sent_signal: group_signals.append((pid, sent_signal)),
    )

    with pytest.raises(DeployError, match="timed out"):
        run_deploy(
            _plan(tmp_path),
            approval_path=tmp_path / "approval.json",
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert process is not None
    assert group_signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
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
            approval_path=tmp_path / "approval.json",
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
            approval_path=tmp_path / "approval.json",
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
            approval_path=tmp_path / "approval.json",
            non_interactive=False,
            confirm=lambda _: True,
        )

    assert caught.value.result is not None
    stdout = caught.value.result.stdout
    assert len(stdout.encode("utf-8")) <= 65_536
    assert stdout.encode("utf-8").decode("utf-8") == stdout


def test_approval_file_replacement_does_not_follow_destination_symlink(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("symlink permissions vary on Windows")
    victim = tmp_path / "victim.json"
    victim.write_text("private", encoding="utf-8")
    approval = tmp_path / "approval.json"
    approval.symlink_to(victim)

    approve(_plan(tmp_path), approval)

    assert victim.read_text(encoding="utf-8") == "private"
    assert approval.is_file() and not approval.is_symlink()
