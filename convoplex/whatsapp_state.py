"""Small JSON file for the WhatsApp session.

The action log stays append-only. This file holds the demo customer, the 24-hour
window, seen message-id hashes, and delivery ticks. It stores no tokens, no full
phone numbers, and no full WhatsApp message ids.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from convoplex.config import Settings
from convoplex.integrations.whatsapp import wamid_hash

WINDOW = timedelta(hours=24)
SEEN_LIMIT = 2000
STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3}


def store_path(settings: Settings) -> Path:
    return settings.output_dir / "whatsapp_state.json"


def empty_state() -> dict:
    return {
        "demo_customer_id": None,
        "last_inbound_at": None,
        "seen_ids": [],
        "statuses": {},
        "last_status": None,
    }


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def window_open(last_inbound_at: str | None, now: datetime) -> bool:
    """True when the demo phone has written inside the last 24 hours."""
    last = parse_stamp(last_inbound_at)
    if last is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if last > now:
        return True
    return now - last < WINDOW


def prefer_status(current: str | None, new: str) -> str:
    """Delivery ticks only move forward. A failure sticks."""
    if new == "failed" or current == "failed":
        return "failed"
    if STATUS_RANK.get(new, 0) >= STATUS_RANK.get(current or "", 0):
        return new
    return current or new


def canonical_message_id(value: str) -> str:
    """Store a hash once a raw Cloud API id has been seen."""
    text = str(value or "")
    if text.startswith("wamid."):
        return wamid_hash(text)
    return text


def canonicalize_state(state: dict) -> bool:
    """Rewrite any persisted raw message id to its hash. Returns True if it changed."""
    changed = False
    seen: list[str] = []
    for item in state.get("seen_ids") or []:
        text = str(item)
        nxt = canonical_message_id(text)
        changed = changed or nxt != text
        seen.append(nxt)
    state["seen_ids"] = seen
    statuses: dict[str, str] = {}
    for key, value in (state.get("statuses") or {}).items():
        text = str(key)
        nxt = canonical_message_id(text)
        changed = changed or nxt != text
        if isinstance(value, str):
            statuses[nxt] = value
    state["statuses"] = statuses
    return changed


def stamp_from_message(timestamp: str | None, now: datetime) -> datetime:
    parsed = parse_stamp(timestamp)
    if parsed is None:
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return parsed


WHATSAPP_STATE_KEY = "whatsapp_state"


class WhatsAppStore:
    def __init__(self, path: Path | str, *, connect_fn=None, postgres: bool = False):
        self.path = Path(path)
        self.connect_fn = connect_fn
        self.postgres = postgres

    def load(self) -> dict:
        if self.connect_fn is not None:
            return self._load_db()
        if not self.path.exists():
            return empty_state()
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                raw = handle.read()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if not raw.strip():
            return empty_state()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return empty_state()
        if not isinstance(payload, dict):
            return empty_state()
        state = empty_state()
        state.update(payload)
        if not isinstance(state.get("seen_ids"), list):
            state["seen_ids"] = []
        if not isinstance(state.get("statuses"), dict):
            state["statuses"] = {}
        if canonicalize_state(state):
            self._write_unlocked(state)
        return state

    def update(self, mutate) -> dict:
        if self.connect_fn is not None:
            return self._update_db(mutate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read()
                if raw.strip():
                    try:
                        state = json.loads(raw)
                    except json.JSONDecodeError:
                        state = empty_state()
                    if not isinstance(state, dict):
                        state = empty_state()
                else:
                    state = empty_state()
                base = empty_state()
                base.update(state)
                if not isinstance(base.get("seen_ids"), list):
                    base["seen_ids"] = []
                if not isinstance(base.get("statuses"), dict):
                    base["statuses"] = {}
                canonicalize_state(base)
                mutate(base)
                canonicalize_state(base)
                self._write_handle(handle, base)
                return base
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                self._write_handle(handle, state)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_handle(handle, state: dict) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())

    def demo_customer_id(self) -> int | None:
        value = self.load().get("demo_customer_id")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def set_demo_customer(self, customer_id: int | None) -> None:
        def mutate(state: dict) -> None:
            state["demo_customer_id"] = customer_id

        self.update(mutate)

    def claim(self, message_id: str) -> bool:
        claimed = False

        def mutate(state: dict) -> None:
            nonlocal claimed
            seen = state["seen_ids"]
            if message_id in seen:
                claimed = False
                return
            seen.append(message_id)
            if len(seen) > SEEN_LIMIT:
                del seen[:-SEEN_LIMIT]
            claimed = True

        self.update(mutate)
        return claimed

    def note_inbound(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

        def mutate(state: dict) -> None:
            state["last_inbound_at"] = stamp

        self.update(mutate)

    def set_status(self, wamid: str, status: str) -> str:
        applied = status

        def mutate(state: dict) -> None:
            nonlocal applied
            current = state["statuses"].get(wamid)
            applied = prefer_status(current if isinstance(current, str) else None, status)
            state["statuses"][wamid] = applied
            state["last_status"] = applied

        self.update(mutate)
        return applied

    def statuses(self) -> dict[str, str]:
        raw = self.load().get("statuses") or {}
        return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}

    def reset(self) -> None:
        if self.connect_fn is not None:
            conn = self._connect()
            try:
                self._begin(conn)
                conn.execute("DELETE FROM app_kv WHERE key = ?", (WHATSAPP_STATE_KEY,))
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
            finally:
                conn.close()
            return
        if self.path.exists():
            self.path.unlink()

    def _load_db(self) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM app_kv WHERE key = ?",
                (WHATSAPP_STATE_KEY,),
            ).fetchone()
        finally:
            conn.close()
        state = _state_from_raw(row["value"] if row else "")
        if canonicalize_state(state):
            self._write_db(state)
        return state

    def _update_db(self, mutate) -> dict:
        conn = self._connect()
        try:
            self._begin(conn)
            conn.execute(
                "INSERT INTO app_kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                (WHATSAPP_STATE_KEY, "{}"),
            )
            if self.postgres:
                row = conn.execute(
                    "SELECT value FROM app_kv WHERE key = ? FOR UPDATE",
                    (WHATSAPP_STATE_KEY,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT value FROM app_kv WHERE key = ?",
                    (WHATSAPP_STATE_KEY,),
                ).fetchone()
            state = _state_from_raw(row["value"] if row else "")
            canonicalize_state(state)
            mutate(state)
            canonicalize_state(state)
            text = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                "UPDATE app_kv SET value = ? WHERE key = ?",
                (text, WHATSAPP_STATE_KEY),
            )
            self._commit(conn)
            return state
        except Exception:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def _write_db(self, state: dict) -> None:
        text = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        conn = self._connect()
        try:
            self._begin(conn)
            conn.execute(
                """
                INSERT INTO app_kv (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (WHATSAPP_STATE_KEY, text),
            )
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


def _state_from_raw(raw: str | None) -> dict:
    if not raw or not str(raw).strip():
        return empty_state()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return empty_state()
    if not isinstance(payload, dict):
        return empty_state()
    state = empty_state()
    state.update(payload)
    if not isinstance(state.get("seen_ids"), list):
        state["seen_ids"] = []
    if not isinstance(state.get("statuses"), dict):
        state["statuses"] = {}
    return state
