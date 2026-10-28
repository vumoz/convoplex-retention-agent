"""Language opt-in: keyword detection, the WhatsApp list, storage, and migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from convoplex.db import connect, ensure_customer_language, init_db
from convoplex.integrations.whatsapp import build_send_interactive_request
from convoplex.languages import (
    LANGUAGES,
    fold_language_text,
    interpret_language,
    language_list_interactive,
    save_language,
)
from convoplex.synthetic.generate import generate
from tests.test_api import _app

_OLD_CUSTOMERS = """
CREATE TABLE retailers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    area TEXT NOT NULL,
    category_focus TEXT NOT NULL,
    phone TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    city TEXT NOT NULL,
    area TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('en', 'pcm')),
    archetype TEXT NOT NULL DEFAULT 'manual',
    synthetic INTEGER NOT NULL DEFAULT 1
);
"""


def test_keywords_ignore_case_and_diacritics_and_leave_normal_replies_alone():
    assert fold_language_text("Èdè Yorùbá") == "ede yoruba"
    assert interpret_language("YORUBA") == {"action": "set", "language": "yo"}
    assert interpret_language("Èdè Yorùbá") == {"action": "set", "language": "yo"}
    assert interpret_language("Igbo") == {"action": "set", "language": "ig"}
    assert interpret_language("Hausa") == {"action": "set", "language": "ha"}
    assert interpret_language("Broken") == {"action": "set", "language": "pcm"}
    assert interpret_language("broken english") == {"action": "set", "language": "pcm"}
    assert interpret_language("English") == {"action": "set", "language": "en"}
    assert interpret_language("change language to Igbo") == {"action": "set", "language": "ig"}
    assert interpret_language("lang") == {"action": "menu"}
    assert interpret_language("language") == {"action": "menu"}
    assert interpret_language("change language") == {"action": "menu"}
    assert interpret_language("lang_ha") == {"action": "set", "language": "ha"}
    assert interpret_language("How much last price?") is None
    assert interpret_language("Yes o, no wahala. Abeg send am today.") is None
    assert interpret_language("The bag is broken") is None


def test_interactive_list_has_one_row_per_language():
    interactive = language_list_interactive()
    assert interactive["type"] == "list"
    rows = interactive["action"]["sections"][0]["rows"]
    assert [row["id"] for row in rows] == [f"lang_{code}" for code in LANGUAGES]
    assert rows[1]["title"] == "Broken English"
    assert "Pidgin" not in rows[1]["title"]
    assert all(len(row["title"]) <= 24 for row in rows)
    assert len(interactive["action"]["button"]) <= 20
    spec = build_send_interactive_request("1150414038145295", "15550001111", interactive, "v21.0")
    body = spec["json"]
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "list"
    assert body["to"] == "15550001111"


def test_default_is_english_and_a_choice_is_stored(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    ada = client.get("/v1/customers/2").json()
    assert ada["language"] == "en"
    assert ada["language_label"] == "English"
    assert ada["language_opted_in"] is False

    saved = client.post("/v1/customers/2/language", json={"language": "yo"})
    assert saved.status_code == 200
    body = saved.json()
    assert body["language"] == "yo"
    assert body["language_label"] == "Yoruba"
    assert body["language_opted_in"] is True
    assert body["language_channel"] == "dashboard"
    assert body["language_opted_in_at"]

    missing = client.post("/v1/customers/2/language", json={"language": "fr"})
    assert missing.status_code == 400

    locked = TestClient(_app(shop, tmp_path, admin_token="s3cret"))
    denied = locked.post("/v1/customers/2/language", json={"language": "ha"})
    assert denied.status_code == 401
    allowed = locked.post(
        "/v1/customers/2/language",
        json={"language": "ha"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["language"] == "ha"


def test_whatsapp_keyword_switches_and_menu_does_not_call_the_agent(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path, whatsapp_demo_to="15550001111"))
    client.post("/v1/whatsapp/customer", json={"customer_id": 2})
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "15550001111",
                                    "id": "wamid.LANG",
                                    "timestamp": "1761523200",
                                    "type": "text",
                                    "text": {"body": "Èdè Yorùbá"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    sent = client.post("/v1/whatsapp/webhook", json=payload)
    assert sent.status_code == 200
    assert sent.json()["processed"] is True
    row = shop.execute("SELECT language, language_opted_in, language_channel FROM customers WHERE id = 2").fetchone()
    assert row["language"] == "yo"
    assert row["language_opted_in"] == 1
    assert row["language_channel"] == "simulator"
    actions = client.get("/v1/actions").json()["entries"]
    assert any(entry.get("payload", {}).get("task") == "language" for entry in actions)
    assert "create_offer" not in json.dumps(actions)


def test_sqlite_migration_widens_the_check_and_marks_broken_english_as_opt_in(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = connect(path)
    conn.executescript(_OLD_CUSTOMERS)
    conn.execute(
        "INSERT INTO retailers (name, city, area, category_focus, phone) VALUES ('Shop', 'Lagos', 'Ikeja', 'provisions', '+2348000001001')"
    )
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language)
        VALUES (1, 'Adaeze Balogun', '+2348030001102', 'Lagos', 'Yaba', 'pcm')
        """
    )
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language)
        VALUES (1, 'Tunde Adeyemi', '+2348030001103', 'Lagos', 'Maryland', 'en')
        """
    )
    conn.commit()
    ensure_customer_language(conn, postgres=False, key=str(path))
    pcm = conn.execute("SELECT language_opted_in, language_channel FROM customers WHERE language = 'pcm'").fetchone()
    english = conn.execute("SELECT language_opted_in FROM customers WHERE language = 'en'").fetchone()
    assert pcm["language_opted_in"] == 1
    assert pcm["language_channel"] == "seed"
    assert english["language_opted_in"] == 0
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language)
        VALUES (1, 'Ngozi Eze', '+2348030001104', 'Lagos', 'Ikeja', 'ig')
        """
    )
    conn.commit()
    assert conn.execute("SELECT language FROM customers WHERE full_name = 'Ngozi Eze'").fetchone()["language"] == "ig"
    conn.close()


def test_customer_184_stays_on_broken_english(tmp_path):
    path = generate(tmp_path / "book.sqlite")
    conn = connect(path)
    row = conn.execute(
        "SELECT language, language_opted_in, language_channel FROM customers WHERE id = 184"
    ).fetchone()
    conn.close()
    assert row["language"] == "pcm"
    assert row["language_opted_in"] == 1
    assert row["language_channel"] == "seed"


def test_save_language_roundtrip(shop):
    assert save_language(shop, 3, "ha", channel="whatsapp", when="2026-10-27T12:00:00+00:00") == "ha"
    row = shop.execute(
        "SELECT language, language_opted_in, language_opted_in_at, language_channel FROM customers WHERE id = 3"
    ).fetchone()
    assert row["language"] == "ha"
    assert row["language_opted_in"] == 1
    assert row["language_opted_in_at"] == "2026-10-27T12:00:00+00:00"
    assert row["language_channel"] == "whatsapp"


def test_fresh_schema_accepts_every_language(tmp_path):
    conn = init_db(tmp_path / "new.sqlite")
    conn.execute(
        "INSERT INTO retailers (name, city, area, category_focus, phone) VALUES ('Shop', 'Lagos', 'Ikeja', 'provisions', '+2348000001001')"
    )
    for index, code in enumerate(LANGUAGES, start=1):
        conn.execute(
            """
            INSERT INTO customers (retailer_id, full_name, phone, city, area, language)
            VALUES (1, ?, ?, 'Lagos', 'Ikeja', ?)
            """,
            (f"Person {index}", f"+23480300019{index:02d}", code),
        )
    conn.commit()
    stored = {row["language"] for row in conn.execute("SELECT language FROM customers")}
    conn.close()
    assert stored == set(LANGUAGES)


def test_postgres_migration_when_available(tmp_path):
    pytest.importorskip("psycopg")
    url = __import__("os").environ.get("CONVOPLEX_TEST_DATABASE_URL") or (
        "postgresql:///convoplex_test?host=/var/run/postgresql"
    )
    try:
        import psycopg

        probe = psycopg.connect(url, connect_timeout=3)
        probe.close()
    except Exception as exc:
        pytest.skip(f"Postgres is not available ({type(exc).__name__})")
    from convoplex.config import Settings
    from convoplex.db import open_db

    settings = Settings(
        db_path=tmp_path / "unused.sqlite",
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        database_url=url,
    )
    conn = open_db(settings)
    try:
        conn.execute("ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_language_check")
        conn.execute(
            "ALTER TABLE customers ADD CONSTRAINT customers_language_check "
            "CHECK (language IN ('en', 'pcm'))"
        )
        conn.execute("ALTER TABLE customers DROP COLUMN IF EXISTS language_opted_in")
        conn.execute("ALTER TABLE customers DROP COLUMN IF EXISTS language_opted_in_at")
        conn.execute("ALTER TABLE customers DROP COLUMN IF EXISTS language_channel")
        conn.execute("ALTER TABLE customers DROP COLUMN IF EXISTS language_hint_sent")
        conn.execute("DELETE FROM order_items")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM offers")
        conn.execute("DELETE FROM customers")
        conn.execute("DELETE FROM products")
        conn.execute("DELETE FROM retailers")
        retailer = conn.execute(
            """
            INSERT INTO retailers (name, city, area, category_focus, phone)
            VALUES ('Shop', 'Lagos', 'Ikeja', 'provisions', '+2348000001001')
            """
        )
        retailer_id = int(retailer.lastrowid)
        conn.execute(
            """
            INSERT INTO customers (retailer_id, full_name, phone, city, area, language)
            VALUES (?, 'Ada', '+2348030001102', 'Lagos', 'Yaba', 'pcm')
            """,
            (retailer_id,),
        )
        conn.commit()
        from convoplex.db import _LANGUAGE_READY

        _LANGUAGE_READY.discard(url)
        ensure_customer_language(conn, postgres=True, key=url)
        row = conn.execute(
            "SELECT language_opted_in, language_channel FROM customers WHERE language = 'pcm'"
        ).fetchone()
        assert row["language_opted_in"] == 1
        assert row["language_channel"] == "seed"
        conn.execute(
            """
            INSERT INTO customers (retailer_id, full_name, phone, city, area, language)
            VALUES (?, 'Ngozi', '+2348030001104', 'Lagos', 'Ikeja', 'ig')
            """,
            (retailer_id,),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM customers WHERE language = 'ig'").fetchone()["n"] == 1
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.execute("DELETE FROM customers")
        conn.execute("DELETE FROM retailers")
        conn.commit()
        conn.close()
