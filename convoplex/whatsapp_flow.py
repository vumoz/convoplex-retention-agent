"""Route WhatsApp traffic through the retention agent.

Inbound messages from the demo phone are mapped to the selected synthetic
customer. Outbound Graph calls always use WHATSAPP_DEMO_TO. The fictional
customer phone is logged as an id, never dialled.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import httpx

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import get_customer
from convoplex.db import open_db
from convoplex.integrations.images import (
    build_promo_image,
    load_offer_for_card,
    promo_filename,
    read_promo_bytes,
    remember_promo,
)
from convoplex.integrations.voice import VoiceError, transcribe_audio
from convoplex.integrations.whatsapp import (
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_LANGUAGE,
    InboundMessage,
    SendBlocked,
    StatusUpdate,
    WhatsAppClient,
    WhatsAppError,
    audio_filename,
    mask_phone,
    missing_setting_names,
    normalize_phone,
    phone_base64,
    public_wamid,
    redact,
    redact_wamids,
    require_allowlisted,
)
from convoplex.languages import (
    CONFIRM,
    interpret_language,
    language_list_interactive,
    save_language,
    take_language_hint,
)
from convoplex.llm import LLMError, LiveMuse, MockMuse
from convoplex.money import naira
from convoplex.whatsapp_state import WhatsAppStore, store_path, window_open

FALLBACK_CUSTOMER_ID = 184


def store_for(settings: Settings) -> WhatsAppStore:
    if settings.database_url:
        return WhatsAppStore(
            store_path(settings),
            connect_fn=lambda: open_db(settings),
            postgres=True,
        )
    return WhatsAppStore(store_path(settings))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def status_view(settings: Settings, channel: str, store: WhatsAppStore, now: datetime | None = None) -> dict:
    moment = now or now_utc()
    state = store.load()
    inbound = state.get("last_inbound_at")
    if not isinstance(inbound, str):
        inbound = None
    open_now = window_open(inbound, moment)
    missing = missing_setting_names(
        token=settings.whatsapp_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        verify_token=settings.whatsapp_verify_token,
        demo_to=settings.whatsapp_demo_to,
    )
    if channel == "whatsapp" and not settings.whatsapp_app_secret:
        missing.append("WHATSAPP_APP_SECRET")
    customer_id = state.get("demo_customer_id")
    try:
        customer_id = int(customer_id) if customer_id is not None else None
    except (TypeError, ValueError):
        customer_id = None
    masked = mask_phone(settings.whatsapp_demo_to) if settings.whatsapp_demo_to else None
    return {
        "channel": channel,
        "configured": not missing,
        "can_send": _can_send(settings),
        "missing": missing,
        "window": "open" if open_now else "closed",
        "window_open": open_now,
        "last_inbound_at": inbound,
        "last_status": state.get("last_status"),
        "demo_to_masked": masked,
        "demo_customer_id": customer_id,
        "graph_version": settings.graph_api_version,
        "callback_path": "/v1/whatsapp/webhook",
        "signature_required": channel == "whatsapp" or bool(settings.whatsapp_app_secret),
        "synthetic": True,
    }


def _can_send(settings: Settings) -> bool:
    return bool(
        settings.whatsapp_token
        and settings.whatsapp_phone_number_id
        and normalize_phone(settings.whatsapp_demo_to)
    )


def window_closed_note(kind: str) -> str:
    held = "the promo card" if kind == "image" else "this text"
    return (
        "The 24-hour customer service window is closed. "
        "WhatsApp only accepts an approved template outside that window, "
        f"so Convoplex sent {DEFAULT_TEMPLATE} ({DEFAULT_TEMPLATE_LANGUAGE}) instead of {held}. "
        "Reply from the demo phone, then send again."
    )


def safe_append(log: ActionLog, settings: Settings, *, kind: str, actor: str, payload: dict) -> dict:
    """Write an audit line after refusing to persist secrets or the demo number."""
    blob = json.dumps(payload, ensure_ascii=False)
    token = settings.whatsapp_token or ""
    secret = settings.whatsapp_app_secret or ""
    verify = settings.whatsapp_verify_token or ""
    demo = normalize_phone(settings.whatsapp_demo_to)
    if token and token in blob:
        raise RuntimeError("refusing to log the WhatsApp token")
    if secret and secret in blob:
        raise RuntimeError("refusing to log the WhatsApp app secret")
    if verify and verify in blob:
        raise RuntimeError("refusing to log the verify token")
    if demo and len(demo) >= 6 and demo in blob:
        raise RuntimeError("refusing to log the demo phone number")
    coded = phone_base64(demo) if demo else ""
    if coded and (coded in blob or (len(coded) > 8 and coded[:-1] in blob)):
        raise RuntimeError("refusing to log the demo phone number")
    if "wamid." in blob:
        raise RuntimeError("refusing to log a WhatsApp message id")
    return log.append(kind=kind, actor=actor, payload=payload)


def _masked_demo(settings: Settings) -> str:
    return mask_phone(settings.whatsapp_demo_to)


def resolve_demo_customer(store: WhatsAppStore, conn) -> int | None:
    chosen = store.demo_customer_id()
    if chosen is not None and get_customer(conn, chosen) is not None:
        return chosen
    if get_customer(conn, FALLBACK_CUSTOMER_ID) is not None:
        store.set_demo_customer(FALLBACK_CUSTOMER_ID)
        return FALLBACK_CUSTOMER_ID
    return None


def set_demo_customer(settings: Settings, customer_id: int) -> dict:
    conn = open_db(settings)
    try:
        if get_customer(conn, customer_id) is None:
            raise LookupError(f"Unknown customer {customer_id}")
    finally:
        conn.close()
    store_for(settings).set_demo_customer(customer_id)
    return {"customer_id": customer_id, "synthetic": True}


def record_statuses(
    settings: Settings,
    log: ActionLog,
    store: WhatsAppStore,
    statuses: list[StatusUpdate],
) -> int:
    demo = normalize_phone(settings.whatsapp_demo_to)
    applied = 0
    for item in statuses:
        if not item.message_id or item.status not in {"sent", "delivered", "read", "failed"}:
            continue
        recipient = normalize_phone(item.recipient)
        if recipient and demo and recipient != demo:
            continue
        if recipient and not demo:
            continue
        public = public_wamid(item.message_id)
        status = store.set_status(public["wamid_hash"], item.status)
        payload: dict = {
            "wamid_hash": public["wamid_hash"],
            "wamid_tail": public["wamid_tail"],
            "status": status,
        }
        if item.recipient:
            payload["to_masked"] = mask_phone(item.recipient)
        customer_id = _customer_for_wamid(log, public["wamid_hash"])
        if customer_id is not None:
            payload["customer_id"] = customer_id
        safe_append(log, settings, kind="whatsapp_status", actor="whatsapp", payload=payload)
        applied += 1
    return applied


def _customer_for_wamid(log: ActionLog, wamid_hash: str) -> int | None:
    found = None
    for entry in log.read_all():
        payload = entry.get("payload") or {}
        if entry.get("kind") != "whatsapp_send":
            continue
        if payload.get("wamid_hash") != wamid_hash:
            continue
        customer_id = payload.get("customer_id")
        if customer_id is not None:
            found = int(customer_id)
    return found


def _latest_winback(log: ActionLog, customer_id: int) -> tuple[int, str] | None:
    found = None
    for entry in log.read_all():
        payload = entry.get("payload") or {}
        if entry.get("kind") != "message" or entry.get("actor") != "agent":
            continue
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        if payload.get("task") != "winback":
            continue
        content = payload.get("content") or ""
        if content:
            found = (int(entry["seq"]), content)
    return found


def remember_send(
    settings: Settings,
    log: ActionLog,
    store: WhatsAppStore,
    *,
    customer_id: int | None,
    message_seq: int | None,
    wamid_hash: str,
    wamid_tail: str,
    kind: str,
    note: str,
    caption: str | None,
) -> None:
    store.set_status(wamid_hash, "sent")
    payload: dict = {
        "wamid_hash": wamid_hash,
        "wamid_tail": wamid_tail,
        "kind": kind,
        "to_masked": _masked_demo(settings),
        "note": note,
        "status": "sent",
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    if message_seq is not None:
        payload["message_seq"] = message_seq
    if caption:
        payload["caption"] = caption
    safe_append(log, settings, kind="whatsapp_send", actor="whatsapp", payload=payload)


def deliver(
    settings: Settings,
    channel: str,
    log: ActionLog,
    store: WhatsAppStore,
    http: httpx.Client | None,
    *,
    customer_id: int | None,
    message_seq: int | None,
    kind: str,
    text: str,
    image: bytes | None = None,
    caption: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Send free-form text or an image when the window is open, otherwise the template."""
    moment = now or now_utc()
    client = WhatsAppClient(settings, channel=channel, http=http)
    demo = settings.whatsapp_demo_to or ""
    open_now = window_open(store.load().get("last_inbound_at"), moment)
    if kind == "image" and open_now:
        if not image:
            raise WhatsAppError("Generate the promo card before sending it on WhatsApp.")
        result = client.send_image(demo, image, caption or "")
        remember_send(
            settings,
            log,
            store,
            customer_id=customer_id,
            message_seq=message_seq,
            wamid_hash=result.wamid_hash,
            wamid_tail=result.wamid_tail,
            kind="image",
            note="",
            caption=caption,
        )
        return _delivery_body(
            customer_id, "image", result.wamid_hash, result.wamid_tail, settings, "open", ""
        )
    if kind == "text" and open_now:
        result = client.send_text(demo, text)
        remember_send(
            settings,
            log,
            store,
            customer_id=customer_id,
            message_seq=message_seq,
            wamid_hash=result.wamid_hash,
            wamid_tail=result.wamid_tail,
            kind="text",
            note="",
            caption=None,
        )
        return _delivery_body(
            customer_id, "text", result.wamid_hash, result.wamid_tail, settings, "open", ""
        )
    result = client.send_template(demo)
    note = window_closed_note(kind if kind in {"text", "image"} else "text")
    remember_send(
        settings,
        log,
        store,
        customer_id=customer_id,
        message_seq=message_seq,
        wamid_hash=result.wamid_hash,
        wamid_tail=result.wamid_tail,
        kind="template",
        note=note,
        caption=None,
    )
    return _delivery_body(
        customer_id, "template", result.wamid_hash, result.wamid_tail, settings, "closed", note
    )


def _delivery_body(
    customer_id: int | None,
    sent: str,
    wamid_hash: str,
    wamid_tail: str,
    settings: Settings,
    window: str,
    note: str,
) -> dict:
    return {
        "customer_id": customer_id,
        "sent": sent,
        "wamid_hash": wamid_hash,
        "wamid_tail": wamid_tail,
        "to_masked": _masked_demo(settings),
        "window": window,
        "note": note,
        "template": DEFAULT_TEMPLATE if sent == "template" else None,
        "synthetic": True,
    }


def _ensure_live(settings: Settings, channel: str) -> None:
    require_allowlisted(
        channel=channel,
        demo_to=settings.whatsapp_demo_to,
        token=settings.whatsapp_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        requested=settings.whatsapp_demo_to,
    )


def send_winback(
    settings: Settings,
    channel: str,
    log: ActionLog,
    llm,
    store: WhatsAppStore,
    http: httpx.Client | None,
    customer_id: int,
    *,
    max_tokens: int,
    language: str | None = None,
) -> dict:
    _ensure_live(settings, channel)
    conn = open_db(settings)
    try:
        if get_customer(conn, customer_id) is None:
            raise LookupError(f"Unknown customer {customer_id}")
        existing = _latest_winback(log, customer_id)
        if existing is None:
            draft_winback(conn, log, llm, customer_id, max_tokens=max_tokens, language=language)
            existing = _latest_winback(log, customer_id)
    finally:
        conn.close()
    if existing is None:
        raise WhatsAppError("There is no win-back to send.")
    seq, text = existing
    return deliver(
        settings,
        channel,
        log,
        store,
        http,
        customer_id=customer_id,
        message_seq=seq,
        kind="text",
        text=text,
    )


def send_promo(
    settings: Settings,
    channel: str,
    log: ActionLog,
    store: WhatsAppStore,
    http: httpx.Client | None,
    offer_id: int,
) -> dict:
    _ensure_live(settings, channel)
    conn = open_db(settings)
    try:
        offer = load_offer_for_card(conn, offer_id)
        if offer is None:
            raise LookupError(f"Unknown offer {offer_id}")
        row = conn.execute("SELECT customer_id FROM offers WHERE id = ?", (offer_id,)).fetchone()
        image = read_promo_bytes(conn, settings.output_dir, offer_id)
    finally:
        conn.close()
    customer_id = int(row["customer_id"]) if row and row["customer_id"] is not None else None
    if not image:
        raise WhatsAppError("Generate the promo card before sending it on WhatsApp.")
    caption = f"{offer['product_name']} · {naira(offer['offer_price_ngn'])} · {offer['retailer_name']}"
    return deliver(
        settings,
        channel,
        log,
        store,
        http,
        customer_id=customer_id,
        message_seq=None,
        kind="image",
        text=caption,
        image=image,
        caption=caption,
    )


def send_template_command(settings: Settings, log: ActionLog, store: WhatsAppStore, http: httpx.Client | None) -> dict:
    customer_id = store.demo_customer_id()
    client = WhatsAppClient(settings, channel=settings.channel, http=http)
    result = client.send_template(settings.whatsapp_demo_to)
    remember_send(
        settings,
        log,
        store,
        customer_id=customer_id,
        message_seq=None,
        wamid_hash=result.wamid_hash,
        wamid_tail=result.wamid_tail,
        kind="template",
        note=f"Template {DEFAULT_TEMPLATE} ({DEFAULT_TEMPLATE_LANGUAGE}).",
        caption=None,
    )
    return _delivery_body(
        customer_id,
        "template",
        result.wamid_hash,
        result.wamid_tail,
        settings,
        "open" if window_open(store.load().get("last_inbound_at"), now_utc()) else "closed",
        "",
    )


def process_messages(
    settings: Settings,
    channel: str,
    log: ActionLog,
    llm,
    store: WhatsAppStore,
    messages: list[InboundMessage],
    *,
    http: httpx.Client | None,
    asr_http: httpx.Client | None,
    max_tokens: int,
) -> None:
    """Run accepted demo-phone messages through transcription and the reply flow."""
    for message in messages:
        try:
            _process_one(
                settings,
                channel,
                log,
                llm,
                store,
                message,
                http=http,
                asr_http=asr_http,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            _log_failure(settings, log, message, exc)


def _log_failure(settings: Settings, log: ActionLog, message: InboundMessage, exc: Exception) -> None:
    demo = normalize_phone(settings.whatsapp_demo_to)
    coded = phone_base64(demo)
    detail = redact_wamids(
        redact(
            str(exc),
            settings.whatsapp_token,
            settings.whatsapp_app_secret,
            settings.whatsapp_verify_token,
            demo,
            coded,
            coded[:-1] if len(coded) > 8 else None,
        )
    )
    public = public_wamid(message.message_id) if message.message_id else {}
    payload = {
        "wamid_hash": public.get("wamid_hash"),
        "wamid_tail": public.get("wamid_tail"),
        "error": redact_wamids(detail[:400]),
    }
    try:
        safe_append(log, settings, kind="whatsapp_error", actor="whatsapp", payload=payload)
    except Exception:
        return


def _process_one(
    settings: Settings,
    channel: str,
    log: ActionLog,
    llm,
    store: WhatsAppStore,
    message: InboundMessage,
    *,
    http: httpx.Client | None,
    asr_http: httpx.Client | None,
    max_tokens: int,
) -> None:
    conn = open_db(settings)
    try:
        customer_id = resolve_demo_customer(store, conn)
        if customer_id is None:
            safe_append(
                log,
                settings,
                kind="whatsapp_error",
                actor="whatsapp",
                payload={
                    **public_wamid(message.message_id),
                    "error": "No demo customer is selected. Choose one in the dashboard or CLI.",
                },
            )
            return
        safe_append(
            log,
            settings,
            kind="whatsapp_inbound",
            actor="whatsapp",
            payload={
                "customer_id": customer_id,
                **public_wamid(message.message_id),
                "kind": message.kind,
                "from_masked": mask_phone(message.from_phone),
            },
        )
        if message.kind in {"text", "interactive"}:
            text = (message.text or "").strip()
            if not text:
                return
            if _language_turn(settings, channel, log, store, conn, http, customer_id, text):
                return
            result = handle_reply(conn, log, llm, customer_id, text, max_tokens=max_tokens)
        elif message.kind == "audio":
            text, result = _audio_reply(
                settings,
                conn,
                log,
                llm,
                message,
                customer_id,
                http=http,
                asr_http=asr_http,
                max_tokens=max_tokens,
            )
            if interpret_language(text):
                _language_turn(settings, channel, log, store, conn, http, customer_id, text)
                return
            if result is None:
                return
        else:
            return
    finally:
        conn.close()
    if channel != "whatsapp":
        return
    seq = _latest_agent_seq(log, customer_id)
    hint = None
    try:
        hint_conn = open_db(settings)
        try:
            hint = take_language_hint(hint_conn, customer_id)
        finally:
            hint_conn.close()
    except Exception:
        hint = None
    outbound = result.message
    if hint:
        outbound = f"{outbound.rstrip()}\n\n{hint}"
    try:
        deliver(
            settings,
            channel,
            log,
            store,
            http,
            customer_id=customer_id,
            message_seq=seq,
            kind="text",
            text=outbound,
        )
    except SendBlocked as exc:
        _log_failure(settings, log, message, exc)


def _language_turn(settings, channel, log, store, conn, http, customer_id: int, text: str) -> bool:
    """Switch language or send the menu. True when this message was not a shop reply."""
    choice = interpret_language(text)
    if choice is None:
        return False
    log.append(
        kind="message",
        actor="customer",
        payload={"customer_id": customer_id, "task": "language", "content": text},
    )
    if choice["action"] == "menu":
        confirmation = "Pick a language. You can change it any time by replying with its name."
        log.append(
            kind="message",
            actor="agent",
            payload={
                "customer_id": customer_id,
                "task": "language",
                "direction": "out",
                "content": confirmation,
                "intent": None,
            },
        )
        if channel == "whatsapp":
            _send_language_menu(settings, channel, log, store, http, customer_id)
        return True
    code = save_language(conn, customer_id, choice["language"], channel="whatsapp" if channel == "whatsapp" else "simulator")
    confirmation = CONFIRM[code]
    log.append(
        kind="message",
        actor="agent",
        payload={
            "customer_id": customer_id,
            "task": "language",
            "direction": "out",
            "content": confirmation,
            "intent": None,
            "language": code,
        },
    )
    if channel == "whatsapp":
        try:
            deliver(
                settings,
                channel,
                log,
                store,
                http,
                customer_id=customer_id,
                message_seq=None,
                kind="text",
                text=confirmation,
            )
        except SendBlocked as exc:
            _log_failure(settings, log, _stub_message(text), exc)
    return True


def _stub_message(text: str):
    return InboundMessage(
        message_id="",
        from_phone="",
        kind="text",
        text=text,
        media_id=None,
        mime_type=None,
        timestamp=None,
    )


def _send_language_menu(settings, channel, log, store, http, customer_id: int) -> None:
    from convoplex.integrations.whatsapp import WhatsAppClient

    moment = now_utc()
    inbound = store.load().get("last_inbound_at")
    if not isinstance(inbound, str) or not window_open(inbound, moment):
        try:
            deliver(
                settings,
                channel,
                log,
                store,
                http,
                customer_id=customer_id,
                message_seq=None,
                kind="text",
                text="Reply LANGUAGE when the chat is open and I will show the language list.",
            )
        except SendBlocked:
            return
        return
    try:
        client = WhatsAppClient(settings, channel=channel, http=http)
        client.send_interactive(settings.whatsapp_demo_to, language_list_interactive())
    except (SendBlocked, WhatsAppError) as exc:
        _log_failure(
            settings,
            log,
            _stub_message("language"),
            exc,
        )


def _latest_agent_seq(log: ActionLog, customer_id: int) -> int | None:
    found = None
    for entry in log.read_all():
        payload = entry.get("payload") or {}
        if entry.get("kind") != "message" or entry.get("actor") != "agent":
            continue
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        content = payload.get("content") or ""
        if content:
            found = int(entry["seq"])
    return found


def _audio_reply(
    settings: Settings,
    conn,
    log: ActionLog,
    llm,
    message: InboundMessage,
    customer_id: int,
    *,
    http: httpx.Client | None,
    asr_http: httpx.Client | None,
    max_tokens: int,
):
    if not message.media_id:
        raise WhatsAppError("Audio message is missing a media id.")
    client = WhatsAppClient(settings, channel="whatsapp", http=http)
    raw, mime = client.download_media(message.media_id)
    filename = audio_filename(mime or message.mime_type, message.media_id)
    try:
        transcript = transcribe_audio(
            raw,
            filename=filename,
            content_type=mime or message.mime_type or "",
            mode=settings.llm_mode,
            api_key=settings.meta_api_key,
            base_url=settings.meta_base_url,
            model=settings.asr_model,
            client=asr_http,
        )
    except VoiceError as exc:
        raise WhatsAppError(str(exc)) from exc
    log.append(
        kind="model_call",
        actor="agent",
        payload={
            "customer_id": customer_id,
            "task": "transcribe",
            "model": transcript.model,
            "transcript": transcript.text,
            "latency_ms": transcript.latency_ms,
            "audio_format": transcript.source_format,
            "audio_duration_ms": transcript.audio_duration_ms,
            "finish_reason": "stop",
            "attempt": 1,
            "max_tokens": None,
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        },
    )
    spoken = transcript.text.strip()
    if not spoken:
        return "", None
    if interpret_language(spoken):
        return spoken, None
    result = handle_reply(conn, log, llm, customer_id, spoken, max_tokens=max_tokens)
    return transcript.text, result


def run_check(settings: Settings, http: httpx.Client | None = None) -> int:
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        missing = []
        if not settings.whatsapp_token:
            missing.append("WHATSAPP_TOKEN")
        if not settings.whatsapp_phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        print(f"WhatsApp is not configured. Missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    owns = http is None
    client_http = http
    try:
        info = WhatsAppClient(settings, channel=settings.channel, http=client_http).fetch_phone()
    except WhatsAppError as exc:
        print(_public_error(settings, exc), file=sys.stderr)
        return 1
    finally:
        if owns and client_http is not None:
            client_http.close()
    from convoplex.integrations.whatsapp import format_phone_check

    print(format_phone_check(info))
    return 0


def run_send_template(settings: Settings, http: httpx.Client | None = None) -> int:
    log = ActionLog(settings.log_path)
    store = store_for(settings)
    try:
        result = send_template_command(settings, log, store, http)
    except (SendBlocked, WhatsAppError) as exc:
        print(_public_error(settings, exc), file=sys.stderr)
        return 1
    _print_delivery(result)
    return 0


def run_winback(
    settings: Settings,
    customer_id: int,
    *,
    language: str | None = None,
    promo_card: bool = False,
    http: httpx.Client | None = None,
) -> int:
    """Same send as POST /v1/customers/{id}/whatsapp. --promo-card also sends the card."""
    log = ActionLog(settings.log_path)
    store = store_for(settings)
    llm = _llm_for(settings)
    try:
        result = send_winback(
            settings,
            settings.channel,
            log,
            llm,
            store,
            http,
            customer_id,
            max_tokens=settings.max_tokens,
            language=language,
        )
    except LookupError:
        print(f"Unknown customer {customer_id}", file=sys.stderr)
        return 1
    except (SendBlocked, WhatsAppError, LLMError) as exc:
        print(_public_error(settings, exc), file=sys.stderr)
        return 1
    _print_delivery(result)
    if not promo_card:
        return 0
    try:
        promo = _send_promo_card(settings, log, store, http, customer_id)
    except (SendBlocked, WhatsAppError, LLMError, LookupError) as exc:
        print(_public_error(settings, exc), file=sys.stderr)
        return 1
    print("Promo card:")
    _print_delivery(promo)
    return 0


def _llm_for(settings: Settings):
    if settings.llm_mode == "live":
        return LiveMuse(
            api_key=settings.meta_api_key,
            base_url=settings.meta_base_url,
            model=settings.chat_model,
        )
    return MockMuse()


def _send_promo_card(
    settings: Settings,
    log: ActionLog,
    store: WhatsAppStore,
    http: httpx.Client | None,
    customer_id: int,
) -> dict:
    conn = open_db(settings)
    try:
        row = conn.execute(
            "SELECT id FROM offers WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
            (customer_id,),
        ).fetchone()
        if row is None:
            raise WhatsAppError("There is no offer to put on a promo card.")
        offer_id = int(row["id"])
        offer = load_offer_for_card(conn, offer_id)
        existing = read_promo_bytes(conn, settings.output_dir, offer_id) if offer else None
        saved = None
        card = None
        if offer is not None and not existing:
            card = build_promo_image(
                offer,
                mode=settings.llm_mode,
                api_key=settings.meta_api_key,
                base_url=settings.meta_base_url,
                model=settings.image_model,
            )
            saved = remember_promo(conn, card.image, settings.output_dir, offer_id)
    finally:
        conn.close()
    if offer is None:
        raise WhatsAppError("There is no offer to put on a promo card.")
    if card is not None and saved is not None:
        log.append(
            kind="model_call",
            actor="agent",
            payload={
                "customer_id": customer_id,
                "task": "promo_card",
                "model": card.model,
                "offer_id": offer_id,
                "latency_ms": card.latency_ms,
                "path": str(saved),
                "url": f"/media/promo/{promo_filename(offer_id)}",
                "finish_reason": "stop",
                "attempt": 1,
                "max_tokens": None,
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                },
            },
        )
    return send_promo(settings, settings.channel, log, store, http, offer_id)


def _print_delivery(result: dict) -> None:
    label = result.get("sent") or "message"
    if label == "template":
        label = f"{DEFAULT_TEMPLATE} ({DEFAULT_TEMPLATE_LANGUAGE})"
    print(f"Sent {label} to {result.get('to_masked')}")
    if result.get("note"):
        print(result["note"])
    print(f"Message id: …{result.get('wamid_tail')}")


def _public_error(settings: Settings, exc: Exception) -> str:
    demo = normalize_phone(settings.whatsapp_demo_to)
    coded = phone_base64(demo)
    return redact_wamids(
        redact(
            str(exc),
            settings.whatsapp_token,
            settings.whatsapp_app_secret,
            demo,
            coded,
            coded[:-1] if len(coded) > 8 else None,
        )
    )


def run_customer(settings: Settings, customer_id: int) -> int:
    try:
        set_demo_customer(settings, customer_id)
    except LookupError:
        print(f"Unknown customer {customer_id}", file=sys.stderr)
        return 1
    print(
        f"Demo customer is {customer_id}. "
        "Inbound WhatsApp messages from the allow-listed phone map to this customer."
    )
    return 0
