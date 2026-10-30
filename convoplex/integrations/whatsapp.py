"""WhatsApp Cloud API client.

Live sends go only to WHATSAPP_DEMO_TO, and only when the channel is whatsapp.
Synthetic customer numbers are never the Graph `to` field. Tests pass an httpx
client with a fake transport; nothing in this module opens a socket by itself
unless a caller asks for a real request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

GRAPH_VERSION = "v21.0"
GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_TEMPLATE = "hello_world"
DEFAULT_TEMPLATE_LANGUAGE = "en_US"
MEDIA_HOSTS = frozenset({"lookaside.fbsbx.com", "graph.facebook.com"})
MAX_MEDIA_BYTES = 20 * 1024 * 1024
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


class WhatsAppError(RuntimeError):
    """Graph rejected a call, or the local request could not be built."""


class SendBlocked(WhatsAppError):
    """Policy refusal. Nothing was sent."""


@dataclass
class InboundMessage:
    message_id: str
    from_phone: str
    kind: str
    text: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    timestamp: str | None = None


@dataclass
class StatusUpdate:
    message_id: str
    status: str
    recipient: str
    timestamp: str | None = None


@dataclass
class SendResult:
    wamid_hash: str
    wamid_tail: str
    to: str
    kind: str


WAMID_TAIL_LEN = 6
_WAMID_RE = re.compile(r"wamid\.[A-Za-z0-9+/_=-]+")


def wamid_hash(wamid: str) -> str:
    """SHA-256 of the Cloud API message id. Safe to store and to match on."""
    return hashlib.sha256(wamid.encode("utf-8")).hexdigest()


def wamid_tail(wamid: str) -> str:
    """Last few characters for display. The recipient number sits at the front."""
    return (wamid or "")[-WAMID_TAIL_LEN:]


def public_wamid(wamid: str) -> dict[str, str]:
    return {"wamid_hash": wamid_hash(wamid), "wamid_tail": wamid_tail(wamid)}


def phone_base64(digits: str) -> str:
    """Unpadded base64 of the phone digits. A wamid embeds this near the front."""
    if not digits:
        return ""
    return base64.b64encode(digits.encode("utf-8")).decode("ascii").rstrip("=")


def redact_wamids(text: str) -> str:
    """Replace a full Cloud API message id with its short tail."""

    def repl(match: re.Match[str]) -> str:
        full = match.group(0)
        if len(full) <= WAMID_TAIL_LEN:
            return "…"
        return "…" + full[-WAMID_TAIL_LEN:]

    return _WAMID_RE.sub(repl, text)


def scrub_wamids(value):
    """Copy JSON-like data with every full message id reduced to its tail."""
    if isinstance(value, str):
        return redact_wamids(value)
    if isinstance(value, list):
        return [scrub_wamids(item) for item in value]
    if isinstance(value, dict):
        return {str(key): scrub_wamids(item) for key, item in value.items()}
    return value


def normalize_phone(value: str | None) -> str:
    """Digits only, so +234 801… and 234801… compare equal."""
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def mask_phone(value: str | None) -> str:
    """Show only the last two digits."""
    digits = normalize_phone(value)
    if len(digits) < 2:
        return "••"
    return "••" + digits[-2:]


def redact(text: str, *secrets: str | None) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def verify_subscription(mode: str | None, token: str | None, challenge: str | None, expected_token: str) -> str | None:
    if mode == "subscribe" and token and expected_token and token == expected_token and challenge:
        return challenge
    return None


def signature_matches(body: bytes, header: str | None, app_secret: str | None) -> bool:
    """True only when X-Hub-Signature-256 matches app_secret.

    A missing secret does not match. Live WhatsApp must set WHATSAPP_APP_SECRET
    and reject unsigned posts. The simulator skips this call when the secret is unset.
    """
    if not app_secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    given = header.split("=", 1)[1].strip().lower()
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if len(given) != len(digest):
        return False
    return hmac.compare_digest(given, digest)


def parse_inbound(payload: dict) -> list[InboundMessage]:
    """Read text and audio messages from a Cloud API webhook body."""
    found: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                kind = str(message.get("type") or "unknown")
                text = None
                media_id = None
                mime_type = None
                if kind == "text":
                    body = message.get("text") or {}
                    if isinstance(body, dict):
                        text = body.get("body")
                elif kind == "audio":
                    audio = message.get("audio") or {}
                    if isinstance(audio, dict):
                        media_id = audio.get("id")
                        mime_type = audio.get("mime_type")
                elif kind == "interactive":
                    interactive = message.get("interactive") or {}
                    if isinstance(interactive, dict):
                        reply = interactive.get("list_reply") or interactive.get("button_reply") or {}
                        if isinstance(reply, dict):
                            text = reply.get("id") or reply.get("title")
                stamp = message.get("timestamp")
                found.append(
                    InboundMessage(
                        message_id=str(message.get("id") or ""),
                        from_phone=str(message.get("from") or ""),
                        kind=kind,
                        text=str(text) if text is not None else None,
                        media_id=str(media_id) if media_id else None,
                        mime_type=str(mime_type) if mime_type else None,
                        timestamp=str(stamp) if stamp else None,
                    )
                )
    return found


def parse_statuses(payload: dict) -> list[StatusUpdate]:
    found: list[StatusUpdate] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for item in value.get("statuses") or []:
                if not isinstance(item, dict):
                    continue
                stamp = item.get("timestamp")
                found.append(
                    StatusUpdate(
                        message_id=str(item.get("id") or ""),
                        status=str(item.get("status") or ""),
                        recipient=str(item.get("recipient_id") or ""),
                        timestamp=str(stamp) if stamp else None,
                    )
                )
    return found


def messages_url(phone_number_id: str, version: str | None = None) -> str:
    return f"{GRAPH_HOST}/{version or GRAPH_VERSION}/{phone_number_id}/messages"


def build_mark_read_request(phone_number_id: str, message_id: str, version: str | None = None) -> dict:
    """Mark an inbound message read and show the typing indicator. Does not call Graph."""
    return {
        "url": messages_url(phone_number_id, version),
        "json": {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        },
    }


def build_send_text_request(phone_number_id: str, to: str, body: str, version: str | None = None) -> dict:
    """JSON body for a session text message. This function does not call Graph."""
    return {
        "url": messages_url(phone_number_id, version),
        "json": {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        },
    }


def build_send_template_request(
    phone_number_id: str,
    to: str,
    *,
    name: str = DEFAULT_TEMPLATE,
    language: str = DEFAULT_TEMPLATE_LANGUAGE,
    version: str | None = None,
) -> dict:
    return {
        "url": messages_url(phone_number_id, version),
        "json": {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {"name": name, "language": {"code": language}},
        },
    }


CONFIRM_YES_ID = "buy_yes"
CONFIRM_NO_ID = "buy_no"
CONFIRM_YES_TITLE = "Yes, I want it"
CONFIRM_NO_TITLE = "No"


def confirm_purchase_interactive(body: str) -> dict:
    """Reply buttons for a one-time purchase confirm. Titles stay within 20 characters."""
    return {
        "type": "button",
        "body": {"text": (body or "")[:1024]},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": CONFIRM_YES_ID, "title": CONFIRM_YES_TITLE}},
                {"type": "reply", "reply": {"id": CONFIRM_NO_ID, "title": CONFIRM_NO_TITLE}},
            ]
        },
    }


def build_send_interactive_request(
    phone_number_id: str,
    to: str,
    interactive: dict,
    version: str | None = None,
) -> dict:
    """JSON body for a session interactive message. This function does not call Graph."""
    return {
        "url": messages_url(phone_number_id, version),
        "json": {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        },
    }


def build_send_image_request(
    phone_number_id: str,
    to: str,
    media_id: str,
    caption: str,
    version: str | None = None,
) -> dict:
    image: dict = {"id": media_id}
    if caption:
        image["caption"] = caption
    return {
        "url": messages_url(phone_number_id, version),
        "json": {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": image,
        },
    }


def build_upload_media_request(phone_number_id: str, version: str | None = None) -> dict:
    return {
        "url": f"{GRAPH_HOST}/{version or GRAPH_VERSION}/{phone_number_id}/media",
        "data": {"messaging_product": "whatsapp", "type": "image/jpeg"},
        "filename": "promo.jpg",
        "content_type": "image/jpeg",
    }


def build_phone_request(phone_number_id: str, version: str | None = None) -> dict:
    return {
        "url": f"{GRAPH_HOST}/{version or GRAPH_VERSION}/{phone_number_id}",
        "params": {"fields": "display_phone_number,verified_name,quality_rating"},
    }


def build_media_metadata_url(media_id: str, version: str | None = None) -> str:
    return f"{GRAPH_HOST}/{version or GRAPH_VERSION}/{media_id}"


def missing_setting_names(
    *,
    token: str | None,
    phone_number_id: str | None,
    verify_token: str | None,
    demo_to: str | None,
) -> list[str]:
    missing: list[str] = []
    if not token:
        missing.append("WHATSAPP_TOKEN")
    if not phone_number_id:
        missing.append("WHATSAPP_PHONE_NUMBER_ID")
    if not verify_token:
        missing.append("WHATSAPP_VERIFY_TOKEN")
    if not normalize_phone(demo_to):
        missing.append("WHATSAPP_DEMO_TO")
    return missing


def require_allowlisted(
    *,
    channel: str,
    demo_to: str | None,
    token: str | None,
    phone_number_id: str | None,
    requested: str | None,
    outbound: bool = True,
) -> str:
    """Return the demo digits, or refuse. The requested number is never used unless it is the demo."""
    if channel == "whatsapp" and not outbound:
        raise SendBlocked(
            "Outbound messaging is paused (CONVOPLEX_OUTBOUND=off). Pending drafts are kept. Nothing was sent."
        )
    if channel != "whatsapp":
        raise SendBlocked(
            "Live sending is off. Set CONVOPLEX_CHANNEL=whatsapp or switch the dashboard to WhatsApp live. "
            "Nothing was sent."
        )
    missing = []
    if not token:
        missing.append("WHATSAPP_TOKEN")
    if not phone_number_id:
        missing.append("WHATSAPP_PHONE_NUMBER_ID")
    demo = normalize_phone(demo_to)
    if not demo:
        missing.append("WHATSAPP_DEMO_TO")
    if missing:
        raise SendBlocked(f"WhatsApp is not configured. Missing: {', '.join(missing)}. Nothing was sent.")
    if requested is not None and normalize_phone(requested) != demo:
        raise SendBlocked(
            "Refusing to send. Messages go only to the allow-listed demo phone. Nothing was sent."
        )
    return demo


def _require_version(version: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+", version or ""):
        raise WhatsAppError("GRAPH_API_VERSION must look like v21.0.")
    return version


def _require_phone_id(phone_number_id: str | None) -> str:
    if not phone_number_id or not re.fullmatch(r"\d{1,32}", phone_number_id):
        raise WhatsAppError("WHATSAPP_PHONE_NUMBER_ID must be digits.")
    return phone_number_id


def _require_media_id(media_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_id or ""):
        raise WhatsAppError("Media id is not valid.")
    return media_id


def _require_media_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in MEDIA_HOSTS:
        raise WhatsAppError("Refusing to download media from an unexpected host.")
    return url


def webp_to_jpeg(raw: bytes) -> bytes:
    """WhatsApp image messages accept JPEG. Promo cards are stored as WEBP."""
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise WhatsAppError("Promo card is not a readable image.") from exc
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def audio_filename(mime_type: str | None, media_id: str) -> str:
    mime = (mime_type or "").lower()
    if "wav" in mime:
        return f"{media_id}.wav"
    if "ogg" in mime or "opus" in mime:
        return f"{media_id}.ogg"
    if "mpeg" in mime or "mp3" in mime:
        return f"{media_id}.mp3"
    return f"{media_id}.ogg"


def default_graph_http() -> httpx.Client:
    return httpx.Client(timeout=60.0, follow_redirects=False)


class WhatsAppClient:
    """Thin Graph caller. `http` is injectable so tests never touch the network."""

    def __init__(self, settings, *, channel: str, http: httpx.Client | None = None):
        self.settings = settings
        self.channel = channel
        self._http = http

    def _secrets(self) -> tuple[str | None, ...]:
        demo = normalize_phone(self.settings.whatsapp_demo_to)
        coded = phone_base64(demo)
        return (
            self.settings.whatsapp_token,
            self.settings.whatsapp_app_secret,
            demo,
            coded,
            coded[:-1] if len(coded) > 8 else None,
        )

    def _destination(self, requested: str | None) -> str:
        return require_allowlisted(
            channel=self.channel,
            demo_to=self.settings.whatsapp_demo_to,
            token=self.settings.whatsapp_token,
            phone_number_id=self.settings.whatsapp_phone_number_id,
            requested=requested,
            outbound=getattr(self.settings, "outbound_enabled", True),
        )

    def _version(self) -> str:
        return _require_version(self.settings.graph_api_version)

    def _phone_id(self) -> str:
        return _require_phone_id(self.settings.whatsapp_phone_number_id)

    def _auth(self) -> dict[str, str]:
        token = self.settings.whatsapp_token or ""
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        owns = self._http is None
        http = self._http or default_graph_http()
        try:
            try:
                response = http.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise WhatsAppError("WhatsApp network error.") from exc
        finally:
            if owns:
                http.close()
        if response.status_code >= 400:
            detail = redact_wamids(redact(response.text[:300], *self._secrets()))
            raise WhatsAppError(f"Graph returned HTTP {response.status_code}: {detail}")
        return response

    def _post_messages(self, spec: dict) -> str:
        response = self._request(
            "POST",
            spec["url"],
            headers={**self._auth(), "Content-Type": "application/json"},
            json=spec["json"],
        )
        try:
            payload = response.json()
            wamid = payload["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WhatsAppError("Graph response is missing messages[0].id.") from exc
        if not wamid:
            raise WhatsAppError("Graph response is missing messages[0].id.")
        return str(wamid)

    def send_interactive(self, to: str, interactive: dict) -> SendResult:
        dest = self._destination(to)
        spec = build_send_interactive_request(self._phone_id(), dest, interactive, self._version())
        return SendResult(**public_wamid(self._post_messages(spec)), to=dest, kind="interactive")

    def mark_read(self, message_id: str) -> None:
        """POST status=read plus a typing indicator. The raw message id is not logged here."""
        message_id = (message_id or "").strip()
        if not message_id:
            return
        if not self.settings.whatsapp_token:
            raise WhatsAppError("WhatsApp is not configured. Missing: WHATSAPP_TOKEN.")
        spec = build_mark_read_request(self._phone_id(), message_id, self._version())
        self._request(
            "POST",
            spec["url"],
            headers={**self._auth(), "Content-Type": "application/json"},
            json=spec["json"],
        )

    def send_text(self, to: str, body: str) -> SendResult:
        dest = self._destination(to)
        text = (body or "").strip()
        if not text:
            raise WhatsAppError("Refusing to send an empty WhatsApp message.")
        spec = build_send_text_request(self._phone_id(), dest, text[:TEXT_LIMIT], self._version())
        return SendResult(**public_wamid(self._post_messages(spec)), to=dest, kind="text")

    def send_template(
        self,
        to: str | None = None,
        *,
        name: str = DEFAULT_TEMPLATE,
        language: str = DEFAULT_TEMPLATE_LANGUAGE,
    ) -> SendResult:
        dest = self._destination(to)
        spec = build_send_template_request(
            self._phone_id(),
            dest,
            name=name,
            language=language,
            version=self._version(),
        )
        return SendResult(**public_wamid(self._post_messages(spec)), to=dest, kind="template")

    def send_image(self, to: str, image_webp: bytes, caption: str) -> SendResult:
        dest = self._destination(to)
        jpeg = webp_to_jpeg(image_webp)
        media_id = self._upload_jpeg(jpeg)
        spec = build_send_image_request(
            self._phone_id(),
            dest,
            media_id,
            (caption or "")[:CAPTION_LIMIT],
            self._version(),
        )
        return SendResult(**public_wamid(self._post_messages(spec)), to=dest, kind="image")

    def _upload_jpeg(self, jpeg: bytes) -> str:
        spec = build_upload_media_request(self._phone_id(), self._version())
        response = self._request(
            "POST",
            spec["url"],
            headers=self._auth(),
            data=spec["data"],
            files={"file": (spec["filename"], jpeg, spec["content_type"])},
        )
        try:
            media_id = response.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WhatsAppError("Graph media upload is missing id.") from exc
        if not media_id:
            raise WhatsAppError("Graph media upload is missing id.")
        return str(media_id)

    def fetch_phone(self) -> dict:
        if not self.settings.whatsapp_token:
            raise WhatsAppError("WhatsApp is not configured. Missing: WHATSAPP_TOKEN.")
        spec = build_phone_request(self._phone_id(), self._version())
        response = self._request("GET", spec["url"], headers=self._auth(), params=spec["params"])
        try:
            payload = response.json()
        except ValueError as exc:
            raise WhatsAppError("Graph phone lookup returned non-JSON.") from exc
        quality = payload.get("quality_rating")
        score = payload.get("quality_score")
        if not quality and isinstance(score, dict):
            quality = score.get("score")
        return {
            "display_phone_number": str(payload.get("display_phone_number") or ""),
            "verified_name": str(payload.get("verified_name") or ""),
            "quality_rating": str(quality or "unknown"),
        }

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        if not self.settings.whatsapp_token:
            raise WhatsAppError("WhatsApp is not configured. Missing: WHATSAPP_TOKEN.")
        media_id = _require_media_id(media_id)
        meta_url = build_media_metadata_url(media_id, self._version())
        meta_response = self._request("GET", meta_url, headers=self._auth())
        try:
            meta = meta_response.json()
        except ValueError as exc:
            raise WhatsAppError("Graph media lookup returned non-JSON.") from exc
        url = _require_media_url(str(meta.get("url") or ""))
        mime = str(meta.get("mime_type") or "application/octet-stream")
        file_response = self._request("GET", url, headers=self._auth(), follow_redirects=True)
        content = file_response.content
        if len(content) > MAX_MEDIA_BYTES:
            raise WhatsAppError("Voice note is larger than 20 MB.")
        if not content:
            raise WhatsAppError("WhatsApp returned an empty voice note.")
        return content, mime


def format_phone_check(info: dict) -> str:
    lines = [f"Display number: {info.get('display_phone_number') or 'unknown'}"]
    if info.get("verified_name"):
        lines.append(f"Verified name: {info['verified_name']}")
    lines.append(f"Quality: {info.get('quality_rating') or 'unknown'}")
    return "\n".join(lines)
