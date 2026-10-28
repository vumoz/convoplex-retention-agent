"""The usage-counter upsert must run on Postgres, not only on SQLite.

SQLite accepted `SET hits = hits + 1`. Postgres rejects it as ambiguous because
ON CONFLICT can see both the stored row and EXCLUDED. The webhook treats a
counter failure as fail-open so the reply still goes out.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest

from convoplex.access import (
    LOGGER,
    USAGE_UPSERT_SQL,
    CappedLLM,
    consume_bucket,
    select_mode,
)
from convoplex.actionlog import DbActionLog
from convoplex.config import Settings
from convoplex.db import open_db
from convoplex.integrations.images import load_promo_blob, save_promo_blob
from convoplex.whatsapp_state import WhatsAppStore

_UNQUALIFIED_UPDATE = re.compile(
    r"DO\s+UPDATE\s+SET\s+(\w+)\s*=\s*\1\b",
    re.IGNORECASE,
)
_LOCAL_POSTGRES = "postgresql:///convoplex_test?host=/var/run/postgresql"


def _settings(tmp_path: Path, **extra) -> Settings:
    return Settings(
        db_path=tmp_path / "shop.sqlite",
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        **extra,
    )


def test_usage_upsert_qualifies_the_hits_column():
    folded = " ".join(USAGE_UPSERT_SQL.split()).lower()
    assert "hits = usage_counters.hits + 1" in folded
    assert "hits = hits" not in folded
    root = Path(__file__).resolve().parent.parent / "convoplex"
    for path in root.rglob("*.py"):
        match = _UNQUALIFIED_UPDATE.search(path.read_text(encoding="utf-8"))
        assert match is None, f"{path.name} updates {match.group(1)} without a qualifier"


def test_sqlite_counter_increments_and_stops_at_the_cap(tmp_path):
    settings = _settings(tmp_path)
    conn = open_db(settings)
    try:
        assert consume_bucket(conn, "meta:2026-10-27", 2) is True
        assert consume_bucket(conn, "meta:2026-10-27", 2) is True
        assert consume_bucket(conn, "meta:2026-10-27", 2) is False
        hits = conn.execute(
            "SELECT hits FROM usage_counters WHERE bucket = ?",
            ("meta:2026-10-27",),
        ).fetchone()["hits"]
        assert hits == 2
    finally:
        conn.close()


def test_spent_cap_still_falls_back_when_the_webhook_fails_open(tmp_path):
    settings = _settings(tmp_path, llm_mode="live", meta_api_key="not-sent", meta_daily_cap=1)
    assert select_mode(settings, admin=False, task="chat", fail_open=True) == "live"
    assert select_mode(settings, admin=False, task="chat", fail_open=True) == "mock"


def test_webhook_counter_failure_logs_and_still_calls_the_model(tmp_path, monkeypatch):
    def boom(conn, bucket, limit):
        raise RuntimeError(
            'column reference "hits" is ambiguous LINE 3: '
            "ON CONFLICT(bucket) DO UPDATE SET hits = hits + 1 "
            "postgres://user:hidden-pass@db.example/app"
        )

    monkeypatch.setattr("convoplex.access.consume_bucket", boom)
    notes: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            notes.append(record.getMessage())

    handler = Grab()
    LOGGER.addHandler(handler)
    settings = _settings(tmp_path, llm_mode="live", meta_api_key="not-sent", meta_daily_cap=5)
    calls = {"live": 0, "mock": 0}

    class Live:
        def complete(self, messages, *, tools, max_tokens):
            calls["live"] += 1
            return "reply"

    class Mock:
        def complete(self, messages, *, tools, max_tokens):
            calls["mock"] += 1
            return "mock"

    try:
        with pytest.raises(RuntimeError, match="ambiguous"):
            select_mode(settings, admin=False, task="chat")
        result = CappedLLM(settings, Live(), Mock()).complete([], tools=None, max_tokens=20)
    finally:
        LOGGER.removeHandler(handler)

    assert result == "reply"
    assert calls == {"live": 1, "mock": 0}
    assert notes
    assert "usage_counter_failed" in notes[-1]
    assert "ambiguous" in notes[-1]
    assert "hidden-pass" not in notes[-1]
    assert "not-sent" not in notes[-1]
    assert "postgres://redacted" in notes[-1]


def _postgres_settings(tmp_path: Path) -> Settings:
    pytest.importorskip("psycopg")
    url = os.environ.get("CONVOPLEX_TEST_DATABASE_URL") or _LOCAL_POSTGRES
    try:
        import psycopg

        probe = psycopg.connect(url, connect_timeout=3)
        probe.close()
    except Exception as exc:
        pytest.skip(f"Postgres is not available ({type(exc).__name__})")
    settings = _settings(tmp_path, database_url=url)
    conn = open_db(settings)
    try:
        conn.execute(
            "TRUNCATE TABLE usage_counters, action_log, action_lock, promo_cards, app_kv"
        )
        conn.commit()
    finally:
        conn.close()
    return settings


def test_postgres_counter_upsert_increments_and_stops(tmp_path):
    settings = _postgres_settings(tmp_path)
    conn = open_db(settings)
    try:
        assert consume_bucket(conn, "meta:2026-10-27", 2) is True
        assert consume_bucket(conn, "meta:2026-10-27", 2) is True
        stopped = consume_bucket(conn, "meta:2026-10-27", 2)
        hits = conn.execute(
            "SELECT hits FROM usage_counters WHERE bucket = ?",
            ("meta:2026-10-27",),
        ).fetchone()["hits"]
    finally:
        conn.close()
    assert stopped is False
    assert hits == 2


def test_postgres_action_log_promo_blob_and_whatsapp_state(tmp_path):
    """The other ON CONFLICT writes used on the live path also run on Postgres."""
    settings = _postgres_settings(tmp_path)
    log = DbActionLog(lambda: open_db(settings), postgres=True)
    first = log.append(kind="message", actor="whatsapp", payload={"text": "Hi"}, ts="2026-10-27T00:00:00+00:00")
    second = log.append(kind="message", actor="agent", payload={"text": "Welcome"}, ts="2026-10-27T00:00:01+00:00")
    assert second["prev_hash"] == first["hash"]
    assert log.verify().ok is True

    conn = open_db(settings)
    try:
        conn.execute(
            "UPDATE action_log SET payload = ? WHERE seq = 1",
            ('{"text":"rewritten"}',),
        )
        conn.commit()
        assert log.verify().ok is False

        save_promo_blob(conn, 7, b"RIFF-one")
        assert load_promo_blob(conn, 7) == b"RIFF-one"
        save_promo_blob(conn, 7, b"RIFF-two")
        assert load_promo_blob(conn, 7) == b"RIFF-two"
    finally:
        conn.close()

    store = WhatsAppStore(tmp_path / "ignored.json", connect_fn=lambda: open_db(settings), postgres=True)
    assert store.claim("hash-1") is True
    assert store.claim("hash-1") is False
    store.set_demo_customer(184)
    assert store.demo_customer_id() == 184
    assert not (tmp_path / "ignored.json").exists()
    store.reset()
    assert store.demo_customer_id() is None
