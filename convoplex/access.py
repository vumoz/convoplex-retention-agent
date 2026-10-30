"""Public-demo guards: admin token, per-IP try-it limit, daily Meta cap.

When ADMIN_TOKEN is set, every shop write requires it. Unset ADMIN_TOKEN
leaves local runs and the test suite open. A public or pilot profile
(CONVOPLEX_PROFILE) refuses mutations when the token is missing. The
WhatsApp webhook is not gated here; signature checks stay on the route.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from convoplex.db import open_db

LOGGER = logging.getLogger("convoplex.http")
ADMIN_COOKIE = "convoplex_admin"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # The dashboard is one HTML file with inline script and style.
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'self'; form-action 'self'"
    ),
}


class PromoCapError(Exception):
    """The public daily cap is spent and this call would draw an image."""


def configure_access_log() -> None:
    if LOGGER.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def access_record(*, method: str, path: str, status: int) -> dict:
    """One request line. Path only: query strings can carry hub.verify_token."""
    return {"msg": "request", "method": method, "path": path, "status": status}


def log_access(request: Request, status: int) -> None:
    LOGGER.info(json.dumps(access_record(method=request.method, path=request.url.path, status=status)))


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def presented_admin_token(request: Request) -> str:
    header = (request.headers.get("x-admin-token") or "").strip()
    if header:
        return header
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (request.cookies.get(ADMIN_COOKIE) or "").strip()


def token_matches(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    try:
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def is_admin(request: Request, settings) -> bool:
    """True only when ADMIN_TOKEN is set and this request presented it.

    An unset token leaves the admin routes open (see require_admin) but does not
    bypass the daily Meta cap. Otherwise a public deploy that forgot the token
    would spend the model budget without a limit.
    """
    expected = settings.admin_token
    if not expected:
        return False
    return token_matches(presented_admin_token(request), expected)


def require_admin(request: Request, settings) -> None:
    """Local with no token stays open. Public and pilot never do.

    A public or pilot profile with ADMIN_TOKEN unset refuses every mutation
    instead of treating the missing secret as permission.
    """
    profile = getattr(settings, "profile", "local")
    if profile in {"public", "pilot"} and not settings.admin_token:
        raise HTTPException(401, "admin token required")
    if not settings.admin_token:
        return
    if is_admin(request, settings):
        return
    raise HTTPException(401, "admin token required")


# Postgres rejects an unqualified `hits` here: the SET expression can see both
# the existing row and EXCLUDED, so `hits = hits + 1` is ambiguous. SQLite
# accepted it. Qualify the existing row. The other ON CONFLICT updates in this
# repo already use `excluded.<column>`.
USAGE_UPSERT_SQL = """
INSERT INTO usage_counters (bucket, hits) VALUES (?, 1)
ON CONFLICT(bucket) DO UPDATE SET hits = usage_counters.hits + 1
WHERE usage_counters.hits < ?
"""


def consume_bucket(conn, bucket: str, limit: int) -> bool:
    """Increment a counter if it is still under the limit. False when it is not."""
    if limit <= 0:
        return False
    cursor = conn.execute(USAGE_UPSERT_SQL, (bucket, limit))
    ok = cursor.rowcount > 0
    conn.commit()
    return ok


def meta_bucket(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return "meta:" + moment.strftime("%Y-%m-%d")


def _log_counter_failure(exc: Exception) -> None:
    """Record a counter failure without the database URL or a phone number."""
    try:
        configure_access_log()
        text = re.sub(r"(?i)postgres(?:ql)?://\S+", "postgres://redacted", str(exc))
        text = " ".join(text.split())[:300]
        LOGGER.warning(json.dumps({"msg": "usage_counter_failed", "error": text}))
    except Exception:
        return


def select_mode(
    settings,
    *,
    admin: bool,
    task: str,
    now: datetime | None = None,
    fail_open: bool = False,
) -> str:
    """live, or mock when the daily cap is spent. Promo raises instead of drawing a stand-in.

    fail_open is the WhatsApp webhook path: a broken counter is logged and the
    reply still goes out. A spent cap is not a broken counter.
    """
    if settings.llm_mode != "live":
        return "mock"
    if admin:
        return "live"
    try:
        conn = open_db(settings)
    except Exception as exc:
        if not fail_open:
            raise
        _log_counter_failure(exc)
        return "live"
    try:
        allowed = consume_bucket(conn, meta_bucket(now), settings.meta_daily_cap)
    except PromoCapError:
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if not fail_open:
            raise
        _log_counter_failure(exc)
        return "live"
    finally:
        conn.close()
    if allowed:
        return "live"
    if task == "promo":
        raise PromoCapError(
            "Daily image cap reached. An admin can still generate a card."
        )
    return "mock"


def enforce_try_limit(request: Request, settings, now: float | None = None) -> None:
    limit = settings.try_ip_limit
    window = max(int(settings.try_window_seconds), 1)
    slot = int((now if now is not None else time.time()) // window)
    bucket = f"ip:{client_ip(request)}:{slot}"
    conn = open_db(settings)
    try:
        allowed = consume_bucket(conn, bucket, limit)
    finally:
        conn.close()
    if not allowed:
        raise HTTPException(429, "Try-it limit reached for this network. Wait and try again.")


class CappedLLM:
    """Webhook model calls: live until the daily cap, then the mock.

    A failure in the counter is logged and the live model still runs. The
    reply is not abandoned because the cap table could not be updated.
    """

    def __init__(self, settings, live, mock):
        self.settings = settings
        self.live = live
        self.mock = mock

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        chosen = self.mock
        if self.settings.llm_mode == "live" and self.live is not None:
            mode = select_mode(self.settings, admin=False, task="chat", fail_open=True)
            if mode == "live":
                chosen = self.live
        return chosen.complete(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            retry_on_length=retry_on_length,
        )
