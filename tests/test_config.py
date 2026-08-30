from pathlib import Path
from stat import S_IMODE

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


def test_workspace_hash_is_stable_for_one_salt_and_changes_with_another() -> None:
    assert hash_workspace("/private/project", b"a" * 32) == hash_workspace(
        "/private/project", b"a" * 32
    )
    assert hash_workspace("/private/project", b"a" * 32) != hash_workspace(
        "/private/project", b"b" * 32
    )
