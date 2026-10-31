"""Seeing the rice sends a picture, even when the sentence also asks to buy."""

import json

import httpx
from fastapi.testclient import TestClient

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.llm import Completion, MockMuse
from convoplex.messages import wants_product_image
from convoplex.money import naira
from tests.test_whatsapp import DEMO, DEMO_DIGITS, PHONE_ID, SECRET, TOKEN, _app, _inbound, _sign


class _TextOnly:
    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content="Mama Gold rice dey available.\nINTENT: question", finish_reason="stop")


def test_picture_phrases_match_with_punctuation_and_rice():
    for phrase in (
        "show me the rice picture",
        "Show me the rice picture.",
        "show me the rice image",
        "give me picture of the rice",
        "Picture of the rice",
        "Abeg send the rice picture",
        "Let me see the rice",
        "Image",
        "pix of the rice",
        "My customer, show me the rice picture",
    ):
        assert wants_product_image(phrase), phrase


def test_picture_beats_the_text_offer_with_and_without_an_open_offer(shop, log_path):
    phrases = (
        "show me the rice picture",
        "give me picture of the rice",
        "My customer, show me the rice picture",
    )
    for phrase in phrases:
        for primed in (False, True):
            shop.execute("DELETE FROM purchase_states")
            shop.execute("DELETE FROM offers")
            shop.commit()
            log = ActionLog(log_path)
            if primed:
                draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
            result = handle_reply(
                shop, log, _TextOnly(), 1, phrase, max_tokens=1000, from_voice=True
            )
            assert result.image_offer_id, phrase
            assert result.confirm_offer_id == result.image_offer_id
            assert result.order is None
            assert "Mama Gold" in result.message
            assert "₦" in result.message
            assert "available" not in result.message.lower()
            if phrase.lower().startswith("my customer"):
                assert result.message.startswith("My customer")
            price = shop.execute(
                "SELECT offer_price_ngn FROM offers WHERE id = ?",
                (result.image_offer_id,),
            ).fetchone()["offer_price_ngn"]
            assert naira(int(price)) in result.message


def _bodies(calls: list[httpx.Request]) -> tuple[list[str], list[str]]:
    kinds: list[str] = []
    texts: list[str] = []
    for call in calls:
        if call.url.path.endswith("/media"):
            kinds.append("media")
            continue
        if not call.content or call.content[:1] != b"{":
            continue
        body = json.loads(call.content)
        kind = body.get("type")
        if kind:
            kinds.append(kind)
        if kind == "image":
            texts.append(body["image"].get("caption") or "")
        elif kind == "interactive":
            texts.append(body["interactive"]["body"]["text"])
        elif kind == "text":
            texts.append(body["text"]["body"])
    return kinds, texts


def test_webhook_sends_an_image_for_text_and_voice(shop, tmp_path, monkeypatch):
    spoken = {"text": "show me the rice picture"}

    def fake_transcribe(raw, **kwargs):
        from convoplex.integrations.voice import Transcript

        return Transcript(text=spoken["text"], model="mock", latency_ms=0, source_format="wav", mode="mock")

    monkeypatch.setattr("convoplex.whatsapp_flow.transcribe_audio", fake_transcribe)
    monkeypatch.setattr(
        "convoplex.integrations.whatsapp.WhatsAppClient.download_media",
        lambda self, media_id: (b"RIFFfake", "audio/wav"),
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "uploaded-rice"})
        if b'"status"' in request.content and b"read" in request.content:
            return httpx.Response(200, json={"success": True})
        body = json.loads(request.content)
        if body.get("type") == "image":
            return httpx.Response(200, json={"messages": [{"id": "wamid.IMG"}]})
        if body.get("type") == "interactive":
            return httpx.Response(200, json={"messages": [{"id": "wamid.BTN"}]})
        return httpx.Response(200, json={"messages": [{"id": "wamid.TXT"}]})

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

    def post_text(message_id: str, text: str) -> None:
        calls.clear()
        raw = json.dumps(_inbound(message_id, DEMO_DIGITS, text)).encode()
        response = client.post(
            "/v1/whatsapp/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
        )
        assert response.status_code == 200
        kinds, texts = _bodies(calls)
        assert "image" in kinds, (text, kinds, texts)
        assert "media" in kinds
        assert any("Mama Gold" in item and "₦" in item for item in texts)

    def post_voice(message_id: str, text: str) -> None:
        spoken["text"] = text
        calls.clear()
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": DEMO_DIGITS,
                                        "timestamp": "1761523200",
                                        "type": "audio",
                                        "audio": {"id": message_id, "mime_type": "audio/wav"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        raw = json.dumps(payload).encode()
        response = client.post(
            "/v1/whatsapp/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
        )
        assert response.status_code == 200
        kinds, texts = _bodies(calls)
        assert "image" in kinds, (text, kinds, texts)
        assert any("Mama Gold" in item and "₦" in item for item in texts)

    post_text("wamid.PIC1", "show me the rice picture")
    post_voice("wamid.PIC2", "Show me the rice picture.")
    post_text("wamid.PIC3", "My customer, show me the rice picture")
    client.post("/v1/customers/1/winback")
    post_voice("wamid.PIC4", "give me picture of the rice")
    post_text("wamid.PIC5", "Abeg send the rice picture")


def test_a_failed_image_send_is_logged_and_the_text_says_it_is_coming(shop, tmp_path, monkeypatch):
    from convoplex.integrations.whatsapp import WhatsAppError

    def explode(*args, **kwargs):
        raise WhatsAppError("upload failed")

    monkeypatch.setattr("convoplex.whatsapp_flow.deliver", explode)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if b'"status"' in request.content and b"read" in request.content:
            return httpx.Response(200, json={"success": True})
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
    raw = json.dumps(_inbound("wamid.FAILPIC", DEMO_DIGITS, "show me the rice picture")).encode()
    response = client.post(
        "/v1/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(raw)},
    )
    assert response.status_code == 200
    logged = client.get("/v1/actions").json()["entries"]
    images = [entry for entry in logged if entry["kind"] == "whatsapp_image"]
    assert images
    assert any(entry["payload"].get("ok") is False for entry in images)
    bodies = []
    for call in calls:
        if not call.content or call.content[:1] != b"{":
            continue
        body = json.loads(call.content)
        if body.get("type") == "interactive":
            bodies.append(body["interactive"]["body"]["text"])
        elif body.get("type") == "text":
            bodies.append(body["text"]["body"])
    assert bodies
    assert "picture dey come" in bodies[-1].lower()
    assert "Mama Gold" in bodies[-1]
