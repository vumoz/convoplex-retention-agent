from fastapi.testclient import TestClient

from convoplex.synthetic.generate import DEFAULT_CUSTOMERS
from tests.test_api import _app


def test_dashboard_page_and_thread(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    assert "Verify chain" in html
    assert "Reset demo" in html
    assert "Naira recovered" in html
    assert "Voice note" in html
    assert "At risk" in html
    assert "Lapsed" in html
    assert "WhatsApp" in html

    missing = client.get("/v1/customers/99/thread")
    assert missing.status_code == 404

    client.post("/v1/customers/1/winback")
    client.post("/v1/customers/1/reply", json={"text": "How much last price?"})
    thread = client.get("/v1/customers/1/thread")
    assert thread.status_code == 200
    body = thread.json()
    assert body["customer"]["full_name"] == "Chinedu Okonkwo"
    assert body["customer"]["reasons"]
    roles = [message["role"] for message in body["messages"]]
    assert roles[0] == "shop"
    assert "customer" in roles
    assert body["offer"]["offer_price_ngn"] == 70200
    assert body["offer"]["promo_url"].endswith("/offer-1.webp")
    assert body["synthetic"] is True


def test_reset_demo_rebuilds_the_synthetic_book(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    client.post("/v1/customers/1/winback")
    assert client.get("/v1/actions").json()["count"] > 0
    reset = client.post("/v1/demo/reset")
    assert reset.status_code == 200
    assert reset.json()["ok"] is True
    assert reset.json()["synthetic"] is True
    assert reset.json()["customers"] == DEFAULT_CUSTOMERS
    assert client.get("/v1/actions").json()["count"] == 0
    assert client.post("/v1/actions/verify").json() == {
        "ok": True,
        "entries": 0,
        "error": None,
        "seq": 0,
    }
    listed = client.get("/v1/customers/at-risk?limit=500")
    assert listed.status_code == 200
    assert listed.json()["customers"]
    retailers = {row["name"] for row in client.get("/v1/retailers").json()["retailers"]}
    assert "Mama Nkechi Provisions" in retailers
    assert "Wuse Fresh Basket" in retailers
