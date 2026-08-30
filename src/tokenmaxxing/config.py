import hashlib
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    db_path: Path
    salt_path: Path


def default_paths(
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> AppPaths:
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if configured_home := environ.get("TOKENMAXXING_HOME"):
        data_dir = Path(configured_home)
    elif platform == "darwin":
        data_dir = home / "Library" / "Application Support" / "tokenmaxxing"
    elif configured_xdg_data_home := environ.get("XDG_DATA_HOME"):
        data_dir = Path(configured_xdg_data_home) / "tokenmaxxing"
    else:
        data_dir = home / ".local" / "share" / "tokenmaxxing"
    return AppPaths(
        data_dir=data_dir,
        db_path=data_dir / "tokenmaxxing.sqlite3",
        salt_path=data_dir / "salt",
    )


def load_or_create_salt(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        salt = path.read_bytes()
    else:
        salt = secrets.token_bytes(32)
        with os.fdopen(descriptor, "wb") as salt_file:
            salt_file.write(salt)
            salt_file.flush()
            os.fsync(salt_file.fileno())
    if len(salt) != 32:
        raise ValueError("salt must contain exactly 32 bytes")
    os.chmod(path, 0o600)
    return salt


def hash_workspace(path: str, salt: bytes) -> str:
    if len(salt) != 32:
        raise ValueError("salt must contain exactly 32 bytes")
    return hashlib.blake2b(path.encode("utf-8"), key=salt, digest_size=32).hexdigest()
