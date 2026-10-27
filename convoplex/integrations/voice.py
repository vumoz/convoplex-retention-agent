"""Muse Voice Transcribe client. Upstream: POST /v1/asr/transcribe (multipart)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import httpx

from convoplex.llm import LLMError

ASR_PATH = "/asr/transcribe"


def ffmpeg_to_wav_command(src: str, dst: str) -> list[str]:
    """WhatsApp voice notes arrive as OGG/Opus. Meta ASR wants mono 16-bit PCM WAV."""
    return ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst]


def ogg_to_wav_bytes(ogg_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        src = Path(folder) / "in.ogg"
        dst = Path(folder) / "out.wav"
        src.write_bytes(ogg_bytes)
        command = ffmpeg_to_wav_command(str(src), str(dst))
        completed = subprocess.run(command, check=False, capture_output=True)
        if completed.returncode != 0 or not dst.exists():
            detail = completed.stderr.decode("utf-8", errors="replace")[-400:]
            raise LLMError(f"ffmpeg could not convert OGG to WAV: {detail}")
        return dst.read_bytes()


def transcribe_request_fields(wav_bytes: bytes, model: str) -> dict:
    """Multipart parts for POST /v1/asr/transcribe."""
    body = json.dumps(
        {"model": model, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"},
        separators=(",", ":"),
    )
    return {
        "request": ("request", body, "application/json"),
        "audio": ("audio.wav", wav_bytes, "audio/wav"),
    }


def transcribe(
    wav_bytes: bytes,
    *,
    api_key: str,
    base_url: str,
    model: str,
    client: httpx.Client,
) -> dict:
    """Call Muse Voice Transcribe. Audio must already be mono 16-bit WAV at 16 kHz."""
    response = client.post(
        f"{base_url.rstrip('/')}{ASR_PATH}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        files=transcribe_request_fields(wav_bytes, model),
    )
    if response.status_code >= 400:
        raise LLMError(f"Meta ASR returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMError("Meta ASR returned a non-JSON body.") from exc
    if "transcript" not in payload:
        raise LLMError("Meta ASR response is missing transcript.")
    return payload
