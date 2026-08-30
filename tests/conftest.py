from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tokenmaxxing.sqlite3"


@pytest.fixture
def database(db_path: Path) -> Iterator[object]:
    from tokenmaxxing.db import Database

    database = Database.open(db_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repository(database: object) -> object:
    from tokenmaxxing.repository import Repository

    return Repository(database)
