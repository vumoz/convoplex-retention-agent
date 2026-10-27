"""Runtime settings. Secrets come from the environment and are never logged."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_CHAT_MODEL = "muse-spark-1.3"
DEFAULT_IMAGE_MODEL = "muse-image-1.0"
DEFAULT_ASR_MODEL = "muse-voice-transcribe-1.0"
MIN_TOOL_TOKENS = 2500

LLM_MODES = ("auto", "mock", "live")
CHANNELS = ("simulator", "whatsapp")
DEFAULT_GRAPH_VERSION = "v21.0"


def clean_graph_version(raw: str | None) -> str:
    version = (raw or DEFAULT_GRAPH_VERSION).strip()
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("GRAPH_API_VERSION must look like v21.0")
    return version


def clean_channel(raw: str | None) -> str:
    chosen = (raw or "simulator").strip().lower()
    if chosen not in CHANNELS:
        raise ValueError("CONVOPLEX_CHANNEL must be simulator or whatsapp")
    return chosen


def resolve_llm_mode(mode: str | None = None, *, api_key: str | None = None) -> str:
    """auto → live when a key is present, otherwise the deterministic mock."""
    chosen = (mode or os.environ.get("CONVOPLEX_LLM") or "auto").strip().lower()
    if chosen not in LLM_MODES:
        raise ValueError(f"CONVOPLEX_LLM must be one of {', '.join(LLM_MODES)}")
    key = api_key if api_key is not None else os.environ.get("META_API_KEY")
    key = (key or "").strip() or None
    if chosen == "auto":
        return "live" if key else "mock"
    return chosen


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_path: Path
    output_dir: Path = field(default_factory=lambda: ROOT / "output")
    llm_mode: str = "mock"
    meta_base_url: str = DEFAULT_BASE_URL
    chat_model: str = DEFAULT_CHAT_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    asr_model: str = DEFAULT_ASR_MODEL
    max_tokens: int = MIN_TOOL_TOKENS
    meta_api_key: str | None = field(default=None, repr=False)
    whatsapp_verify_token: str | None = field(default=None, repr=False)
    whatsapp_token: str | None = field(default=None, repr=False)
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: str | None = field(default=None, repr=False)
    whatsapp_demo_to: str | None = field(default=None, repr=False)
    graph_api_version: str = DEFAULT_GRAPH_VERSION
    channel: str = "simulator"
    database_url: str | None = field(default=None, repr=False)
    admin_token: str | None = field(default=None, repr=False)
    meta_daily_cap: int = 80
    try_ip_limit: int = 60
    try_window_seconds: int = 3600

    def with_llm_mode(self, mode: str) -> Settings:
        resolved = resolve_llm_mode(mode, api_key=self.meta_api_key)
        return replace(self, llm_mode=resolved)

    def with_channel(self, channel: str) -> Settings:
        return replace(self, channel=clean_channel(channel))

    @staticmethod
    def from_env() -> Settings:
        key = (os.environ.get("META_API_KEY") or "").strip() or None
        mode = resolve_llm_mode(os.environ.get("CONVOPLEX_LLM"), api_key=key)
        db = Path(os.environ.get("CONVOPLEX_DB") or (ROOT / "data" / "convoplex.sqlite"))
        log = Path(os.environ.get("CONVOPLEX_LOG") or (ROOT / "data" / "actions.jsonl"))
        output = Path(os.environ.get("CONVOPLEX_OUTPUT") or (ROOT / "output"))
        if not db.is_absolute():
            db = ROOT / db
        if not log.is_absolute():
            log = ROOT / log
        if not output.is_absolute():
            output = ROOT / output
        try:
            max_tokens = int(os.environ.get("META_MAX_TOKENS") or MIN_TOOL_TOKENS)
        except ValueError as exc:
            raise ValueError("META_MAX_TOKENS must be an integer") from exc
        return Settings(
            db_path=db,
            log_path=log,
            output_dir=output,
            llm_mode=mode,
            meta_base_url=(os.environ.get("META_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            chat_model=os.environ.get("META_CHAT_MODEL") or DEFAULT_CHAT_MODEL,
            image_model=os.environ.get("META_IMAGE_MODEL") or DEFAULT_IMAGE_MODEL,
            asr_model=os.environ.get("META_ASR_MODEL") or DEFAULT_ASR_MODEL,
            max_tokens=max_tokens,
            meta_api_key=key,
            whatsapp_verify_token=(os.environ.get("WHATSAPP_VERIFY_TOKEN") or "").strip() or None,
            whatsapp_token=(
                os.environ.get("WHATSAPP_TOKEN") or os.environ.get("WHATSAPP_ACCESS_TOKEN") or ""
            ).strip()
            or None,
            whatsapp_phone_number_id=(os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or "").strip() or None,
            whatsapp_app_secret=(os.environ.get("WHATSAPP_APP_SECRET") or "").strip() or None,
            whatsapp_demo_to=(os.environ.get("WHATSAPP_DEMO_TO") or "").strip() or None,
            graph_api_version=clean_graph_version(os.environ.get("GRAPH_API_VERSION")),
            channel=clean_channel(os.environ.get("CONVOPLEX_CHANNEL")),
            database_url=(os.environ.get("DATABASE_URL") or "").strip() or None,
            admin_token=(os.environ.get("ADMIN_TOKEN") or "").strip() or None,
            meta_daily_cap=_positive_int("META_DAILY_CAP", 80),
            try_ip_limit=_positive_int("TRY_IP_LIMIT", 60),
            try_window_seconds=_positive_int("TRY_WINDOW_SECONDS", 3600),
        )


def _positive_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value
