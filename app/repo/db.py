"""Short-lived SQLite connections and explicit atomic write transactions."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.repo.models import SCHEMA


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._anchor = None
        self.uri = self.path == ":memory:"
        if self.uri:
            self.path = f"file:invoice-audit-{uuid4().hex}?mode=memory&cache=shared"
            self._anchor = self._connect()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, uri=self.uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connection(self, *, write: bool = False):
        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def close(self):
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
