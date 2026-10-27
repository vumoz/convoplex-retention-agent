"""Muse Voice Transcribe client. Upstream: POST /v1/asr/transcribe (multipart)."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from convoplex.llm import LLMError

ASR_PATH = "/asr/transcribe"
ASR_RATES = (16000, 24000)

MOCK_ACCEPT = "Yes o, no wahala. Abeg send am today."
MOCK_DECLINE = "No thank you, maybe next month"
MOCK_QUESTION = "How much last price?"


class VoiceError(LLMError):
    """The audio cannot be prepared. Distinct from an upstream model failure."""


def ffmpeg_to_wav_command(src: str, dst: str) -> list[str]:
    """WhatsApp voice notes arrive as OGG/Opus. Meta ASR wants mono 16-bit PCM WAV."""
    return ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _missing_ffmpeg_message() -> str:
    command = " ".join(ffmpeg_to_wav_command("in.ogg", "out.wav"))
    return (
        "ffmpeg is not installed, so this voice note cannot be converted to the "
        "mono 16-bit WAV (16 kHz) Muse Voice Transcribe expects. "
        f"Install ffmpeg and the agent will run `{command}`. "
        "Or upload a mono 16-bit WAV at 16 or 24 kHz."
    )


def convert_to_asr_wav(raw: bytes, suffix: str) -> bytes:
    if not ffmpeg_available():
        raise VoiceError(_missing_ffmpeg_message())
    with tempfile.TemporaryDirectory() as folder:
        src = Path(folder) / f"in{suffix}"
        dst = Path(folder) / "out.wav"
        src.write_bytes(raw)
        command = ffmpeg_to_wav_command(str(src), str(dst))
        try:
            completed = subprocess.run(command, check=False, capture_output=True)
        except FileNotFoundError as exc:
            raise VoiceError(_missing_ffmpeg_message()) from exc
        if completed.returncode != 0 or not dst.exists():
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-400:]
            raise VoiceError(
                "ffmpeg could not convert the voice note to mono 16-bit WAV. "
                + (detail or "No ffmpeg error text.")
            )
        return dst.read_bytes()


def ogg_to_wav_bytes(ogg_bytes: bytes) -> bytes:
    return convert_to_asr_wav(ogg_bytes, ".ogg")


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
    """Call Muse Voice Transcribe. Audio must already be mono 16-bit WAV at 16 or 24 kHz."""
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


def audio_kind(raw: bytes, filename: str, content_type: str) -> str:
    name = (filename or "").lower()
    mime = (content_type or "").lower()
    if name.endswith((".ogg", ".oga", ".opus")) or "ogg" in mime or "opus" in mime:
        return "ogg"
    if name.endswith(".wav") or "wav" in mime or raw[:4] == b"RIFF":
        return "wav"
    return ""


def wav_spec(raw: bytes) -> tuple[int, int, int] | None:
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            return handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
    except (wave.Error, EOFError):
        return None


def wav_is_asr_ready(raw: bytes) -> bool:
    spec = wav_spec(raw)
    if spec is None:
        return False
    channels, width, rate = spec
    return channels == 1 and width == 2 and rate in ASR_RATES


def ensure_asr_wav(raw: bytes, filename: str, content_type: str) -> tuple[bytes, str]:
    """Return WAV bytes the ASR endpoint will accept, plus the source container."""
    if not raw:
        raise VoiceError("Audio file is empty.")
    kind = audio_kind(raw, filename, content_type)
    if kind == "ogg":
        return ogg_to_wav_bytes(raw), "ogg"
    if kind == "wav":
        if wav_is_asr_ready(raw):
            return raw, "wav"
        if wav_spec(raw) is None:
            raise VoiceError("This file looks like a WAV but it could not be read.")
        return convert_to_asr_wav(raw, ".wav"), "wav"
    raise VoiceError("Audio must be WAV or OGG.")


def mock_transcript(filename: str) -> str:
    """Deterministic mock line. The file name picks decline, a price question, or accept."""
    stem = Path(filename or "").stem.lower().replace("_", " ").replace("-", " ")
    if any(token in stem for token in ("decline", "no thanks", "not now")):
        return MOCK_DECLINE
    if any(token in stem for token in ("price", "question", "how much")):
        return MOCK_QUESTION
    return MOCK_ACCEPT


@dataclass
class Transcript:
    text: str
    model: str
    latency_ms: int
    source_format: str
    mode: str
    audio_duration_ms: int | None = None


def transcribe_audio(
    raw: bytes,
    *,
    filename: str,
    content_type: str,
    mode: str,
    api_key: str | None,
    base_url: str,
    model: str,
    client: httpx.Client | None = None,
) -> Transcript:
    """Transcribe a voice note. Mock mode never touches the network."""
    wav, source_format = ensure_asr_wav(raw, filename, content_type)
    if mode != "live":
        return Transcript(
            text=mock_transcript(filename),
            model="mock",
            latency_ms=0,
            source_format=source_format,
            mode="mock",
        )
    if not api_key:
        raise LLMError(
            "META_API_KEY is not set. Export it in your shell, or run with CONVOPLEX_LLM=mock."
        )
    owns = client is None
    http = client or httpx.Client(timeout=120.0)
    started = time.perf_counter()
    try:
        payload = transcribe(
            wav,
            api_key=api_key,
            base_url=base_url,
            model=model,
            client=http,
        )
    finally:
        if owns:
            http.close()
    latency_ms = int((time.perf_counter() - started) * 1000)
    text = str(payload.get("transcript") or "").strip()
    if not text:
        raise LLMError("Meta ASR returned an empty transcript.")
    duration = payload.get("audioDurationMs")
    try:
        duration_ms = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    return Transcript(
        text=text,
        model=model,
        latency_ms=latency_ms,
        source_format=source_format,
        mode="live",
        audio_duration_ms=duration_ms,
    )
