from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


_STARTER_FILES = {
    "tokenmaxxing.yaml": "tokenmaxxing.yaml",
    "custom.css": "custom.css",
    "gitignore": ".gitignore",
}


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    root: Path
    config: Path
    generated: Path
    site: Path
    build_state: Path
    deploy_approval: Path
    logs: Path


def profile_paths(config_path: Path) -> ProfilePaths:
    config = config_path.resolve()
    root = config.parent
    generated = root / ".tokenmaxxing"
    return ProfilePaths(
        root=root,
        config=config,
        generated=generated,
        site=generated / "site",
        build_state=generated / "build.json",
        deploy_approval=generated / "deploy-approval.json",
        logs=generated / "logs",
    )


def _ensure_project_destination(project_root: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(project_root)
    except ValueError as error:
        raise ValueError("profile project writes must stay inside the project") from error
    current = project_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("profile project destinations must not be symlinks")
        if current.exists() and not current.resolve().is_relative_to(project_root):
            raise ValueError("profile project writes must stay inside the project")


def _write_missing(
    source: Traversable, destination: Path, project_root: Path
) -> None:
    _ensure_project_destination(project_root, destination)
    try:
        with destination.open("x", encoding="utf-8", newline="") as output:
            output.write(source.read_text(encoding="utf-8"))
    except FileExistsError:
        pass


def _copy_tree_missing(
    source: Traversable, destination: Path, project_root: Path
) -> None:
    _ensure_project_destination(project_root, destination)
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_tree_missing(child, target, project_root)
        else:
            _write_missing(child, target, project_root)


def initialize_project(
    directory: Path, *, editable_template: bool, force: bool
) -> Path:
    if directory.exists():
        if not directory.is_dir():
            raise NotADirectoryError(f"profile project path is not a directory: {directory}")
        if not force and next(directory.iterdir(), None) is not None:
            raise FileExistsError("profile project directory is non-empty; use force to add missing files")
    else:
        directory.mkdir(parents=True)

    project_root = directory.resolve()
    package = resources.files("tokenmaxxing.profile")
    starters = package.joinpath("starters")
    templates = package.joinpath("templates")
    if editable_template and templates.is_dir():
        _ensure_project_destination(project_root, project_root / "template")
    for resource_name, destination_name in _STARTER_FILES.items():
        _write_missing(
            starters.joinpath(resource_name),
            project_root / destination_name,
            project_root,
        )

    if editable_template and templates.is_dir():
        _copy_tree_missing(templates, project_root / "template", project_root)
    return directory / "tokenmaxxing.yaml"


def _split_windows_command(command: str) -> list[str]:
    arguments: list[str] = []
    length = len(command)
    index = 0
    while index < length:
        while index < length and command[index] in " \t":
            index += 1
        if index == length:
            break
        argument: list[str] = []
        quoted = False
        while index < length:
            backslashes = 0
            while index < length and command[index] == "\\":
                backslashes += 1
                index += 1
            if index < length and command[index] == '"':
                argument.extend("\\" * (backslashes // 2))
                if backslashes % 2:
                    argument.append('"')
                    index += 1
                elif quoted and index + 1 < length and command[index + 1] == '"':
                    argument.append('"')
                    index += 2
                else:
                    quoted = not quoted
                    index += 1
            else:
                argument.extend("\\" * backslashes)
                if index == length or (not quoted and command[index] in " \t"):
                    break
                argument.append(command[index])
                index += 1
        arguments.append("".join(argument))
    return arguments


def _editor_command(environ: Mapping[str, str]) -> list[str]:
    configured = environ.get("VISUAL") or environ.get("EDITOR")
    if configured:
        command = (
            _split_windows_command(configured)
            if sys.platform == "win32"
            else shlex.split(configured, posix=True)
        )
        if command:
            return command
    if sys.platform == "win32":
        return ["notepad.exe"]
    if sys.platform == "darwin":
        return ["open", "-W"]
    return ["xdg-open"]


def open_editor(config_path: Path, environ: Mapping[str, str]) -> int:
    editor = _editor_command(environ)
    completed = subprocess.run(
        [*editor, str(config_path)], shell=False, check=False
    )
    return completed.returncode
