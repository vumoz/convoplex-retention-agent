"""FastAPI surface for the retention agent.

Voice notes and promo cards run here. WhatsApp sends stay off until the
channel is whatsapp and the recipient is the allow-listed demo phone.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from convoplex import __version__
from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import get_customer, list_at_risk
from convoplex.db import SYNTHETIC_LABEL, connect, scenario_as_of
from convoplex.integrations.images import (
    build_promo_image,
    clear_promo_cards,
    load_offer_for_card,
    promo_filename,
    save_promo_image,
)
from convoplex.integrations.voice import VoiceError, transcribe_audio
from convoplex.integrations.whatsapp import (
    SendBlocked,
    WhatsAppError,
    normalize_phone,
    parse_inbound,
    parse_statuses,
    signature_matches,
    verify_subscription,
)
from convoplex.llm import LLMError, LiveMuse, MockMuse
from convoplex.metrics import snapshot
from convoplex.synthetic.generate import DEFAULT_CUSTOMERS, DEFAULT_SEED, generate
from convoplex.whatsapp_flow import (
    process_messages,
    record_statuses,
    send_promo,
    send_winback,
    set_demo_customer,
    status_view,
    store_for,
)
from convoplex.whatsapp_state import stamp_from_message

DASHBOARD = Path(__file__).resolve().parent / "static" / "dashboard.html"
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ChannelIn(BaseModel):
    channel: str = Field(pattern="^(simulator|whatsapp)$")


class DemoCustomerIn(BaseModel):
    customer_id: int = Field(ge=1)


def build_llm(settings: Settings):
    if settings.llm_mode == "live":
        return LiveMuse(
            api_key=settings.meta_api_key,
            base_url=settings.meta_base_url,
            model=settings.chat_model,
        )
    return MockMuse()


def create_app(settings: Settings | None = None, *, generate_if_missing: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    if generate_if_missing and not settings.db_path.exists():
        generate(settings.db_path)
    elif not settings.db_path.exists():
        from convoplex.db import init_db

        init_db(settings.db_path)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    (settings.output_dir / "promo").mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="Convoplex Retention Agent",
        version=__version__,
        summary="Win back lapsed customers for Nigerian retailers.",
        description=(
            "Synthetic-data agent built during the Meta AI Developer Hackathon, "
            "27 Oct to 4 Nov 2026. Muse Spark drafts WhatsApp-style win-backs. "
            "Voice notes are transcribed and promo cards are drawn locally over a "
            "text-free product image. WhatsApp delivery uses the Cloud API test "
            "number only when the channel is whatsapp and the recipient is the "
            "allow-listed demo phone."
        ),
    )
    app.state.settings = settings
    app.state.channel = settings.channel
    app.state.log = ActionLog(settings.log_path)
    app.state.llm = build_llm(settings)
    app.state.http_client = None
    app.state.whatsapp_http = None

    def get_settings(request: Request) -> Settings:
        return request.app.state.settings

    def get_conn(current: Settings = Depends(get_settings)):
        conn = connect(current.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/")
    def dashboard_page():
        if not DASHBOARD.exists():
            raise HTTPException(404, "dashboard is not installed")
        return FileResponse(DASHBOARD, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        conn = connect(settings.db_path)
        try:
            as_of = scenario_as_of(conn).isoformat()
            label = SYNTHETIC_LABEL
        finally:
            conn.close()
        return {
            "service": "convoplex-retention-agent",
            "version": __version__,
            "llm_mode": settings.llm_mode,
            "chat_model": settings.chat_model,
            "asr_model": settings.asr_model,
            "image_model": settings.image_model,
            "meta_key_set": bool(settings.meta_api_key),
            "scenario_as_of": as_of,
            "synthetic": True,
            "label": label,
            "dashboard": True,
            "channel": app.state.channel,
        }

    @app.get("/v1/retailers")
    def retailers(conn=Depends(get_conn)):
        rows = conn.execute(
            "SELECT id, name, city, area, category_focus, phone FROM retailers ORDER BY id"
        ).fetchall()
        return {
            "synthetic": True,
            "retailers": [dict(row) for row in rows],
        }

    @app.get("/v1/customers/at-risk")
    def at_risk(
        retailer_id: int | None = None,
        limit: int = Query(50, ge=1, le=500),
        include_lost: bool = False,
        as_of: str | None = None,
        conn=Depends(get_conn),
    ):
        day = _parse_day(as_of) if as_of else scenario_as_of(conn)
        rows = list_at_risk(
            conn,
            as_of=day,
            retailer_id=retailer_id,
            limit=limit,
            include_lost=include_lost,
        )
        return {"as_of": day.isoformat(), "synthetic": True, "customers": [_public(row) for row in rows]}

    @app.get("/v1/customers/{customer_id}")
    def customer(customer_id: int, conn=Depends(get_conn)):
        row = get_customer(conn, customer_id)
        if row is None:
            raise HTTPException(404, "customer not found")
        return _public(row)

    @app.get("/v1/customers/{customer_id}/thread")
    def thread(customer_id: int, conn=Depends(get_conn)):
        row = get_customer(conn, customer_id)
        if row is None:
            raise HTTPException(404, "customer not found")
        return {
            "customer_id": customer_id,
            "customer": _public(row),
            "messages": _thread_messages(
                app.state.log.read_all(),
                customer_id,
                store_for(settings).statuses(),
            ),
            "offer": _latest_offer(conn, customer_id),
            "order": _latest_offer_order(conn, customer_id),
            "synthetic": True,
        }

    @app.post("/v1/customers/{customer_id}/winback")
    def winback(customer_id: int, conn=Depends(get_conn)):
        try:
            result = draft_winback(
                conn,
                app.state.log,
                app.state.llm,
                customer_id,
                max_tokens=settings.max_tokens,
            )
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _agent_body(result)

    @app.post("/v1/customers/{customer_id}/reply")
    def reply(customer_id: int, body: ReplyIn, conn=Depends(get_conn)):
        try:
            result = handle_reply(
                conn,
                app.state.log,
                app.state.llm,
                customer_id,
                body.text,
                max_tokens=settings.max_tokens,
            )
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _agent_body(result)

    @app.get("/v1/metrics")
    def metrics(conn=Depends(get_conn)):
        return snapshot(conn, app.state.log.read_all())

    @app.get("/v1/actions")
    def actions(limit: int = Query(100, ge=1, le=1000)):
        entries = app.state.log.read_all()
        return {"entries": entries[-limit:], "count": len(entries)}

    @app.post("/v1/actions/verify")
    def verify_actions():
        result = app.state.log.verify()
        return {"ok": result.ok, "entries": result.entries, "error": result.error, "seq": result.seq}

    @app.post("/v1/demo/reset")
    def reset_demo():
        path = generate(settings.db_path)
        if settings.log_path.exists():
            settings.log_path.unlink()
        clear_promo_cards(settings.output_dir)
        store_for(settings).reset()
        return {
            "ok": True,
            "synthetic": True,
            "database": str(path),
            "customers": DEFAULT_CUSTOMERS,
            "seed": DEFAULT_SEED,
        }

    @app.get("/v1/whatsapp/status")
    def whatsapp_status():
        return status_view(settings, app.state.channel, store_for(settings))

    @app.post("/v1/whatsapp/channel")
    def whatsapp_channel(body: ChannelIn):
        app.state.channel = body.channel
        app.state.settings = settings.with_channel(body.channel)
        return status_view(app.state.settings, app.state.channel, store_for(app.state.settings))

    @app.post("/v1/whatsapp/customer")
    def whatsapp_customer(body: DemoCustomerIn):
        try:
            set_demo_customer(app.state.settings, body.customer_id)
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        return status_view(app.state.settings, app.state.channel, store_for(app.state.settings))

    @app.post("/v1/customers/{customer_id}/whatsapp")
    def whatsapp_winback(customer_id: int):
        try:
            return send_winback(
                app.state.settings,
                app.state.channel,
                app.state.log,
                app.state.llm,
                store_for(app.state.settings),
                app.state.whatsapp_http,
                customer_id,
                max_tokens=app.state.settings.max_tokens,
            )
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        except SendBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        except (WhatsAppError, LLMError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/v1/offers/{offer_id}/whatsapp")
    def whatsapp_promo(offer_id: int):
        try:
            return send_promo(
                app.state.settings,
                app.state.channel,
                app.state.log,
                store_for(app.state.settings),
                app.state.whatsapp_http,
                offer_id,
            )
        except LookupError:
            raise HTTPException(404, "offer not found") from None
        except SendBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        except WhatsAppError as exc:
            raise HTTPException(409 if "Generate the promo card" in str(exc) else 502, str(exc)) from exc

    @app.get("/v1/whatsapp/webhook")
    def whatsapp_verify(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ):
        expected = settings.whatsapp_verify_token
        if not expected:
            return JSONResponse(
                status_code=501,
                content={
                    "status": "unconfigured",
                    "message": "Set WHATSAPP_VERIFY_TOKEN to complete the Cloud API handshake.",
                },
            )
        challenge = verify_subscription(hub_mode, hub_verify_token, hub_challenge, expected)
        if challenge is None:
            raise HTTPException(403, "verification failed")
        return PlainTextResponse(challenge)

    @app.post("/v1/whatsapp/webhook")
    async def whatsapp_receive(request: Request, background: BackgroundTasks):
        current = request.app.state.settings
        raw = await request.body()
        secret = current.whatsapp_app_secret
        live = request.app.state.channel == "whatsapp"
        if live and not secret:
            raise HTTPException(403, "WHATSAPP_APP_SECRET is required for live WhatsApp webhooks")
        if secret and not signature_matches(raw, request.headers.get("x-hub-signature-256"), secret):
            raise HTTPException(403, "invalid signature")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(400, "webhook body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "webhook body must be a JSON object")
        store = store_for(current)
        status_count = record_statuses(current, app.state.log, store, parse_statuses(payload))
        demo = normalize_phone(current.whatsapp_demo_to)
        accepted: list[InboundMessage] = []
        duplicates: list[str] = []
        ignored = 0
        moment = datetime.now(timezone.utc)
        for item in parse_inbound(payload):
            if not item.message_id or normalize_phone(item.from_phone) != demo:
                ignored += 1
                continue
            if item.kind not in {"text", "audio"}:
                ignored += 1
                continue
            if not store.claim(item.message_id):
                duplicates.append(item.message_id)
                continue
            store.note_inbound(stamp_from_message(item.timestamp, moment))
            accepted.append(item)
        if accepted:
            background.add_task(
                process_messages,
                current,
                app.state.channel,
                app.state.log,
                app.state.llm,
                store,
                accepted,
                http=app.state.whatsapp_http,
                asr_http=app.state.http_client,
                max_tokens=current.max_tokens,
            )
        return {
            "status": "ok",
            "processed": bool(accepted),
            "accepted": [item.message_id for item in accepted],
            "duplicates": duplicates,
            "ignored": ignored,
            "statuses": status_count,
        }

    @app.post("/v1/voice/transcribe")
    def voice_transcribe(
        request: str = Form(...),
        audio: UploadFile = File(...),
        customer_id: int | None = Form(None),
        conn=Depends(get_conn),
    ):
        try:
            body = json.loads(request)
        except json.JSONDecodeError:
            raise HTTPException(400, "request part must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "request part must be a JSON object")
        if customer_id is not None and get_customer(conn, customer_id) is None:
            raise HTTPException(404, "customer not found")
        # Sync read keeps this on the same thread as the SQLite connection.
        raw = audio.file.read()
        if len(raw) > MAX_AUDIO_BYTES:
            raise HTTPException(400, "Audio is larger than 20 MB.")
        model = str(body.get("model") or settings.asr_model)
        try:
            transcript = transcribe_audio(
                raw,
                filename=audio.filename or "",
                content_type=audio.content_type or "",
                mode=settings.llm_mode,
                api_key=settings.meta_api_key,
                base_url=settings.meta_base_url,
                model=model,
                client=app.state.http_client,
            )
        except VoiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        app.state.log.append(
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
                "usage": _usage_zeros(),
            },
        )
        response = {
            "transcript": transcript.text,
            "model": transcript.model,
            "latency_ms": transcript.latency_ms,
            "audio_format": transcript.source_format,
            "llm": transcript.mode,
            "synthetic": True,
        }
        if customer_id is None:
            return response
        try:
            result = handle_reply(
                conn,
                app.state.log,
                app.state.llm,
                customer_id,
                transcript.text,
                max_tokens=settings.max_tokens,
            )
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        response.update(_agent_body(result))
        return response

    @app.post("/v1/offers/{offer_id}/promo-card")
    def promo_card(offer_id: int, conn=Depends(get_conn)):
        offer = load_offer_for_card(conn, offer_id)
        if offer is None:
            raise HTTPException(404, "offer not found")
        try:
            card = build_promo_image(
                offer,
                mode=settings.llm_mode,
                api_key=settings.meta_api_key,
                base_url=settings.meta_base_url,
                model=settings.image_model,
                client=app.state.http_client,
            )
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        path = save_promo_image(card.image, settings.output_dir, offer_id)
        url = f"/media/promo/{promo_filename(offer_id)}"
        app.state.log.append(
            kind="model_call",
            actor="agent",
            payload={
                "customer_id": offer.get("customer_id"),
                "task": "promo_card",
                "model": card.model,
                "offer_id": offer_id,
                "latency_ms": card.latency_ms,
                "path": str(path),
                "url": url,
                "prompt": card.prompt,
                "overlay": {
                    "retailer_name": offer["retailer_name"],
                    "city": offer["city"],
                    "product_name": offer["product_name"],
                    "offer_price_ngn": offer["offer_price_ngn"],
                    "list_price_ngn": offer["list_price_ngn"],
                    "discount_percent": offer["discount_percent"],
                    "price": card.overlay["price"],
                    "was": card.overlay["was"],
                    "off": card.overlay["off"],
                },
                "finish_reason": "stop",
                "attempt": 1,
                "max_tokens": None,
                "usage": _usage_zeros(),
            },
        )
        return {
            "offer_id": offer_id,
            "path": str(path),
            "url": url,
            "model": card.model,
            "llm": card.mode,
            "latency_ms": card.latency_ms,
            "prompt": card.prompt,
            "overlay": {
                "retailer_name": offer["retailer_name"],
                "city": offer["city"],
                "product_name": offer["product_name"],
                "offer_price_ngn": offer["offer_price_ngn"],
                "list_price_ngn": offer["list_price_ngn"],
                "discount_percent": offer["discount_percent"],
                "price": card.overlay["price"],
                "was": card.overlay["was"],
                "off": card.overlay["off"],
            },
            "synthetic": True,
        }

    app.mount("/media", StaticFiles(directory=str(settings.output_dir)), name="media")
    return app


def __getattr__(name: str):
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)


def _parse_day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "as_of must be YYYY-MM-DD") from None


def _usage_zeros() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


def _public(row: dict) -> dict:
    favourite = row.get("favourite_product")
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "phone": row["phone"],
        "city": row["city"],
        "area": row["area"],
        "language": row["language"],
        "retailer_id": row["retailer_id"],
        "retailer_name": row["retailer_name"],
        "segment": row["segment"],
        "score": row["score"],
        "recency_days": row["recency_days"],
        "frequency": row["frequency"],
        "monetary_ngn": row["monetary_ngn"],
        "median_gap_days": row["median_gap_days"],
        "reasons": row["reasons"],
        "favourite_product": favourite["name"] if favourite else None,
        "favourite_in_stock": bool(favourite and favourite["stock_qty"] > 0),
        "synthetic": True,
        "synthetic_archetype": row["synthetic_archetype"],
    }


def _agent_body(result) -> dict:
    return {
        "customer_id": result.customer_id,
        "task": result.task,
        "language": result.language,
        "message": result.message,
        "intent": result.intent,
        "offer": result.offer,
        "order": result.order,
        "tool_calls": result.tool_trace,
        "llm": result.llm_mode,
        "synthetic": True,
    }


def _thread_messages(entries: list[dict], customer_id: int, statuses: dict[str, str] | None = None) -> list[dict]:
    statuses = statuses or {}
    sends_by_seq: dict[int, dict] = {}
    loose: list[tuple[int, dict]] = []
    for entry in entries:
        payload = entry.get("payload") or {}
        if entry.get("kind") != "whatsapp_send":
            continue
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        view = _whatsapp_view(entry, statuses)
        seq = payload.get("message_seq")
        if seq:
            sends_by_seq[int(seq)] = view
        else:
            loose.append((int(entry.get("seq") or 0), view))

    messages: list[tuple[int, dict]] = []
    pending: dict | None = None
    for entry in entries:
        payload = entry.get("payload") or {}
        if int(payload.get("customer_id") or 0) != int(customer_id):
            continue
        kind = entry.get("kind")
        if kind == "model_call" and payload.get("task") == "transcribe":
            pending = {
                "model": payload.get("model"),
                "latency_ms": payload.get("latency_ms"),
                "transcript": payload.get("transcript"),
            }
            continue
        if kind != "message":
            continue
        content = payload.get("content") or ""
        if content == "draft win-back":
            continue
        actor = entry.get("actor")
        seq = int(entry.get("seq") or 0)
        if actor == "customer":
            item = {
                "role": "customer",
                "text": content,
                "ts": entry.get("ts"),
                "seq": entry.get("seq"),
            }
            if pending and pending.get("transcript") == content:
                item["voice"] = {
                    "model": pending.get("model"),
                    "latency_ms": pending.get("latency_ms"),
                }
            pending = None
            messages.append((seq, item))
        elif actor == "agent" and content:
            item = {
                "role": "shop",
                "text": content,
                "task": payload.get("task"),
                "intent": payload.get("intent"),
                "ts": entry.get("ts"),
                "seq": entry.get("seq"),
            }
            attached = sends_by_seq.get(seq)
            if attached:
                item["whatsapp"] = attached
            messages.append((seq, item))
    for seq, view in loose:
        messages.append(
            (
                seq,
                {
                    "role": "shop",
                    "text": view.get("caption") or view.get("note") or "WhatsApp template hello_world (en_US).",
                    "ts": view.get("ts"),
                    "seq": seq,
                    "whatsapp": view,
                },
            )
        )
    messages.sort(key=lambda item: item[0])
    return [item for _, item in messages]


def _whatsapp_view(entry: dict, statuses: dict[str, str]) -> dict:
    payload = entry.get("payload") or {}
    wamid = payload.get("wamid")
    status = statuses.get(wamid) if wamid else None
    if status is None:
        status = payload.get("status")
    return {
        "kind": payload.get("kind"),
        "status": status,
        "to_masked": payload.get("to_masked"),
        "wamid": wamid,
        "note": payload.get("note") or "",
        "caption": payload.get("caption") or "",
        "ts": entry.get("ts"),
    }


def _latest_offer(conn, customer_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT o.id, o.offer_price_ngn, o.list_price_ngn, o.discount_percent, o.status,
               p.name AS product_name
        FROM offers o
        JOIN products p ON p.id = o.product_id
        WHERE o.customer_id = ?
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    offer_id = int(row["id"])
    return {
        "offer_id": offer_id,
        "offer_price_ngn": int(row["offer_price_ngn"]),
        "list_price_ngn": int(row["list_price_ngn"]),
        "discount_percent": int(row["discount_percent"]),
        "status": row["status"],
        "product_name": row["product_name"],
        "promo_url": f"/media/promo/{promo_filename(offer_id)}",
    }


def _latest_offer_order(conn, customer_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, total_ngn, status, ordered_on
        FROM orders
        WHERE customer_id = ? AND offer_id IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "order_id": int(row["id"]),
        "total_ngn": int(row["total_ngn"]),
        "status": row["status"],
        "ordered_on": row["ordered_on"],
    }
