import base64
import io

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from convoplex.integrations.images import image_request_body, overlay_copy, promo_prompt, render_promo_card
from convoplex.money import naira
from tests.test_api import _app

OFFER = {
    "offer_id": 1,
    "customer_id": 1,
    "offer_price_ngn": 70200,
    "list_price_ngn": 78000,
    "discount_percent": 10,
    "product_name": "Mama Gold Rice 50kg",
    "category": "provisions",
    "retailer_name": "Mama Nkechi Provisions",
    "city": "Lagos",
}


def test_overlay_uses_the_offer_numbers_and_the_prompt_forbids_text():
    copy = overlay_copy(OFFER)
    assert copy["shop"] == "Mama Nkechi Provisions"
    assert copy["city"] == "Lagos"
    assert copy["product"] == "Mama Gold Rice 50kg"
    assert copy["price"] == naira(70200)
    assert copy["was"] == naira(78000)
    assert copy["off"] == "10% OFF"
    prompt = promo_prompt(OFFER)
    assert "Mama Nkechi Provisions" in prompt
    assert "Lagos" in prompt
    assert "Mama Gold Rice 50kg" in prompt
    assert "₦70,200" in prompt
    assert "₦78,000" in prompt
    assert "10% off" in prompt
    assert "No text" in prompt
    assert "no digits" in prompt


def test_mock_card_is_a_square_webp_and_changes_when_the_price_changes():
    first = render_promo_card(OFFER, None)
    second = render_promo_card({**OFFER, "offer_price_ngn": 50000, "discount_percent": 15}, None)
    assert first != second
    image = Image.open(io.BytesIO(first))
    assert image.size == (1080, 1080)
    assert image.format == "WEBP"


def test_promo_route_saves_overlays_and_logs(shop, tmp_path):
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"mock mode called the image API: {request.url}")

    app = _app(shop, tmp_path)
    app.state.http_client = httpx.Client(transport=httpx.MockTransport(explode))
    client = TestClient(app)
    missing = client.post("/v1/offers/99/promo-card")
    assert missing.status_code == 404

    win = client.post("/v1/customers/1/winback")
    offer_id = win.json()["offer"]["offer_id"]
    card = client.post(f"/v1/offers/{offer_id}/promo-card")
    assert card.status_code == 200
    body = card.json()
    assert body["model"] == "mock"
    assert body["llm"] == "mock"
    assert body["overlay"]["offer_price_ngn"] == 70200
    assert body["overlay"]["list_price_ngn"] == 78000
    assert body["overlay"]["discount_percent"] == 10
    assert body["overlay"]["price"] == "₦70,200"
    assert body["overlay"]["was"] == "₦78,000"
    assert body["overlay"]["off"] == "10% OFF"
    assert "Mama Gold" in body["prompt"]
    assert "No text" in body["prompt"]
    path = tmp_path / "output" / "promo" / f"offer-{offer_id}.webp"
    assert path.is_file()
    assert body["path"] == str(path)
    assert body["url"] == f"/media/promo/offer-{offer_id}.webp"
    served = client.get(body["url"])
    assert served.status_code == 200
    assert client.head(body["url"]).status_code == 200
    assert served.content[:4] == b"RIFF"
    assert b"WEBP" in served.content[:16]

    logged = [
        entry["payload"]
        for entry in client.get("/v1/actions").json()["entries"]
        if entry["kind"] == "model_call" and entry["payload"].get("task") == "promo_card"
    ]
    assert logged[-1]["model"] == "mock"
    assert logged[-1]["offer_id"] == offer_id
    assert logged[-1]["overlay"]["offer_price_ngn"] == 70200
    assert isinstance(logged[-1]["latency_ms"], int)
    assert client.post("/v1/actions/verify").json()["ok"] is True


def test_live_promo_route_posts_generations_then_overlays(shop, tmp_path):
    app = _app(shop, tmp_path, llm_mode="live", meta_api_key="test-key")
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 10, 78000, 70200, 'test', 'pcm', 'sent', '2026-10-27T09:00:00+00:00')
        """
    )
    shop.commit()
    background = _tiny_webp()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = json_body(request)
        encoded = base64.b64encode(background).decode()
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    app.state.http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TestClient(app)
    response = client.post("/v1/offers/1/promo-card")
    assert response.status_code == 200
    assert seen["url"] == "https://api.meta.ai/v1/images/generations"
    assert seen["auth"] == "Bearer test-key"
    assert seen["json"] == image_request_body(promo_prompt(OFFER), "muse-image-1.0")
    body = response.json()
    assert body["model"] == "muse-image-1.0"
    assert body["llm"] == "live"
    saved = (tmp_path / "output" / "promo" / "offer-1.webp").read_bytes()
    assert saved != background
    image = Image.open(io.BytesIO(saved))
    assert image.size == (1080, 1080)
    assert body["overlay"]["price"] == "₦70,200"


def test_live_promo_without_a_key_does_not_call_out(shop, tmp_path):
    shop.execute(
        """
        INSERT INTO offers (
            retailer_id, customer_id, product_id, discount_percent,
            list_price_ngn, offer_price_ngn, message, language, status, created_at
        ) VALUES (1, 1, 1, 10, 78000, 70200, 'test', 'pcm', 'sent', '2026-10-27T09:00:00+00:00')
        """
    )
    shop.commit()
    client = TestClient(_app(shop, tmp_path, llm_mode="live"))
    response = client.post("/v1/offers/1/promo-card")
    assert response.status_code == 502
    assert "META_API_KEY" in response.json()["detail"]


def _tiny_webp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (20, 90, 60)).save(buffer, format="WEBP")
    return buffer.getvalue()


def json_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)
