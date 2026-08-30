from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import string
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tokenmaxxing.profile.config import ProfileConfig
from tokenmaxxing.profile.project import ProfilePaths


_APPROVAL_KEYS = frozenset(
    {
        "version",
        "fingerprint",
        "command_template",
        "argv",
        "cwd",
        "canonical_url",
        "approved_at",
    }
)
_OUTPUT_LIMIT = 64 * 1024
_DEPLOY_TIMEOUT = 900


@dataclass(frozen=True, slots=True)
class DeployPlan:
    command_template: tuple[str, ...]
    argv: tuple[str, ...]
    cwd: Path
    canonical_url: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class DeployResult:
    returncode: int
    stdout: str
    stderr: str


class DeployError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        result: DeployResult | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.timed_out = timed_out


def _fingerprint(
    command_template: tuple[str, ...],
    argv: tuple[str, ...],
    cwd: Path,
    canonical_url: str,
) -> str:
    document = json.dumps(
        {
            "command_template": command_template,
            "argv": argv,
            "cwd": str(cwd),
            "canonical_url": canonical_url,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode()).hexdigest()


def _expand_argument(argument: str, site_dir: Path) -> str:
    if "\0" in argument:
        raise ValueError("deploy command arguments must not contain NUL bytes")
    try:
        fields = tuple(string.Formatter().parse(argument))
    except ValueError as error:
        raise ValueError("deploy command contains an invalid placeholder") from error
    placeholders = [field for _, field, _, _ in fields if field is not None]
    if any(field != "site_dir" for field in placeholders):
        raise ValueError("deploy command contains an unknown placeholder")
    if len(placeholders) > 1:
        raise ValueError("deploy command has more than one placeholder in an argument")
    for _, field, format_spec, conversion in fields:
        if field is not None and (format_spec or conversion is not None):
            raise ValueError("deploy command contains an unknown placeholder")
    return argument.format(site_dir=str(site_dir))


def _resolve_executable(value: str, cwd: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate if candidate.is_absolute() else cwd / candidate
        resolved = resolved.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"deploy executable does not exist: {value}")
    else:
        located = shutil.which(value)
        if located is None:
            raise FileNotFoundError(f"deploy executable was not found: {value}")
        resolved = Path(located).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"deploy executable does not exist: {resolved}")
    if sys.platform == "win32":
        if resolved.suffix.lower() in {".cmd", ".bat"}:
            raise ValueError(
                "Windows deploy commands must use a native executable, not .cmd or .bat"
            )
    elif not os.access(resolved, os.X_OK):
        raise PermissionError(f"deploy executable is not executable: {resolved}")
    return resolved


def make_deploy_plan(config: ProfileConfig, paths: ProfilePaths) -> DeployPlan:
    if not config.deploy.command:
        raise ValueError("deploy command is not configured")
    if not paths.site.is_dir():
        raise FileNotFoundError(f"built profile site does not exist: {paths.site}")

    cwd = paths.root.resolve()
    site_dir = paths.site.resolve()
    expanded = tuple(
        _expand_argument(argument, site_dir) for argument in config.deploy.command
    )
    executable = _resolve_executable(expanded[0], cwd)
    argv = (str(executable), *expanded[1:])
    return DeployPlan(
        command_template=config.deploy.command,
        argv=argv,
        cwd=cwd,
        canonical_url=config.site.canonical_url,
        fingerprint=_fingerprint(
            config.deploy.command,
            argv,
            cwd,
            config.site.canonical_url,
        ),
    )


def _approval_document(plan: DeployPlan) -> dict[str, object]:
    return {
        "version": 1,
        "fingerprint": plan.fingerprint,
        "command_template": list(plan.command_template),
        "argv": list(plan.argv),
        "cwd": str(plan.cwd),
        "canonical_url": plan.canonical_url,
        "approved_at": datetime.now(UTC).isoformat(),
    }


def _write_private_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(document, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def approve(plan: DeployPlan, approval_path: Path) -> None:
    _write_private_json(approval_path, _approval_document(plan))


def _valid_approval(document: object, plan: DeployPlan) -> bool:
    if not isinstance(document, dict) or set(document) != _APPROVAL_KEYS:
        return False
    if type(document["version"]) is not int or document["version"] != 1:
        return False
    command_template = document["command_template"]
    if not isinstance(command_template, list) or not all(
        isinstance(value, str) for value in command_template
    ):
        return False
    if command_template != list(plan.command_template):
        return False
    argv = document["argv"]
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return False
    if argv != list(plan.argv):
        return False
    if document["fingerprint"] != plan.fingerprint:
        return False
    if document["cwd"] != str(plan.cwd):
        return False
    if document["canonical_url"] != plan.canonical_url:
        return False
    approved_at = document["approved_at"]
    if not isinstance(approved_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(approved_at)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def is_approved(plan: DeployPlan, approval_path: Path) -> bool:
    try:
        document = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return _valid_approval(document, plan)


def _read_tail(stream) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - _OUTPUT_LIMIT))
    decoded = stream.read().decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    if len(encoded) <= _OUTPUT_LIMIT:
        return decoded
    return encoded[-_OUTPUT_LIMIT:].decode("utf-8", errors="ignore")


def _process_group_options() -> dict[str, object]:
    if sys.platform == "win32":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": creation_flag}
    return {"start_new_session": True}


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            os.kill(process.pid, getattr(signal, "CTRL_BREAK_EVENT", 1))
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
            )
            if completed.returncode == 0:
                return
        except BaseException:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    _terminate_process_group(process)
    try:
        process.wait(timeout=5)
        return
    except BaseException:
        pass
    _kill_process_group(process)
    try:
        process.wait(timeout=5)
    except BaseException:
        pass


def run_deploy(
    plan: DeployPlan,
    *,
    approval_path: Path,
    non_interactive: bool,
    confirm: Callable[[DeployPlan], bool],
) -> DeployResult:
    if non_interactive:
        if not is_approved(plan, approval_path):
            raise DeployError("deploy command is not approved")
    else:
        if not confirm(plan):
            raise DeployError("deployment cancelled")
        approve(plan, approval_path)

    with tempfile.TemporaryFile("w+b") as stdout, tempfile.TemporaryFile(
        "w+b"
    ) as stderr:
        try:
            process = subprocess.Popen(
                list(plan.argv),
                cwd=plan.cwd,
                shell=False,
                stdout=stdout,
                stderr=stderr,
                **_process_group_options(),
            )
        except OSError as error:
            raise DeployError(f"could not start deploy command: {error}") from error
        try:
            returncode = process.wait(timeout=_DEPLOY_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            _stop_process_group(process)
            result = DeployResult(
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=_read_tail(stdout),
                stderr=_read_tail(stderr),
            )
            raise DeployError(
                f"deployment timed out after {_DEPLOY_TIMEOUT} seconds",
                result=result,
                timed_out=True,
            ) from error
        except BaseException:
            _stop_process_group(process)
            raise

        result = DeployResult(
            returncode=returncode,
            stdout=_read_tail(stdout),
            stderr=_read_tail(stderr),
        )
    if result.returncode != 0:
        raise DeployError(
            f"deployment exited with status {result.returncode}", result=result
        )
    return result
