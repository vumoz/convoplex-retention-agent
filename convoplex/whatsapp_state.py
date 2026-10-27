"""Small JSON file for the WhatsApp session.

The action log stays append-only. This file holds the demo customer, the 24-hour
window, seen message ids, and delivery ticks. It stores no tokens and no full
phone numbers.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from convoplex.config import Settings

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


def stamp_from_message(timestamp: str | None, now: datetime) -> datetime:
    parsed = parse_stamp(timestamp)
    if parsed is None:
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return parsed


class WhatsAppStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict:
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
        return state

    def update(self, mutate) -> dict:
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
                mutate(base)
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(base, ensure_ascii=False, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
                return base
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
        if self.path.exists():
            self.path.unlink()
