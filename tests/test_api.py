from pathlib import Path

from fastapi.testclient import TestClient

from convoplex.api import create_app
from convoplex.config import Settings


def _app(shop, tmp_path, **extra):
    db_file = shop.execute("PRAGMA database_list").fetchone()["file"]
    settings = Settings(
        db_path=Path(db_file),
        log_path=tmp_path / "actions.jsonl",
        output_dir=tmp_path / "output",
        llm_mode=extra.get("llm_mode") or "mock",
        whatsapp_verify_token=extra.get("whatsapp_verify_token"),
        whatsapp_token=extra.get("whatsapp_token"),
        whatsapp_phone_number_id=extra.get("whatsapp_phone_number_id"),
        whatsapp_app_secret=extra.get("whatsapp_app_secret"),
        whatsapp_demo_to=extra.get("whatsapp_demo_to"),
        graph_api_version=extra.get("graph_api_version") or "v21.0",
        channel=extra.get("channel") or "simulator",
        meta_api_key=extra.get("meta_api_key"),
        meta_base_url=extra.get("meta_base_url") or "https://api.meta.ai/v1",
    )
    app = create_app(settings, generate_if_missing=False)
    if "whatsapp_http" in extra:
        app.state.whatsapp_http = extra["whatsapp_http"]
    if "http_client" in extra:
        app.state.http_client = extra["http_client"]
    return app


def test_at_risk_winback_reply_and_metrics(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["synthetic"] is True
    assert health.json()["llm_mode"] == "mock"
    assert "SYNTHETIC" in health.json()["label"]

    listed = client.get("/v1/customers/at-risk")
    assert listed.status_code == 200
    ids = {row["id"] for row in listed.json()["customers"]}
    assert ids == {1, 2}
    chinedu = next(row for row in listed.json()["customers"] if row["id"] == 1)
    assert chinedu["segment"] == "lapsed"
    assert chinedu["reasons"]

    missing = client.get("/v1/customers/99")
    assert missing.status_code == 404

    before = client.get("/v1/metrics").json()
    assert before["offers_sent"] == 0
    assert before["naira_recovered"] == 0

    win = client.post("/v1/customers/1/winback")
    assert win.status_code == 200
    body = win.json()
    assert body["llm"] == "mock"
    assert body["language"] == "pcm"
    assert "₦" in body["message"]
    assert body["offer"]["offer_price_ngn"] == 70200
    assert body["tool_calls"] == ["check_stock", "get_price", "create_offer"]

    reply = client.post("/v1/customers/1/reply", json={"text": "Yes o, no wahala. Abeg send am today."})
    assert reply.status_code == 200
    assert reply.json()["intent"] == "accept"
    assert reply.json()["order"]["total_ngn"] == 70200

    client.post("/v1/customers/2/winback")
    declined = client.post("/v1/customers/2/reply", json={"text": "No thank you, maybe next month"})
    assert declined.json()["intent"] == "decline"
    assert declined.json()["order"] is None

    metrics = client.get("/v1/metrics").json()
    assert metrics["offers_sent"] == 2
    assert metrics["offers_accepted"] == 1
    assert metrics["win_back_rate"] == 0.5
    assert metrics["naira_recovered"] == 70200
    assert metrics["currency"] == "NGN"
    assert metrics["model_calls"] >= 1
    assert metrics["prompt_tokens"] == 0
    assert metrics["completion_tokens"] == 0
    assert metrics["reasoning_tokens"] == 0
    assert metrics["total_tokens"] == 0
    assert metrics["model_latency_ms"] == 0
    logged = client.get("/v1/actions").json()["entries"]
    assert any(entry["kind"] == "model_call" and "usage" in entry["payload"] for entry in logged)

    verified = client.post("/v1/actions/verify")
    assert verified.json()["ok"] is True
    assert verified.json()["entries"] > 0
    actions = client.get("/v1/actions")
    assert actions.json()["count"] == verified.json()["entries"]
    assert client.post("/v1/customers/1/reply", json={"text": ""}).status_code == 422


def test_usage_totals_sum_logged_model_calls():
    from convoplex.metrics import usage_totals

    totals = usage_totals(
        [
            {"kind": "message", "payload": {}},
            {
                "kind": "model_call",
                "payload": {
                    "latency_ms": 1200,
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 1280,
                        "reasoning_tokens": 1221,
                        "total_tokens": 1290,
                    },
                },
            },
            {
                "kind": "model_call",
                "payload": {
                    "latency_ms": 30,
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 8,
                        "reasoning_tokens": 0,
                        "total_tokens": 12,
                    },
                },
            },
        ]
    )
    assert totals == {
        "model_calls": 2,
        "prompt_tokens": 14,
        "completion_tokens": 1288,
        "reasoning_tokens": 1221,
        "total_tokens": 1302,
        "model_latency_ms": 1230,
    }


def test_whatsapp_webhook_verifies_and_ignores_other_numbers(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path, whatsapp_verify_token="hackathon-token"))
    challenge = client.get(
        "/v1/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "hackathon-token", "hub.challenge": "12345"},
    )
    assert challenge.status_code == 200
    assert challenge.text == "12345"
    forbidden = client.get(
        "/v1/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"},
    )
    assert forbidden.status_code == 403

    inbound = client.post(
        "/v1/whatsapp/webhook",
        json={
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "from": "2348030001101",
                                        "type": "text",
                                        "text": {"body": "Yes o"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )
    assert inbound.status_code == 200
    payload = inbound.json()
    assert payload["status"] == "ok"
    assert payload["processed"] is False
    assert payload["ignored"] == 1
    assert "2348030001101" not in inbound.text

    unconfigured = TestClient(_app(shop, tmp_path))
    assert unconfigured.get("/v1/whatsapp/webhook").status_code == 501
