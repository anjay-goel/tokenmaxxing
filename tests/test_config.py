from pathlib import Path
from stat import S_IMODE

import pytest

from tokenmaxxing import config
from tokenmaxxing.config import (
    default_paths,
    hash_workspace,
    load_or_create_salt,
)


def test_salt_is_persistent(tmp_path: Path) -> None:
    path = tmp_path / "salt"
    assert load_or_create_salt(path) == load_or_create_salt(path)


def test_salt_is_private_and_has_the_required_size(tmp_path: Path) -> None:
    salt = load_or_create_salt(tmp_path / "salt")

    assert len(salt) == 32
    assert S_IMODE((tmp_path / "salt").stat().st_mode) == 0o600


def test_default_paths_support_portable_platform_locations(tmp_path: Path) -> None:
    assert default_paths(tmp_path, {}, "darwin").data_dir == (
        tmp_path / "Library" / "Application Support" / "tokenmaxxing"
    )
    assert default_paths(tmp_path, {"XDG_DATA_HOME": "/data"}, "linux").data_dir == (
        Path("/data/tokenmaxxing")
    )
    assert default_paths(
        tmp_path, {"TOKENMAXXING_HOME": "/custom"}, "linux"
    ).data_dir == (Path("/custom"))


def test_windows_data_dir_uses_local_app_data(tmp_path: Path) -> None:
    paths = default_paths(
        tmp_path,
        {"LOCALAPPDATA": r"C:\Users\Ada\AppData\Local"},
        "win32",
    )

    assert paths.data_dir == Path(r"C:\Users\Ada\AppData\Local") / "tokenmaxxing"


def test_windows_data_dir_falls_back_under_user_profile(tmp_path: Path) -> None:
    paths = default_paths(tmp_path, {}, "win32")

    assert paths.data_dir == tmp_path / "AppData" / "Local" / "tokenmaxxing"


def test_windows_accepts_a_valid_salt_when_chmod_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "salt"
    path.write_bytes(b"s" * 32)
    monkeypatch.setattr(config.sys, "platform", "win32")

    def unavailable_chmod(path: Path, mode: int) -> None:
        raise OSError("POSIX modes are unavailable")

    monkeypatch.setattr(config.os, "chmod", unavailable_chmod)

    assert load_or_create_salt(path) == b"s" * 32


def test_workspace_hash_is_stable_for_one_salt_and_changes_with_another() -> None:
    assert hash_workspace("/private/project", b"a" * 32) == hash_workspace(
        "/private/project", b"a" * 32
    )
    assert hash_workspace("/private/project", b"a" * 32) != hash_workspace(
        "/private/project", b"b" * 32
    )
