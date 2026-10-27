import io
import json
import struct
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from convoplex.integrations.voice import (
    MOCK_ACCEPT,
    MOCK_DECLINE,
    MOCK_QUESTION,
    VoiceError,
    mock_transcript,
    ogg_to_wav_bytes,
    transcribe_audio,
    wav_is_asr_ready,
)
from tests.test_api import _app


def test_mock_transcript_follows_the_file_name():
    assert mock_transcript("note.wav") == MOCK_ACCEPT
    assert mock_transcript("decline-note.ogg") == MOCK_DECLINE
    assert mock_transcript("how-much.wav") == MOCK_QUESTION
    assert mock_transcript("price_question.wav") == MOCK_QUESTION


def test_ready_wav_rates_are_16k_and_24k():
    assert wav_is_asr_ready(_wav(16000))
    assert wav_is_asr_ready(_wav(24000))
    assert not wav_is_asr_ready(_wav(8000))


def test_missing_ffmpeg_is_a_clear_error(monkeypatch):
    monkeypatch.setattr("convoplex.integrations.voice.ffmpeg_available", lambda: False)
    with pytest.raises(VoiceError, match="ffmpeg is not installed"):
        ogg_to_wav_bytes(b"OggS-not-really")
    with pytest.raises(VoiceError, match="ffmpeg is not installed"):
        transcribe_audio(
            _wav(8000),
            filename="slow.wav",
            content_type="audio/wav",
            mode="mock",
            api_key=None,
            base_url="https://api.meta.ai/v1",
            model="muse-voice-transcribe-1.0",
        )


def test_mock_transcribe_does_not_call_the_network():
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"mock mode called the network: {request.url}")

    spoken = transcribe_audio(
        _wav(16000),
        filename="note.wav",
        content_type="audio/wav",
        mode="mock",
        api_key="should-not-be-sent",
        base_url="https://api.meta.ai/v1",
        model="muse-voice-transcribe-1.0",
        client=httpx.Client(transport=httpx.MockTransport(explode)),
    )
    assert spoken.text == MOCK_ACCEPT
    assert spoken.model == "mock"
    assert spoken.latency_ms == 0
    assert spoken.mode == "mock"


def test_live_transcribe_posts_the_documented_multipart():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = request.read().decode("utf-8", errors="replace")
        return httpx.Response(
            200,
            json={
                "sessionId": "s1",
                "transcript": "How much last price?",
                "audioDurationMs": 900,
                "turns": [],
            },
        )

    spoken = transcribe_audio(
        _wav(24000),
        filename="question.wav",
        content_type="audio/wav",
        mode="live",
        api_key="test-key",
        base_url="https://api.meta.ai/v1",
        model="muse-voice-transcribe-1.0",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["auth"] == "Bearer test-key"
    assert "muse-voice-transcribe-1.0" in seen["body"]
    assert "PUSH_TO_TALK" in seen["body"]
    assert '"audioEncoding":"WAV"' in seen["body"]
    assert spoken.text == "How much last price?"
    assert spoken.model == "muse-voice-transcribe-1.0"
    assert spoken.audio_duration_ms == 900
    assert spoken.latency_ms >= 0


def test_voice_route_mock_reply_flow(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path))
    bare = client.post("/v1/voice/transcribe")
    assert bare.status_code == 422

    wav = _wav(16000)
    alone = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, _request(), "application/json"),
            "audio": ("note.wav", wav, "audio/wav"),
        },
    )
    assert alone.status_code == 200
    body = alone.json()
    assert body["transcript"] == MOCK_ACCEPT
    assert body["model"] == "mock"
    assert body["latency_ms"] == 0
    assert body["llm"] == "mock"
    assert "intent" not in body

    bad = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, "not-json", "application/json"),
            "audio": ("note.wav", wav, "audio/wav"),
        },
    )
    assert bad.status_code == 400

    other = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, _request(), "application/json"),
            "audio": ("clip.mp3", b"not-audio", "audio/mpeg"),
        },
    )
    assert other.status_code == 400
    assert "WAV or OGG" in other.json()["detail"]

    missing = client.post(
        "/v1/voice/transcribe",
        data={"customer_id": "99"},
        files={
            "request": (None, _request(), "application/json"),
            "audio": ("note.wav", wav, "audio/wav"),
        },
    )
    assert missing.status_code == 404

    win = client.post("/v1/customers/1/winback")
    assert win.status_code == 200
    declined = _voice(client, "decline-note.wav", 1)
    assert declined.status_code == 200
    assert declined.json()["transcript"] == MOCK_DECLINE
    assert declined.json()["intent"] == "decline"
    assert declined.json()["order"] is None

    asked = _voice(client, "how-much.wav", 1)
    assert asked.json()["intent"] == "question"
    assert "₦70,200" in asked.json()["message"]

    client.post("/v1/customers/2/winback")
    accepted = _voice(client, "note.wav", 2)
    assert accepted.json()["intent"] == "accept"
    assert accepted.json()["order"]["total_ngn"] == 70200

    logged = client.get("/v1/actions").json()["entries"]
    transcripts = [
        entry["payload"]
        for entry in logged
        if entry["kind"] == "model_call" and entry["payload"].get("task") == "transcribe"
    ]
    assert {item["transcript"] for item in transcripts} >= {MOCK_ACCEPT, MOCK_DECLINE, MOCK_QUESTION}
    assert all(item["model"] == "mock" for item in transcripts)
    assert all("latency_ms" in item for item in transcripts)
    assert client.post("/v1/actions/verify").json()["ok"] is True

    thread = client.get("/v1/customers/1/thread").json()
    voice_turns = [message for message in thread["messages"] if message.get("voice")]
    assert voice_turns
    assert voice_turns[0]["voice"]["model"] == "mock"
    assert voice_turns[0]["text"] == MOCK_DECLINE


def test_voice_route_reports_missing_ffmpeg(shop, tmp_path, monkeypatch):
    monkeypatch.setattr("convoplex.integrations.voice.ffmpeg_available", lambda: False)
    client = TestClient(_app(shop, tmp_path))
    ogg = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, "{}", "application/json"),
            "audio": ("note.ogg", b"ogg-bytes", "audio/ogg"),
        },
    )
    assert ogg.status_code == 400
    detail = ogg.json()["detail"]
    assert "ffmpeg is not installed" in detail
    assert "ffmpeg -y -i in.ogg -ac 1 -ar 16000 -sample_fmt s16 out.wav" in detail


def test_live_voice_route_uses_the_injected_transport(shop, tmp_path):
    app = _app(shop, tmp_path, llm_mode="live", meta_api_key="test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.meta.ai/v1/asr/transcribe"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = request.read().decode("utf-8", errors="replace")
        assert "muse-voice-transcribe-1.0" in body
        assert "PUSH_TO_TALK" in body
        return httpx.Response(200, json={"transcript": "Abeg send am", "audioDurationMs": 400})

    app.state.http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TestClient(app)
    response = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, _request(), "application/json"),
            "audio": ("note.wav", _wav(16000), "audio/wav"),
        },
    )
    assert response.status_code == 200
    assert response.json()["transcript"] == "Abeg send am"
    assert response.json()["model"] == "muse-voice-transcribe-1.0"
    assert response.json()["llm"] == "live"
    logged = client.get("/v1/actions").json()["entries"]
    payload = logged[-1]["payload"]
    assert payload["task"] == "transcribe"
    assert payload["transcript"] == "Abeg send am"
    assert payload["model"] == "muse-voice-transcribe-1.0"
    assert isinstance(payload["latency_ms"], int)


def test_live_voice_without_a_key_does_not_call_out(shop, tmp_path):
    client = TestClient(_app(shop, tmp_path, llm_mode="live"))
    response = client.post(
        "/v1/voice/transcribe",
        files={
            "request": (None, _request(), "application/json"),
            "audio": ("note.wav", _wav(16000), "audio/wav"),
        },
    )
    assert response.status_code == 502
    assert "META_API_KEY" in response.json()["detail"]


def _voice(client: TestClient, filename: str, customer_id: int):
    return client.post(
        "/v1/voice/transcribe",
        data={"customer_id": str(customer_id)},
        files={
            "request": (None, _request(), "application/json"),
            "audio": (filename, _wav(16000), "audio/wav"),
        },
    )


def _request() -> str:
    return json.dumps(
        {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"},
        separators=(",", ":"),
    )


def _wav(rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * max(rate // 100, 16))
    return buffer.getvalue()
