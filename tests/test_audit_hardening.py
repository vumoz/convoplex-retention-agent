"""Regression tests for the October 2026 audit. One test name per finding."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.commercial import claims_mismatch, commercial_payload, reconcile_text
from convoplex.config import Settings
from convoplex.execution import ExecutionContext
from convoplex.integrations.images import overlay_copy
from convoplex.llm import Completion, MockMuse, ToolCall
from convoplex.messages import out_of_stock_message
from convoplex.purchase import confirm_purchase
from convoplex.tools import create_offer, execute_tool, place_order
from tests.test_api import _app
from tests.test_whatsapp import DEMO_DIGITS, SECRET, _inbound, _live_app, _sign

ROOT = Path(__file__).resolve().parents[1]


class _OrderStub:
    """A model that tries to buy without a customer yes."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(
            content="",
            tool_calls=[
                ToolCall(
                    id="call_stub_order",
                    name="place_order",
                    arguments={"customer_id": 1, "product_id": 1, "qty": 1, "offer_id": 1},
                )
            ],
            finish_reason="tool_calls",
        )


class _Lie:
    mode = "mock"

    def __init__(self, text: str):
        self.text = text

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content=self.text, finish_reason="stop")


def _orders(shop) -> int:
    return int(shop.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"])


def _stock(shop, product_id: int = 1) -> int:
    return int(shop.execute("SELECT stock_qty FROM products WHERE id = ?", (product_id,)).fetchone()["stock_qty"])


def _ctx(customer_id: int = 1) -> ExecutionContext:
    return ExecutionContext(
        retailer_id=1,
        customer_id=customer_id,
        conversation_id=customer_id,
        inbound_event_id="evt-test",
    )


def test_f01_repeated_refusal_and_unconfirmed_place_order_do_not_buy(shop, log_path):
    log = ActionLog(log_path)
    drafted = draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    before = _orders(shop)
    stock = _stock(shop)
    refused = place_order(
        shop, customer_id=1, product_id=1, qty=1, offer_id=drafted.offer["offer_id"]
    )
    assert refused["ok"] is False
    assert refused.get("confirmed") is False
    assert _orders(shop) == before
    assert _stock(shop) == stock

    draft_winback(shop, log, MockMuse(), 2, max_tokens=1000, language="en")
    for text, voice in (("I won't buy rice.", False), ("I won't buy rice.", True)):
        result = handle_reply(shop, log, _OrderStub(), 2, text, max_tokens=1000, from_voice=voice)
        assert result.intent == "decline"
        assert result.order is None
    assert _orders(shop) == before
    assert _stock(shop) == stock

    shop.execute("UPDATE customers SET language = 'pcm' WHERE id = 2")
    shop.commit()
    draft_winback(shop, log, MockMuse(), 2, max_tokens=1000)
    first = handle_reply(shop, log, _OrderStub(), 2, "I won't buy rice.", max_tokens=1000, from_voice=True)
    second = handle_reply(shop, log, _OrderStub(), 2, "I won't buy rice.", max_tokens=1000, from_voice=True)
    assert first.intent == "question"
    assert first.confirm_offer_id
    assert "₦70,200" in first.message
    assert second.order is None
    assert second.intent != "accept"
    assert _orders(shop) == before
    assert _stock(shop) == stock

    no = handle_reply(shop, log, _OrderStub(), 1, "No thank you", max_tokens=1000)
    assert no.intent == "decline"
    assert no.order is None

    yes = handle_reply(shop, log, _OrderStub(), 2, "oya", max_tokens=1000)
    assert yes.intent == "accept"
    assert yes.order and yes.order["ok"]
    assert yes.order["offer_id"] == first.confirm_offer_id
    assert _stock(shop) == stock - 1


def test_f01_stale_confirmation_cannot_execute_and_languages_refuse(shop, log_path):
    offer = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    confirm_purchase(
        shop,
        customer_id=1,
        retailer_id=1,
        product_id=1,
        qty=1,
        offer_id=offer["offer_id"],
    )
    shop.execute(
        "UPDATE purchase_states SET expires_at = '2000-01-01T00:00:00+00:00' WHERE state = 'confirmed'"
    )
    shop.commit()
    before = _orders(shop)
    stock = _stock(shop)
    stale = place_order(shop, customer_id=1, product_id=1, qty=1, offer_id=offer["offer_id"])
    assert stale["ok"] is False
    assert _orders(shop) == before
    assert _stock(shop) == stock

    replaced = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    confirm_purchase(
        shop,
        customer_id=1,
        retailer_id=1,
        product_id=1,
        qty=1,
        offer_id=offer["offer_id"],
    )
    fresh = create_offer(shop, customer_id=1, product_id=1, discount_percent=12)
    assert fresh["offer_id"] != replaced["offer_id"]
    blocked = place_order(shop, customer_id=1, product_id=1, qty=1, offer_id=offer["offer_id"])
    assert blocked["ok"] is False
    assert _orders(shop) == before
    assert _stock(shop) == stock

    log = ActionLog(log_path)
    for language, phrase in (
        ("en", "I won't buy."),
        ("pcm", "I no want am"),
        ("ig", "No thank you"),
        ("yo", "No thank you"),
        ("ha", "No thank you"),
    ):
        shop.execute("UPDATE customers SET language = ? WHERE id = 2", (language,))
        shop.commit()
        draft_winback(shop, log, MockMuse(), 2, max_tokens=1000, language=language)
        voice = handle_reply(shop, log, _OrderStub(), 2, phrase, max_tokens=1000, from_voice=True)
        typed = handle_reply(shop, log, _OrderStub(), 2, phrase, max_tokens=1000, from_voice=False)
        assert voice.order is None
        assert typed.order is None
        assert voice.intent == "decline"
        assert typed.intent == "decline"
    assert _stock(shop) == stock


def test_f01_clear_yes_phrases_place_the_open_offer(shop, log_path):
    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    result = handle_reply(shop, log, _OrderStub(), 1, "I wan buy am", max_tokens=1000)
    assert result.intent == "accept"
    assert result.order and result.order["total_ngn"] == 70200
    assert "₦2,500" in result.message


def test_f02_model_customer_id_cannot_change_another_customer(shop, log_path):
    log = ActionLog(log_path)
    context = _ctx(1)
    before_lang = shop.execute("SELECT language FROM customers WHERE id = 2").fetchone()["language"]
    denied = execute_tool(
        shop,
        "set_language",
        {"customer_id": 2, "language": "ha"},
        context=context,
        log=log,
    )
    assert denied["ok"] is False
    assert denied["denied"] is True
    assert shop.execute("SELECT language FROM customers WHERE id = 2").fetchone()["language"] == before_lang
    audits = [entry for entry in log.read_all() if entry["kind"] == "audit"]
    assert audits
    assert audits[-1]["payload"]["customer_id"] == 1
    assert "phone" not in json.dumps(audits[-1]).lower()

    offer = execute_tool(
        shop,
        "create_offer",
        {"customer_id": 2, "product_id": 1, "discount_percent": 10},
        context=context,
        log=log,
    )
    assert offer["ok"] is False
    assert shop.execute("SELECT COUNT(*) AS n FROM offers WHERE customer_id = 2").fetchone()["n"] == 0

    learned = execute_tool(
        shop,
        "learn_phrase",
        {"customer_id": 2, "phrase": "jara", "language": "pcm", "meaning": "extra"},
        context=context,
        log=log,
    )
    assert learned["ok"] is False
    stock = _stock(shop)
    orders = _orders(shop)
    ordered = execute_tool(
        shop,
        "place_order",
        {"customer_id": 2, "product_id": 1, "qty": 1},
        context=context,
        log=log,
    )
    assert ordered["ok"] is False
    assert _stock(shop) == stock
    assert _orders(shop) == orders

    foreign = create_offer(shop, customer_id=2, product_id=1, discount_percent=10)
    confirm_purchase(
        shop, customer_id=1, retailer_id=1, product_id=1, qty=1, offer_id=foreign["offer_id"]
    )
    crossed = execute_tool(
        shop,
        "place_order",
        {"customer_id": 1, "qty": 1, "offer_id": foreign["offer_id"]},
        context=context,
        log=log,
    )
    assert crossed["ok"] is False
    assert _stock(shop) == stock

    kept = execute_tool(
        shop,
        "set_language",
        {"customer_id": 1, "language": "yo"},
        context=context,
        log=log,
    )
    assert kept["ok"] is True
    assert shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"] == "yo"


def test_f02_public_profile_url_is_not_authorization(shop, tmp_path):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    settings = Settings(
        db_path=Path(db_file),
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        profile="public",
    )
    client = TestClient(__import__("convoplex.api", fromlist=["create_app"]).create_app(settings, generate_if_missing=False))
    before = shop.execute("SELECT language FROM customers WHERE id = 2").fetchone()["language"]
    denied = client.post("/v1/customers/2/language", json={"language": "ha"})
    assert denied.status_code == 401
    assert shop.execute("SELECT language FROM customers WHERE id = 2").fetchone()["language"] == before


def test_f03_spaghetti_does_not_become_rice(shop):
    rice = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    shop.execute("UPDATE offers SET status = 'sent' WHERE id = ?", (rice["offer_id"],))
    shop.commit()
    confirm_purchase(shop, customer_id=1, retailer_id=1, product_id=1, qty=1, offer_id=rice["offer_id"])
    before = _orders(shop)
    rice_stock = _stock(shop, 1)
    pasta_stock = _stock(shop, 2)
    wrong = place_order(shop, customer_id=1, qty=1, product="Spaghetti", offer_id=rice["offer_id"])
    assert wrong["ok"] is False
    assert _orders(shop) == before
    assert _stock(shop, 1) == rice_stock
    assert _stock(shop, 2) == pasta_stock

    unnamed = place_order(shop, customer_id=1, qty=1, product="Golden Penny Spaghetti 500g")
    assert unnamed["ok"] is False
    assert "rice" not in unnamed["error"].lower()
    assert _stock(shop, 1) == rice_stock

    shop.execute("UPDATE products SET stock_qty = 4 WHERE id = 2")
    shop.commit()
    instock = place_order(shop, customer_id=1, qty=1, product="Golden Penny Spaghetti 500g")
    assert instock["ok"] is False
    assert instock.get("confirmed") is False
    assert _stock(shop, 2) == 4
    assert _orders(shop) == before


def test_f03_resolution_matrix_and_product_change(shop):
    offer = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    shop.execute("UPDATE offers SET status = 'sent' WHERE id = ?", (offer["offer_id"],))
    shop.commit()
    before = _orders(shop)
    stock = _stock(shop)

    by_name = place_order(shop, customer_id=1, qty=1, product="Mama Gold Rice 50kg")
    assert by_name["ok"] is False and _orders(shop) == before

    confirm_purchase(shop, customer_id=1, retailer_id=1, product_id=1, qty=1, offer_id=offer["offer_id"])
    by_name = place_order(shop, customer_id=1, qty=1, product="Mama Gold Rice 50kg")
    assert by_name["ok"] is True
    assert by_name["product_name"] == "Mama Gold Rice 50kg"
    assert by_name["offer_id"] == offer["offer_id"]

    offer_b = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    confirm_purchase(
        shop, customer_id=1, retailer_id=1, product_id=1, qty=1, offer_id=offer_b["offer_id"]
    )
    by_id = place_order(shop, customer_id=1, qty=1, product_id=1, offer_id=offer_b["offer_id"])
    assert by_id["ok"] is True and by_id["product_id"] == 1

    offer_c = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    confirm_purchase(
        shop, customer_id=1, retailer_id=1, product_id=1, qty=1, offer_id=offer_c["offer_id"]
    )
    by_offer = place_order(shop, customer_id=1, qty=1, offer_id=offer_c["offer_id"])
    assert by_offer["ok"] is True and by_offer["product_name"] == "Mama Gold Rice 50kg"

    bare = create_offer(shop, customer_id=3, product_id=1, discount_percent=10)
    shop.execute("DELETE FROM offers WHERE id = ?", (bare["offer_id"],))
    shop.commit()
    confirm_purchase(shop, customer_id=3, retailer_id=1, product_id=1, qty=1, offer_id=None)
    no_offer = place_order(shop, customer_id=3, qty=1, product_id=1)
    assert no_offer["ok"] is True
    assert no_offer["offer_id"] is None
    assert no_offer["unit_price_ngn"] == 78000

    shop.execute(
        """
        INSERT INTO retailers (name, city, area, category_focus, phone)
        VALUES ('Other Shop', 'Abuja', 'Garki', 'provisions', '+2348000001999')
        """
    )
    shop.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (2, 'RICE-50', 'Foreign Rice', 'provisions', 'bag', 80000, 5)
        """
    )
    shop.commit()
    foreign = shop.execute("SELECT id FROM products WHERE retailer_id = 2").fetchone()["id"]
    crossed = place_order(shop, customer_id=1, qty=1, product_id=foreign)
    assert crossed["ok"] is False
    assert _stock(shop, foreign) == 5

    shop.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (1, 'RICE-25', 'Ofada Rice 5kg', 'provisions', 'bag', 12000, 6)
        """
    )
    shop.commit()
    ambiguous = place_order(shop, customer_id=1, qty=1, product="rice")
    assert ambiguous["ok"] is False
    assert len(ambiguous["candidates"]) >= 2
    orders_after_named = _orders(shop)
    empty = place_order(shop, customer_id=1, qty=1, product="Golden Penny Spaghetti 500g")
    assert empty["ok"] is False
    assert _stock(shop, 2) == 0
    assert _orders(shop) == orders_after_named

    rice_left = _stock(shop, 1)
    confirm_purchase(shop, customer_id=1, retailer_id=1, product_id=1, qty=1, offer_id=None)
    switched = place_order(shop, customer_id=1, qty=1, product_id=2)
    assert switched["ok"] is False
    assert _stock(shop, 1) == rice_left


def test_f04_wrong_amounts_fall_back_and_reconcile(shop, log_path):
    log = ActionLog(log_path)
    draft = draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    lied = handle_reply(
        shop,
        log,
        _Lie(
            "Buy your rice for ₦1 today. Mama Gold Rice 50kg na ₦70,200. "
            "Golden Penny Spaghetti 500g too. Free delivery.\nINTENT: question"
        ),
        1,
        "How much be the rice now?",
        max_tokens=1000,
    )
    assert "₦1" not in lied.message
    assert "₦70,200" in lied.message
    assert "free delivery" not in lied.message.lower()
    assert "Golden Penny Spaghetti" not in lied.message
    assert draft.offer["offer_price_ngn"] == 70200

    profile = {"id": 1, "full_name": "Chinedu Okonkwo", "language": "pcm", "retailer_name": "Shop", "retailer_area": "Ikeja", "area": "Ikeja"}
    payload = commercial_payload(
        profile,
        offer={
            "product_name": "Mama Gold Rice 50kg",
            "offer_price_ngn": 70200,
            "list_price_ngn": 78000,
            "discount_percent": 10,
            "offer_id": draft.offer["offer_id"],
            "qty": 1,
            "stock_qty": 20,
        },
        other_product_names=["Golden Penny Spaghetti 500g"],
    )
    assert claims_mismatch("Mama Gold Rice 50kg na ₦70,200. It was ₦78,000, 10% off.", payload) is False
    assert claims_mismatch("Buy your rice for ₦1 today. Also ₦70,200.", payload) is True
    assert claims_mismatch("That one na one naira.", payload) is True
    assert claims_mismatch("Golden Penny Spaghetti 500g na ₦70,200.", payload) is True
    for language in ("en", "pcm", "ig", "yo", "ha"):
        profile["language"] = language
        text = reconcile_text("Buy am for ₦1. 50% off.", "question", profile, payload)
        assert "₦1" not in text
        assert "₦70,200" in text or "70200" in text.replace(",", "")

    dry = dict(payload)
    dry["in_stock"] = False
    refused = reconcile_text("Buy your rice for ₦1 today.", "question", profile, dry)
    assert refused == out_of_stock_message(profile, "Mama Gold Rice 50kg")
    assert "₦1" not in refused

    card = overlay_copy(
        {
            "retailer_name": "Mama Nkechi Provisions",
            "city": "Lagos",
            "product_name": draft.offer["product_name"],
            "offer_price_ngn": draft.offer["offer_price_ngn"],
            "list_price_ngn": draft.offer["list_price_ngn"],
            "discount_percent": draft.offer["discount_percent"],
            "language": "pcm",
        }
    )
    assert card["price"] == "₦70,200"
    assert card["product"] == draft.offer["product_name"]

    bought = handle_reply(shop, log, MockMuse(), 1, "Yes o, no wahala. Abeg send am today.", max_tokens=1000)
    assert bought.order["total_ngn"] == draft.offer["offer_price_ngn"]
    assert "₦70,200" in bought.message
    assert "₦2,500" in bought.message
    stored = shop.execute(
        "SELECT offer_price_ngn, status FROM offers WHERE id = ?",
        (draft.offer["offer_id"],),
    ).fetchone()
    assert stored["offer_price_ngn"] == 70200
    assert stored["status"] == "accepted"


def test_f05_simulator_webhook_does_not_mutate_and_signed_traffic_does(shop, tmp_path):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    settings = Settings(
        db_path=Path(db_file),
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        admin_token="s3cret",
        channel="simulator",
        whatsapp_demo_to="15550001111",
    )
    from convoplex.api import create_app

    client = TestClient(create_app(settings, generate_if_missing=False))
    orders = _orders(shop)
    actions = client.get("/v1/actions").json()["count"]
    language = shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"]
    unsigned = client.post("/v1/whatsapp/webhook", json=_inbound("wamid.SIM", "15550001111", "Yes o, send am"))
    assert unsigned.status_code == 200
    assert unsigned.json()["processed"] is False
    assert _orders(shop) == orders
    assert client.get("/v1/actions").json()["count"] == actions
    assert shop.execute("SELECT language FROM customers WHERE id = 1").fetchone()["language"] == language

    locked = client.post("/v1/whatsapp/simulate", json=_inbound("wamid.SIM2", "15550001111", "Igbo"))
    assert locked.status_code == 401

    missing = client.post(
        "/v1/whatsapp/simulate",
        json=_inbound("wamid.EMPTY", "", "Yes o"),
        headers={"Authorization": "Bearer s3cret"},
    )
    assert missing.status_code == 200
    assert missing.json()["processed"] is False
    unknown = client.post(
        "/v1/whatsapp/simulate",
        json=_inbound("wamid.WHO", "2348030001999", "Yes o"),
        headers={"Authorization": "Bearer s3cret"},
    )
    assert unknown.json()["processed"] is False
    assert unknown.json()["ignored"] == 1
    assert client.get("/v1/whatsapp/status").json()["demo_customer_id"] is None

    calls: list[httpx.Request] = []
    live = TestClient(_live_app(shop, tmp_path, calls, app_secret=SECRET))
    live.post("/v1/whatsapp/customer", json={"customer_id": 1})
    raw = json.dumps(_inbound("wamid.LIVE1", DEMO_DIGITS, "How much last price?")).encode()
    accepted = live.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert accepted.status_code == 200
    assert accepted.json()["processed"] is True
    status_body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.LIVE1",
                                    "status": "delivered",
                                    "recipient_id": DEMO_DIGITS,
                                    "timestamp": "1",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    encoded = json.dumps(status_body).encode()
    delivered = live.post(
        "/v1/whatsapp/webhook",
        content=encoded,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(encoded)},
    )
    assert delivered.status_code == 200
    assert delivered.json()["statuses"] == 1

    blank = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": "wamid.BLANK", "from": "", "type": "text", "text": {"body": "Yes"}}
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw_blank = json.dumps(blank).encode()
    rejected = live.post(
        "/v1/whatsapp/webhook",
        content=raw_blank,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw_blank)},
    )
    assert rejected.json()["processed"] is False
    assert rejected.json()["ignored"] == 1


def test_f11_profile_channel_and_outbound_kill_switch(shop, tmp_path):
    db_file = Path(shop.execute("PRAGMA database_list").fetchone()["file"])
    first = TestClient(_app(shop, tmp_path))
    second = TestClient(_app(shop, tmp_path))
    switched = first.post("/v1/whatsapp/channel", json={"channel": "whatsapp"})
    assert switched.status_code == 200
    assert second.get("/v1/whatsapp/status").json()["channel"] == "whatsapp"
    cold = TestClient(_app(shop, tmp_path))
    assert cold.get("/v1/whatsapp/status").json()["channel"] == "whatsapp"
    health = cold.get("/health")
    assert health.status_code == 503
    assert "WHATSAPP_APP_SECRET" in health.json()["problems"]
    first.post("/v1/whatsapp/channel", json={"channel": "simulator"})
    assert TestClient(_app(shop, tmp_path)).get("/health").json()["channel"] == "simulator"

    public = Settings(
        db_path=db_file,
        log_path=tmp_path / "public.jsonl",
        output_dir=tmp_path / "output",
        profile="public",
        channel="whatsapp",
    )
    from convoplex.api import create_app

    public_client = TestClient(create_app(public, generate_if_missing=False))
    assert public_client.get("/health").status_code == 503
    assert "ADMIN_TOKEN" in public_client.get("/health").json()["problems"]
    assert public_client.post("/v1/customers/1/winback").status_code == 401
    shop.execute("DELETE FROM app_kv WHERE key = 'channel_mode'")
    shop.commit()

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    paused = Settings(
        db_path=db_file,
        log_path=tmp_path / "paused.jsonl",
        output_dir=tmp_path / "output",
        channel="whatsapp",
        outbound_enabled=False,
        whatsapp_token="token",
        whatsapp_phone_number_id="1150414038145295",
        whatsapp_app_secret="app-secret",
        whatsapp_verify_token="verify-me",
        whatsapp_demo_to="+15550001111",
    )
    app = create_app(paused, generate_if_missing=False)
    app.state.whatsapp_http = httpx.Client(transport=httpx.MockTransport(handler))
    paused_client = TestClient(app)
    blocked = paused_client.post("/v1/customers/1/whatsapp")
    assert blocked.status_code == 409
    assert "CONVOPLEX_OUTBOUND" in blocked.json()["detail"]
    assert "Nothing was sent" in blocked.json()["detail"]
    assert calls == []
    assert paused_client.get("/health").json()["outbound"] == "off"


def test_f13_readme_names_the_supported_runtime():
    readme = (ROOT / "README.md").read_text()
    lowered = readme.lower()
    assert "linux" in lowered
    assert "macos" in lowered
    assert "vercel" in lowered
    assert "windows native is unsupported" in lowered
    assert "python 3.11" in lowered
    assert "fcntl" in lowered
