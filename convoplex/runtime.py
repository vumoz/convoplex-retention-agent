"""Channel and profile that every instance reads the same way.

The channel lives in app_kv when an operator switches it, otherwise in
CONVOPLEX_CHANNEL. A process-local assignment is not the source of truth.
"""

from __future__ import annotations

CHANNEL_KEY = "channel_mode"
PROFILES = ("local", "public", "pilot")


def clean_profile(raw: str | None) -> str:
    chosen = (raw or "local").strip().lower().replace("_", "-")
    if chosen in {"public-demo", "demo"}:
        chosen = "public"
    if chosen not in PROFILES:
        raise ValueError("CONVOPLEX_PROFILE must be local, public, or pilot")
    return chosen


def outbound_enabled(raw: str | None) -> bool:
    chosen = (raw if raw is not None else "on").strip().lower()
    return chosen not in {"off", "0", "false", "no", "stop"}


def stored_channel(conn, default: str) -> str:
    row = conn.execute("SELECT value FROM app_kv WHERE key = ?", (CHANNEL_KEY,)).fetchone()
    if row is None:
        return default
    value = str(row["value"] or "").strip().lower()
    if value in {"simulator", "whatsapp"}:
        return value
    return default


def store_channel(conn, channel: str) -> None:
    if channel not in {"simulator", "whatsapp"}:
        raise ValueError("channel must be simulator or whatsapp")
    conn.execute(
        """
        INSERT INTO app_kv (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (CHANNEL_KEY, channel),
    )
    conn.commit()


def readiness_problems(settings, channel: str) -> list[str]:
    """Missing secrets on a public or pilot profile. Local stays open for tests."""
    problems: list[str] = []
    profile = getattr(settings, "profile", "local")
    if profile in {"public", "pilot"} and not settings.admin_token:
        problems.append("ADMIN_TOKEN")
    if channel == "whatsapp" and not settings.whatsapp_app_secret:
        problems.append("WHATSAPP_APP_SECRET")
    return problems
