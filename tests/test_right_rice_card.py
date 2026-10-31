"""The rice picture belongs to this customer, and the overlay is painted once."""

import io
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from convoplex.actionlog import ActionLog
from convoplex.agent import handle_reply
from convoplex.config import Settings
from convoplex.db import connect, ensure_promo_card_key
from convoplex.greeting import picture_query
from convoplex.integrations import images
from convoplex.integrations.images import promo_card_key
from convoplex.llm import Completion
from convoplex.money import naira
from convoplex.tools import create_offer, place_order
from convoplex import whatsapp_flow
from tests.test_api import _app
from tests.test_whatsapp import DEMO, DEMO_DIGITS, PHONE_ID, SECRET, TOKEN, _inbound, _sign


class _TextOnly:
    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content="Cap Rice dey available.\nINTENT: question", finish_reason="stop")


def _settings(shop, tmp_path: Path, **extra) -> Settings:
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    return Settings(
        db_path=Path(db_file),
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        llm_mode=extra.get("llm_mode") or "mock",
        meta_api_key=extra.get("meta_api_key"),
        meta_base_url="https://api.meta.ai/v1",
        image_model="muse-image-1.0",
    )


def _seed_other_rice(shop) -> dict:
    """Cap Rice on this shelf, and a Wuse offer that must not leak across."""
    shop.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (1, 'RICE-25', 'Cap Rice 25kg', 'provisions', 'bag', 45320, 30)
        """
    )
    shop.execute(
        """
        INSERT INTO retailers (name, city, area, category_focus, phone)
        VALUES ('Wuse Fresh Basket', 'Abuja', 'Wuse 2', 'provisions', '+2348000001004')
        """
    )
    shop.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (2, 'RICE-25', 'Cap Rice 25kg', 'provisions', 'bag', 45320, 30)
        """
    )
    shop.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language, archetype)
        VALUES (2, 'Musa Bello', '+2348030001999', 'Abuja', 'Wuse 2', 'en', 'lapsed')
        """
    )
    cap_here = shop.execute(
        "SELECT id FROM products WHERE retailer_id = 1 AND sku = 'RICE-25'"
    ).fetchone()["id"]
    cap_wuse = shop.execute(
        "SELECT id FROM products WHERE retailer_id = 2 AND sku = 'RICE-25'"
    ).fetchone()["id"]
    wuse_customer = shop.execute("SELECT id FROM customers WHERE retailer_id = 2").fetchone()["id"]
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (2, ?, ?, 10, 45320, 40788, 'other shop', 'en', 'sent', '2026-10-30T18:00:00+00:00')
        """,
        (wuse_customer, cap_wuse),
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 2, ?, 10, 45320, 40788, 'other customer', 'en', 'sent', '2026-10-30T18:01:00+00:00')
        """,
        (cap_here,),
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, ?, 10, 45320, 40788, 'wrong open offer', 'pcm', 'sent', '2026-10-30T18:02:00+00:00')
        """,
        (cap_here,),
    )
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (2, 1, ?, 10, 45320, 40788, 'wrong shop on this customer', 'pcm', 'sent', '2026-10-30T18:03:00+00:00')
        """,
        (cap_wuse,),
    )
    shop.commit()
    foreign = {
        int(row["id"])
        for row in shop.execute(
            """
            SELECT id FROM offers
            WHERE product_id IN (?, ?) OR retailer_id = 2 OR customer_id <> 1
            """,
            (cap_here, cap_wuse),
        ).fetchall()
    }
    return {"cap_here": int(cap_here), "foreign_offer_ids": foreign}


def _offer_row(shop, offer_id: int):
    return shop.execute(
        """
        SELECT o.customer_id, o.retailer_id, o.offer_price_ngn, o.list_price_ngn,
               p.name AS product_name, r.name AS shop_name, r.city
        FROM offers o
        JOIN products p ON p.id = o.product_id
        JOIN retailers r ON r.id = o.retailer_id
        WHERE o.id = ?
        """,
        (offer_id,),
    ).fetchone()


def test_picture_query_treats_bare_rice_as_mama_gold_and_keeps_cap_rice():
    assert picture_query("show me the rice picture") == "rice"
    assert picture_query("give me picture of the rice") == "rice"
    assert picture_query("My customer, show me the rice picture") == "rice"
    assert picture_query("show me the Cap Rice picture") == "show me the Cap Rice picture"
    assert picture_query("show me the Ofada rice picture") == "show me the Ofada rice picture"


def test_rice_picture_stays_on_this_customers_mama_gold(shop, log_path):
    seeded = _seed_other_rice(shop)
    phrases = (
        "show me the rice picture",
        "give me picture of the rice",
        "My customer, show me the rice picture",
    )
    for phrase in phrases:
        result = handle_reply(shop, ActionLog(log_path), _TextOnly(), 1, phrase, max_tokens=1000)
        assert result.image_offer_id, phrase
        assert result.image_offer_id not in seeded["foreign_offer_ids"]
        row = _offer_row(shop, result.image_offer_id)
        assert row["product_name"] == "Mama Gold Rice 50kg"
        assert row["shop_name"] == "Mama Nkechi Provisions"
        assert row["city"] == "Lagos"
        assert int(row["customer_id"]) == 1
        assert int(row["retailer_id"]) == 1
        assert int(row["offer_price_ngn"]) == 70200
        assert "Mama Gold" in result.message
        assert naira(70200) in result.message
        assert "Cap Rice" not in result.message
        assert "Wuse" not in result.message
        assert "40,788" not in result.message
        assert "45,320" not in result.message


def test_naming_cap_rice_does_not_swap_in_mama_gold(shop, log_path):
    _seed_other_rice(shop)
    result = handle_reply(
        shop,
        ActionLog(log_path),
        _TextOnly(),
        1,
        "show me the Cap Rice picture",
        max_tokens=1000,
    )
    row = _offer_row(shop, result.image_offer_id)
    assert row["product_name"] == "Cap Rice 25kg"
    assert int(row["customer_id"]) == 1
    assert int(row["retailer_id"]) == 1
    assert row["shop_name"] == "Mama Nkechi Provisions"


def test_create_offer_for_rice_prefers_mama_gold_and_orders_stay_ambiguous(shop):
    _seed_other_rice(shop)
    shop.execute("DELETE FROM offers WHERE customer_id = 1")
    shop.commit()
    offer = create_offer(shop, customer_id=1, discount_percent=10, product="rice")
    assert offer["ok"] is True
    assert offer["product_name"] == "Mama Gold Rice 50kg"
    assert offer["offer_price_ngn"] == 70200
    named = create_offer(shop, customer_id=1, discount_percent=10, product="Cap Rice")
    assert named["product_name"] == "Cap Rice 25kg"
    ambiguous = place_order(shop, customer_id=1, qty=1, product="rice")
    assert ambiguous["ok"] is False
    names = {item["name"] for item in ambiguous["candidates"]}
    assert "Mama Gold Rice 50kg" in names
    assert "Cap Rice 25kg" in names


def _spy_render(monkeypatch):
    calls = []
    real = images.render_promo_card

    def spy(offer, background=None):
        calls.append(background)
        return real(offer, background)

    monkeypatch.setattr(images, "render_promo_card", spy)
    monkeypatch.setattr(whatsapp_flow, "render_promo_card", spy)
    return calls, real


def _mama_offer(shop):
    shop.execute("DELETE FROM offers")
    shop.commit()
    created = create_offer(shop, customer_id=1, discount_percent=10, product="Mama Gold Rice 50kg")
    assert created["ok"] is True
    return int(created["offer_id"])


def test_shipped_rice_photo_is_text_free_and_overlay_runs_once(shop, tmp_path, monkeypatch):
    asset = whatsapp_flow._BUNDLED_RICE.read_bytes()
    photo = Image.open(io.BytesIO(asset)).convert("RGB")
    edge = photo.getpixel((16, 16))
    shop_band = photo.getpixel((100, 80))
    # A finished card draws a gold border and near-white shop text here.
    assert edge[0] < 80 and edge[1] < 140
    assert shop_band[0] < 80
    offer_id = _mama_offer(shop)
    calls, real = _spy_render(monkeypatch)
    image, source = whatsapp_flow.ensure_promo_bytes(_settings(shop, tmp_path), offer_id)
    assert source == "bundled"
    assert calls == [asset]
    loaded = images.load_offer_for_card(connect(Path(shop.execute("PRAGMA database_list").fetchone()["file"])), offer_id)
    once = real(loaded, asset)
    already = real(
        {
            **loaded,
            "retailer_name": "Wuse Fresh Basket",
            "city": "Abuja",
            "product_name": "Cap Rice 25kg",
            "offer_price_ngn": 40788,
            "list_price_ngn": 45320,
        },
        asset,
    )
    assert image == once
    assert image != real(loaded, already)
    assert image != real(loaded, None)
    assert asset != real(loaded, None)


def test_live_muse_is_preferred_and_a_timeout_uses_the_shipped_photo(shop, tmp_path, monkeypatch):
    offer_id = _mama_offer(shop)
    photo = _solid_webp((24, 120, 70))
    calls, real = _spy_render(monkeypatch)

    def generate(prompt, *, api_key, base_url, model, client):
        assert model == "muse-image-1.0"
        assert "No text" in prompt
        return photo

    monkeypatch.setattr(images, "generate_image", generate)
    image, source = whatsapp_flow.ensure_promo_bytes(
        _settings(shop, tmp_path, llm_mode="live", meta_api_key="test-key"),
        offer_id,
    )
    assert source == "muse"
    assert calls == [photo]
    loaded = images.load_offer_for_card(
        connect(Path(shop.execute("PRAGMA database_list").fetchone()["file"])),
        offer_id,
    )
    assert image == real(loaded, photo)

    def timed_out(prompt, *, api_key, base_url, model, client):
        raise httpx.TimeoutException("muse image timed out")

    shop.execute("DELETE FROM promo_cards")
    shop.commit()
    calls.clear()
    monkeypatch.setattr(images, "generate_image", timed_out)
    fallback, fallback_source = whatsapp_flow.ensure_promo_bytes(
        _settings(shop, tmp_path, llm_mode="live", meta_api_key="test-key"),
        offer_id,
    )
    asset = whatsapp_flow._BUNDLED_RICE.read_bytes()
    assert fallback_source == "bundled"
    assert calls == [asset]
    assert fallback == real(loaded, asset)

    def explode(prompt, *, api_key, base_url, model, client):
        raise AssertionError("muse must not be called without a key")

    shop.execute("DELETE FROM promo_cards")
    shop.commit()
    calls.clear()
    monkeypatch.setattr(images, "generate_image", explode)
    missing_key, missing_source = whatsapp_flow.ensure_promo_bytes(
        _settings(shop, tmp_path, llm_mode="live", meta_api_key=None),
        offer_id,
    )
    assert missing_source == "bundled"
    assert calls == [asset]
    assert missing_key == fallback


def test_placeholder_is_last_when_muse_and_the_shipped_photo_are_unavailable(shop, tmp_path, monkeypatch):
    offer_id = _mama_offer(shop)
    calls, real = _spy_render(monkeypatch)
    monkeypatch.setattr(whatsapp_flow, "_bundled_rice_photo", lambda offer: None)

    def timed_out(prompt, *, api_key, base_url, model, client):
        raise httpx.TimeoutException("muse image timed out")

    monkeypatch.setattr(images, "generate_image", timed_out)
    image, source = whatsapp_flow.ensure_promo_bytes(
        _settings(shop, tmp_path, llm_mode="live", meta_api_key="test-key"),
        offer_id,
    )
    assert source == "placeholder"
    assert calls == [None]
    loaded = images.load_offer_for_card(
        connect(Path(shop.execute("PRAGMA database_list").fetchone()["file"])),
        offer_id,
    )
    assert image == real(loaded, None)


def test_stale_cached_cards_are_ignored_until_the_key_matches(shop, tmp_path, monkeypatch):
    offer_id = _mama_offer(shop)
    settings = _settings(shop, tmp_path)
    calls, _real = _spy_render(monkeypatch)
    first, first_source = whatsapp_flow.ensure_promo_bytes(settings, offer_id)
    assert first_source == "bundled"
    assert len(calls) == 1
    calls.clear()
    again, again_source = whatsapp_flow.ensure_promo_bytes(settings, offer_id)
    assert again_source == "cache"
    assert again == first
    assert calls == []

    shop.execute(
        "UPDATE promo_cards SET image = ?, card_key = ? WHERE offer_id = ?",
        (b"BAD-CARD", "1|stale", offer_id),
    )
    shop.commit()
    stale_file = settings.output_dir / "promo" / f"offer-{offer_id}.webp"
    stale_file.write_bytes(b"BAD-FILE")
    rebuilt, rebuilt_source = whatsapp_flow.ensure_promo_bytes(settings, offer_id)
    assert rebuilt_source == "bundled"
    assert rebuilt == first
    assert not rebuilt.startswith(b"BAD")

    shop.execute("UPDATE offers SET offer_price_ngn = 71000 WHERE id = ?", (offer_id,))
    shop.commit()
    changed, changed_source = whatsapp_flow.ensure_promo_bytes(settings, offer_id)
    assert changed_source == "bundled"
    assert changed != first
    db = connect(settings.db_path)
    try:
        row = db.execute("SELECT card_key, offer_price_ngn FROM promo_cards JOIN offers ON offers.id = promo_cards.offer_id WHERE promo_cards.offer_id = ?", (offer_id,)).fetchone()
        offer = images.load_offer_for_card(db, offer_id)
    finally:
        db.close()
    assert row["card_key"] == promo_card_key(offer)
    assert "71000" in row["card_key"]
    assert str(offer["customer_id"]) in row["card_key"]
    assert str(offer["retailer_id"]) in row["card_key"]
    assert offer["product_name"] in row["card_key"]
    assert images.CARD_VERSION in row["card_key"]


def test_old_promo_table_without_a_key_does_not_need_a_manual_delete(tmp_path):
    db_path = tmp_path / "old.sqlite"
    conn = connect(db_path)
    conn.execute(
        """
        CREATE TABLE promo_cards (
            offer_id INTEGER PRIMARY KEY,
            image BLOB NOT NULL,
            media_type TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO promo_cards (offer_id, image, media_type) VALUES (1, ?, 'image/webp')",
        (b"BAD-CARD",),
    )
    conn.commit()
    ensure_promo_card_key(conn, postgres=False, key=f"{db_path}-migrate")
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(promo_cards)").fetchall()}
    assert "card_key" in columns
    from convoplex.integrations.images import read_promo_bytes

    assert read_promo_bytes(conn, tmp_path, 1, "2|anything") is None
    (tmp_path / "promo").mkdir()
    (tmp_path / "promo" / "offer-1.webp").write_bytes(b"BAD-FILE")
    assert read_promo_bytes(conn, tmp_path, 1, "2|anything") is None
    conn.close()


def test_webhook_logs_the_card_source_and_sends_mama_gold(shop, tmp_path):
    _seed_other_rice(shop)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "uploaded-rice"})
        if b'"status"' in request.content and b"read" in request.content:
            return httpx.Response(200, json={"success": True})
        body = json.loads(request.content)
        if body.get("type") == "image":
            return httpx.Response(200, json={"messages": [{"id": "wamid.IMG"}]})
        return httpx.Response(200, json={"messages": [{"id": "wamid.BTN"}]})

    client = TestClient(
        _app(
            shop,
            tmp_path,
            channel="whatsapp",
            whatsapp_token=TOKEN,
            whatsapp_phone_number_id=PHONE_ID,
            whatsapp_verify_token="verify-me",
            whatsapp_app_secret=SECRET,
            whatsapp_demo_to=DEMO,
            whatsapp_http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )
    client.post("/v1/whatsapp/customer", json={"customer_id": 1})
    raw = json.dumps(_inbound("wamid.RICEPIC", DEMO_DIGITS, "give me picture of the rice")).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    captions = []
    for call in calls:
        if not call.content or call.content[:1] != b"{":
            continue
        body = json.loads(call.content)
        if body.get("type") == "image":
            captions.append(body["image"].get("caption") or "")
    assert captions
    assert "Mama Gold" in captions[0]
    assert "Cap Rice" not in captions[0]
    logged = [
        entry["payload"]
        for entry in client.get("/v1/actions").json()["entries"]
        if entry["kind"] == "whatsapp_image" and entry["payload"].get("ok") is True
    ]
    assert logged
    assert logged[-1]["source"] == "bundled"


def _solid_webp(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), colour).save(buffer, format="WEBP")
    return buffer.getvalue()
