"""WhatsApp Cloud API shapes. Sending is not wired in this build.

Test recipients, when a later day turns sending on, must be the builder's own numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

GRAPH_VERSION = "v21.0"


@dataclass
class InboundMessage:
    message_id: str
    from_phone: str
    kind: str
    text: str | None = None
    media_id: str | None = None


def verify_subscription(mode: str | None, token: str | None, challenge: str | None, expected_token: str) -> str | None:
    if mode == "subscribe" and token and expected_token and token == expected_token and challenge:
        return challenge
    return None


def parse_inbound(payload: dict) -> list[InboundMessage]:
    """Read the Cloud API webhook body. Text and audio messages are recognised; nothing is sent."""
    found: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                kind = message.get("type") or "unknown"
                text = None
                media_id = None
                if kind == "text":
                    text = (message.get("text") or {}).get("body")
                elif kind == "audio":
                    media_id = (message.get("audio") or {}).get("id")
                found.append(
                    InboundMessage(
                        message_id=str(message.get("id") or ""),
                        from_phone=str(message.get("from") or ""),
                        kind=kind,
                        text=text,
                        media_id=media_id,
                    )
                )
    return found


def build_send_text_request(phone_number_id: str, to: str, body: str) -> dict:
    """Request body for a later day. This function does not perform the HTTP call."""
    return {
        "url": f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages",
        "json": {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        },
    }
