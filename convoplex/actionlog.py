"""Append-only hash-chained action log.

Each JSONL line is one event (a message or a tool call). The line stores
prev_hash, and hash is SHA-256 over the rest of the entry. verify() recomputes
the chain and reports the first break.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64
REQUIRED = ("seq", "ts", "kind", "actor", "payload", "prev_hash", "hash")


class ChainError(Exception):
    pass


def _canonical(entry: dict) -> str:
    body = {key: value for key, value in entry.items() if key != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(entry: dict) -> str:
    return hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()


def _parse(text: str) -> list[dict]:
    entries: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainError(f"line {line_no}: invalid JSON ({exc})") from exc
        if not isinstance(item, dict):
            raise ChainError(f"line {line_no}: entry must be a JSON object")
        entries.append(item)
    return entries


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    error: str | None = None
    seq: int | None = None


def verify_entries(entries: list[dict]) -> VerifyResult:
    prev = GENESIS
    for index, entry in enumerate(entries, start=1):
        missing = [key for key in REQUIRED if key not in entry]
        if missing:
            return VerifyResult(False, len(entries), f"seq {entry.get('seq', index)}: missing {', '.join(missing)}", index)
        if entry["seq"] != index:
            return VerifyResult(False, len(entries), f"seq {entry['seq']}: expected seq {index}", index)
        if entry["prev_hash"] != prev:
            return VerifyResult(
                False,
                len(entries),
                f"seq {index}: prev_hash does not match the previous entry",
                index,
            )
        if not isinstance(entry["payload"], dict):
            return VerifyResult(False, len(entries), f"seq {index}: payload must be an object", index)
        expected = compute_hash(entry)
        if entry["hash"] != expected:
            return VerifyResult(False, len(entries), f"seq {index}: hash mismatch", index)
        prev = entry["hash"]
    return VerifyResult(True, len(entries), None, entries[-1]["seq"] if entries else 0)


class ActionLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(self, *, kind: str, actor: str, payload: dict, ts: str | None = None) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        safe_payload = json.loads(json.dumps(payload, ensure_ascii=False))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = _parse(handle.read())
                prev = existing[-1]["hash"] if existing else GENESIS
                seq = (existing[-1]["seq"] + 1) if existing else 1
                entry = {
                    "seq": seq,
                    "ts": stamp,
                    "kind": kind,
                    "actor": actor,
                    "payload": safe_payload,
                    "prev_hash": prev,
                }
                entry["hash"] = compute_hash(entry)
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _parse(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def verify(self) -> VerifyResult:
        try:
            entries = self.read_all()
        except ChainError as exc:
            return VerifyResult(False, 0, str(exc), None)
        return verify_entries(entries)


class DbActionLog:
    """Hash chain stored in the shop database.

    Used when DATABASE_URL is set, so a serverless instance does not need the
    local JSONL file. SQLite tests construct this directly. Postgres locks
    action_lock; SQLite uses BEGIN IMMEDIATE on an autocommit connection.
    """

    def __init__(self, connect_fn, *, postgres: bool = False):
        self.connect_fn = connect_fn
        self.postgres = postgres

    def append(self, *, kind: str, actor: str, payload: dict, ts: str | None = None) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        safe_payload = json.loads(json.dumps(payload, ensure_ascii=False))
        stamp = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        conn = self._connect()
        try:
            self._begin(conn)
            existing = self._read(conn)
            prev = existing[-1]["hash"] if existing else GENESIS
            seq = (int(existing[-1]["seq"]) + 1) if existing else 1
            entry = {
                "seq": seq,
                "ts": stamp,
                "kind": kind,
                "actor": actor,
                "payload": safe_payload,
                "prev_hash": prev,
            }
            entry["hash"] = compute_hash(entry)
            conn.execute(
                """
                INSERT INTO action_log (seq, ts, kind, actor, payload, prev_hash, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seq,
                    stamp,
                    kind,
                    actor,
                    json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")),
                    prev,
                    entry["hash"],
                ),
            )
            self._commit(conn)
            return entry
        except Exception:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def read_all(self) -> list[dict]:
        conn = self._connect()
        try:
            return self._read(conn)
        finally:
            conn.close()

    def verify(self) -> VerifyResult:
        try:
            entries = self.read_all()
        except ChainError as exc:
            return VerifyResult(False, 0, str(exc), None)
        return verify_entries(entries)

    def clear(self) -> None:
        conn = self._connect()
        try:
            self._begin(conn)
            conn.execute("DELETE FROM action_log")
            self._commit(conn)
        except Exception:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def _connect(self):
        conn = self.connect_fn()
        if not self.postgres:
            conn.isolation_level = None
        return conn

    def _begin(self, conn) -> None:
        if self.postgres:
            conn.execute(
                "INSERT INTO action_lock (id, held) VALUES (1, 1) ON CONFLICT (id) DO NOTHING"
            )
            conn.execute("SELECT id FROM action_lock WHERE id = 1 FOR UPDATE")
            return
        conn.execute("BEGIN IMMEDIATE")

    def _commit(self, conn) -> None:
        if self.postgres:
            conn.commit()
            return
        conn.execute("COMMIT")

    def _rollback(self, conn) -> None:
        try:
            if self.postgres:
                conn.rollback()
            else:
                conn.execute("ROLLBACK")
        except Exception:
            return

    @staticmethod
    def _read(conn) -> list[dict]:
        rows = conn.execute(
            "SELECT seq, ts, kind, actor, payload, prev_hash, hash FROM action_log ORDER BY seq"
        ).fetchall()
        entries: list[dict] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                parsed = json.loads(payload)
            else:
                parsed = payload
            if not isinstance(parsed, dict):
                raise ChainError(f"seq {row['seq']}: payload must be an object")
            entries.append(
                {
                    "seq": int(row["seq"]),
                    "ts": row["ts"],
                    "kind": row["kind"],
                    "actor": row["actor"],
                    "payload": parsed,
                    "prev_hash": row["prev_hash"],
                    "hash": row["hash"],
                }
            )
        return entries


def open_action_log(settings):
    """JSONL on disk locally. The database table when DATABASE_URL is set."""
    if getattr(settings, "database_url", None):
        from convoplex.db import open_db

        return DbActionLog(lambda: open_db(settings), postgres=True)
    return ActionLog(settings.log_path)
