import base64
import io
import shutil
import struct
import wave

import httpx
import pytest

from convoplex.integrations.images import generate_image, image_request_body
from convoplex.integrations.voice import ffmpeg_to_wav_command, ogg_to_wav_bytes, transcribe
from convoplex.integrations.whatsapp import build_send_text_request, parse_inbound


def test_ffmpeg_command_matches_the_notes():
    command = ffmpeg_to_wav_command("in.ogg", "out.wav")
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == "in.ogg"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-sample_fmt") + 1] == "s16"
    assert command[-1] == "out.wav"


def test_asr_and_image_clients_match_documented_shapes():
    wav = _tiny_wav()

    def asr(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.meta.ai/v1/asr/transcribe"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = request.read().decode("utf-8", errors="replace")
        assert "muse-voice-transcribe-1.0" in body
        assert "PUSH_TO_TALK" in body
        assert "WAV" in body
        return httpx.Response(
            200,
            json={"sessionId": "s1", "transcript": "abeg send am", "audioDurationMs": 1000, "turns": []},
        )

    transcript = transcribe(
        wav,
        api_key="test-key",
        base_url="https://api.meta.ai/v1",
        model="muse-voice-transcribe-1.0",
        client=httpx.Client(transport=httpx.MockTransport(asr)),
    )
    assert transcript["transcript"] == "abeg send am"

    def images(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.meta.ai/v1/images/generations"
        assert json_body(request) == image_request_body("a promo card", "muse-image-1.0")
        encoded = base64.b64encode(b"RIFF-webp").decode()
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    raw = generate_image(
        "a promo card",
        api_key="test-key",
        base_url="https://api.meta.ai/v1",
        model="muse-image-1.0",
        client=httpx.Client(transport=httpx.MockTransport(images)),
    )
    assert raw == b"RIFF-webp"


def test_whatsapp_parse_and_send_shape():
    messages = parse_inbound(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"id": "1", "from": "2348000000000", "type": "audio", "audio": {"id": "media-9"}}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert messages[0].kind == "audio"
    assert messages[0].media_id == "media-9"
    request = build_send_text_request("PHONE_ID", "2348000000000", "Hello")
    assert request["url"] == "https://graph.facebook.com/v21.0/PHONE_ID/messages"
    assert request["json"]["messaging_product"] == "whatsapp"
    assert request["json"]["text"]["body"] == "Hello"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ogg_conversion_produces_mono_16k_wav(tmp_path):
    wav_path = tmp_path / "in.wav"
    ogg_path = tmp_path / "in.ogg"
    _write_wav(wav_path)
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), "-c:a", "libopus", str(ogg_path)],
        check=True,
        capture_output=True,
    )
    converted = ogg_to_wav_bytes(ogg_path.read_bytes())
    out = tmp_path / "out.wav"
    out.write_bytes(converted)
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000


def _tiny_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * 160)
    return buffer.getvalue()


def _write_wav(path):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<h", 1000) * 800)


def json_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)
