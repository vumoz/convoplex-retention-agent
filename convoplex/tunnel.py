"""cloudflared quick-tunnel helper for the local WhatsApp webhook.

The callback path is /v1/whatsapp/webhook. Meta needs a public https URL and
the verify token. This module prints those two values and never prints the
access token.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading

QUICK_TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
CALLBACK_PATH = "/v1/whatsapp/webhook"


def parse_quick_tunnel_url(text: str) -> str | None:
    match = QUICK_TUNNEL.search(text or "")
    return match.group(0) if match else None


def callback_url(base: str) -> str:
    return base.rstrip("/") + CALLBACK_PATH


def cloudflared_hint(port: int) -> str:
    return f"cloudflared tunnel --url http://127.0.0.1:{port}"


def format_webhook_banner(*, callback: str, verify_token: str | None, public: bool) -> str:
    token_line = verify_token if verify_token else "(set WHATSAPP_VERIFY_TOKEN)"
    lines = [
        f"Callback URL: {callback}",
        f"Verify token: {token_line}",
        "In the Meta app webhook, subscribe to the messages field.",
    ]
    if not public:
        lines.append("Meta cannot call 127.0.0.1. Run the cloudflared command and paste the https callback it prints.")
        lines.append(f"Quick tunnel: {cloudflared_hint(_port_from_callback(callback))}")
    return "\n".join(lines)


def _port_from_callback(callback: str) -> int:
    match = re.search(r":(\d+)/", callback)
    if not match:
        return 8000
    return int(match.group(1))


def start_quick_tunnel(port: int, timeout: float = 25.0) -> tuple[subprocess.Popen[str] | None, str | None]:
    """Start `cloudflared tunnel --url` and return the process plus the public origin.

    When cloudflared is not installed, both values are None. The caller still
    prints the local callback and the install hint.
    """
    if shutil.which("cloudflared") is None:
        return None, None
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    found: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            url = parse_quick_tunnel_url(line)
            if url:
                found.append(url)
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if proc.poll() is not None and not found:
        return proc, None
    return proc, found[0] if found else None
