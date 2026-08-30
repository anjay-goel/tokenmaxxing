import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _migration_paths() -> list[Path]:
    return sorted(Path(__file__).with_name("migrations").glob("*.sql"))


def _migration_version(path: Path) -> int:
    return int(path.stem.partition("_")[0])


class Database:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            database = cls(connection)
            database._migrate()
            return database
        except BaseException:
            connection.close()
            raise

    def _migrate(self) -> None:
        current_version = int(self.pragma("user_version"))
        for path in _migration_paths():
            version = _migration_version(path)
            if version <= current_version:
                continue
            script = path.read_text(encoding="utf-8")
            try:
                self.connection.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
            except BaseException:
                self.connection.rollback()
                raise
            current_version = version

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def object_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def pragma(self, name: str) -> int | str:
        row = self.connection.execute(f"PRAGMA {name}").fetchone()
        if row is None:
            raise ValueError(f"unknown SQLite pragma: {name}")
        return row[0]

    def close(self) -> None:
        self.connection.close()
