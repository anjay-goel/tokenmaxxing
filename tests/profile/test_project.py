from pathlib import Path
from subprocess import CompletedProcess, list2cmdline

import pytest

from tokenmaxxing.profile.config import load_config
from tokenmaxxing.profile.project import (
    ProfilePaths,
    _split_windows_command,
    initialize_project,
    open_editor,
    profile_paths,
)


def test_profile_paths_put_the_deployable_site_in_dist(tmp_path: Path) -> None:
    config = tmp_path / "profile" / "config.yaml"

    assert profile_paths(config) == ProfilePaths(
        root=config.resolve().parent,
        config=config.resolve(),
        site=config.resolve().parent / "dist",
    )


def test_initialize_project_creates_only_editable_starters(tmp_path: Path) -> None:
    project = tmp_path / "profile"

    config = initialize_project(project, editable_template=False, force=False)

    assert config == project / "config.yaml"
    assert not (project / "tokenmaxxing.yaml").exists()
    assert (project / "custom.css").read_text(encoding="utf-8") == ""
    assert "dist/" in (project / ".gitignore").read_text(encoding="utf-8")
    assert not (project / "template").exists()
    assert not (project / ".tokenmaxxing").exists()
    loaded = load_config(config)
    assert loaded.site.title == "Your Name | Token Trail"
    assert loaded.site.description == "A visual snapshot of AI agent usage."
    assert loaded.site.indexable is False
    assert loaded.deploy.command == ()


def test_initialize_project_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    project = tmp_path / "profile"
    project.mkdir()
    (project / "notes.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="non-empty"):
        initialize_project(project, editable_template=False, force=False)

    assert list(project.iterdir()) == [project / "notes.txt"]


def test_force_never_overwrites_an_editable_file(tmp_path: Path) -> None:
    project = tmp_path / "profile"
    project.mkdir()
    css = project / "custom.css"
    css.write_text("body { color: hotpink; }\n", encoding="utf-8")

    initialize_project(project, editable_template=False, force=True)

    assert css.read_text(encoding="utf-8") == "body { color: hotpink; }\n"
    assert (project / "config.yaml").is_file()


def test_force_preserves_an_existing_config(tmp_path: Path) -> None:
    project = tmp_path / "profile"
    project.mkdir()
    config = project / "config.yaml"
    config.write_text("personal: formatting\n", encoding="utf-8")

    initialize_project(project, editable_template=False, force=True)

    assert config.read_text(encoding="utf-8") == "personal: formatting\n"


def test_force_rejects_an_editable_template_symlink_outside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import project as project_module

    packaged = tmp_path / "packaged"
    starters = packaged / "starters"
    starters.mkdir(parents=True)
    (starters / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    (starters / "custom.css").write_text("", encoding="utf-8")
    (starters / "gitignore").write_text(".tokenmaxxing/\n", encoding="utf-8")
    templates = packaged / "templates"
    templates.mkdir()
    (templates / "index.html.j2").write_text("private template", encoding="utf-8")
    monkeypatch.setattr(project_module.resources, "files", lambda package: packaged)

    profile = tmp_path / "profile"
    profile.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (profile / "template").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        initialize_project(profile, editable_template=True, force=True)

    assert list(outside.iterdir()) == []


def test_posix_editor_prefers_visual_and_waits_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import project

    config = tmp_path / "config.yaml"
    calls: list[tuple[list[str], bool, bool]] = []

    def run(
        argv: list[str], *, shell: bool, check: bool
    ) -> CompletedProcess[str]:
        calls.append((argv, shell, check))
        return CompletedProcess(argv, 7)

    monkeypatch.setattr(project.sys, "platform", "darwin")
    monkeypatch.setattr(project.subprocess, "run", run)

    result = open_editor(
        config,
        {"VISUAL": "code --wait", "EDITOR": "ignored-editor"},
    )

    assert result == 7
    assert calls == [(["code", "--wait", str(config)], False, False)]


def test_windows_editor_parses_a_quoted_program_files_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import project

    config = tmp_path / "config.yaml"
    calls: list[list[str]] = []

    def run(
        argv: list[str], *, shell: bool, check: bool
    ) -> CompletedProcess[str]:
        assert shell is False
        assert check is False
        calls.append(argv)
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(project.sys, "platform", "win32")
    monkeypatch.setattr(project.subprocess, "run", run)

    result = open_editor(
        config,
        {
            "EDITOR": (
                r'"C:\Program Files\Example Editor\editor.exe" '
                r'--reuse-window'
            )
        },
    )

    assert result == 0
    assert calls == [
        [
            r"C:\Program Files\Example Editor\editor.exe",
            "--reuse-window",
            str(config),
        ]
    ]


def test_windows_command_parser_preserves_empty_arguments() -> None:
    assert _split_windows_command('editor.exe "" --wait') == [
        "editor.exe",
        "",
        "--wait",
    ]


def test_windows_command_parser_separates_an_argument_ending_in_backslash() -> None:
    command = "editor.exe " + "C:\\output\\" + " --wait"

    assert _split_windows_command(command) == [
        "editor.exe",
        "C:\\output\\",
        "--wait",
    ]


def test_windows_command_parser_round_trips_list2cmdline() -> None:
    arguments = [
        r"C:\Program Files\Example Editor\editor.exe",
        "",
        "C:\\output\\",
        "two words",
        'quote"inside',
        "--wait",
    ]

    assert _split_windows_command(list2cmdline(arguments)) == arguments


def test_windows_editor_falls_back_to_notepad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.profile import project

    config = tmp_path / "config.yaml"
    calls: list[list[str]] = []

    def run(
        argv: list[str], *, shell: bool, check: bool
    ) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(project.sys, "platform", "win32")
    monkeypatch.setattr(project.subprocess, "run", run)

    open_editor(config, {})

    assert calls == [["notepad.exe", str(config)]]
