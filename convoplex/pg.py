"""Thin Postgres connection that accepts the same calls as the sqlite3 connection.

Placeholders stay `?` in the rest of the codebase and are translated here.
INSERT into the shop tables appends RETURNING id so lastrowid keeps working.
One connection per request: do not cache this on the app.
"""

from __future__ import annotations

import re
from typing import Any

from convoplex.db import ID_TABLES, sql_statements

_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)


class PgRow(dict):
    """dict row that also accepts row[0] for the few queries that index columns."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PgCursor:
    def __init__(self, raw: Any, lastrowid: int | None = None):
        self._raw = raw
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        count = getattr(self._raw, "rowcount", -1)
        return int(count if count is not None else -1)

    def fetchone(self):
        row = self._raw.fetchone()
        if row is None:
            return None
        return PgRow(row)

    def fetchall(self):
        return [PgRow(row) for row in self._raw.fetchall()]


class PgConnection:
    def __init__(self, raw: Any):
        self._raw = raw
        self._prepared = False

    def ensure_schema(self, script: str) -> None:
        if self._prepared:
            return
        for statement in sql_statements(script):
            self._raw.execute(statement)
        self._raw.commit()
        self._prepared = True

    def execute(self, sql: str, params: Any = None) -> PgCursor:
        translated, returning = _prepare(sql)
        cursor = self._raw.cursor()
        if params is None:
            cursor.execute(translated)
        else:
            cursor.execute(translated, params)
        lastrowid = None
        if returning:
            row = cursor.fetchone()
            if row is not None and "id" in row:
                lastrowid = int(row["id"])
        return PgCursor(cursor, lastrowid)

    def executemany(self, sql: str, seq) -> PgCursor:
        translated = sql.replace("?", "%s")
        cursor = self._raw.cursor()
        cursor.executemany(translated, list(seq))
        return PgCursor(cursor, None)

    def executescript(self, script: str) -> None:
        for statement in sql_statements(script):
            self._raw.execute(statement)
        self._raw.commit()

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def connect_postgres(url: str, schema_sql: str) -> PgConnection:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "Postgres support needs psycopg. Install dependencies from requirements.txt."
        ) from exc
    raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
    conn = PgConnection(raw)
    conn.ensure_schema(schema_sql)
    return conn


def _prepare(sql: str) -> tuple[str, bool]:
    translated = sql.replace("?", "%s")
    match = _INSERT.match(sql)
    if match is None:
        return translated, False
    table = match.group(1).lower()
    if table not in ID_TABLES:
        return translated, False
    if "returning" in sql.lower():
        return translated, True
    trimmed = translated.rstrip().rstrip(";")
    return trimmed + " RETURNING id", True
