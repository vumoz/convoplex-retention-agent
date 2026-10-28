"""FastAPI surface for the retention agent.

Voice notes and promo cards run here. WhatsApp sends stay off until the
channel is whatsapp and the recipient is the allow-listed demo phone.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from convoplex import __version__
from convoplex.access import (
    ADMIN_COOKIE,
    CappedLLM,
    PromoCapError,
    SECURITY_HEADERS,
    configure_access_log,
    enforce_try_limit,
    is_admin,
    log_access,
    require_admin,
    select_mode,
    token_matches,
)
from convoplex.actionlog import open_action_log
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import get_customer, list_at_risk
from convoplex.db import SYNTHETIC_LABEL, open_db, ping_db, scenario_as_of
from convoplex.integrations.images import (
    build_promo_image,
    clear_promo_cards,
    load_offer_for_card,
    load_promo_blob,
    promo_filename,
    remember_promo,
)
from convoplex.integrations.voice import VoiceError, transcribe_audio
from convoplex.integrations.whatsapp import (
    SendBlocked,
    WhatsAppError,
    normalize_phone,
    parse_inbound,
    parse_statuses,
    public_wamid,
    redact_wamids,
    scrub_wamids,
    signature_matches,
    verify_subscription,
)
from convoplex.languages import clean_language, language_label, save_language
from convoplex.llm import LLMError, LiveMuse, MockMuse
from convoplex.metrics import snapshot
from convoplex.synthetic.generate import DEFAULT_CUSTOMERS, DEFAULT_SEED, generate, reset_and_seed, seed_if_empty
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


class AdminIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class LanguageIn(BaseModel):
    language: str = Field(min_length=2, max_length=8)


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
    settings = _prepare_storage(settings, generate_if_missing)
    media_root = _ensure_output_dir(settings.output_dir)
    configure_access_log()

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
    app.state.log = open_action_log(settings)
    app.state.llm = build_llm(settings)
    app.state.mock_llm = MockMuse()
    app.state.live_llm = app.state.llm if settings.llm_mode == "live" else None
    app.state.media_root = media_root
    app.state.http_client = None
    app.state.whatsapp_http = None

    @app.middleware("http")
    async def protect(request: Request, call_next):
        length = request.headers.get("content-length")
        if length:
            try:
                too_big = int(length) > MAX_AUDIO_BYTES
            except ValueError:
                too_big = False
            if too_big:
                denied = JSONResponse(
                    {"detail": "Request body is larger than 20 MB."},
                    status_code=413,
                )
                _stamp(denied)
                log_access(request, 413)
                return denied
        response = await call_next(request)
        _stamp(response)
        log_access(request, response.status_code)
        return response

    def admin_dep(request: Request) -> None:
        require_admin(request, request.app.state.settings)

    def try_dep(request: Request) -> None:
        enforce_try_limit(request, request.app.state.settings)

    def get_settings(request: Request) -> Settings:
        return request.app.state.settings

    def get_conn(current: Settings = Depends(get_settings)):
        conn = open_db(current)
        try:
            if current.database_url and generate_if_missing:
                seed_if_empty(conn)
            yield conn
        finally:
            conn.close()

    @app.get("/")
    def dashboard_page():
        if not DASHBOARD.exists():
            raise HTTPException(404, "dashboard is not installed")
        return FileResponse(DASHBOARD, headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    def healthz():
        """Lightweight probe. 503 only when DATABASE_URL is set and the ping fails."""
        if not settings.database_url:
            return {"ok": True}
        try:
            ping_db(settings)
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    @app.get("/health")
    def health():
        conn = open_db(settings)
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

    @app.get("/v1/admin/session")
    def admin_session(request: Request):
        current = request.app.state.settings
        required = bool(current.admin_token)
        return {"required": required, "admin": (not required) or is_admin(request, current)}

    @app.post("/v1/admin/login")
    def admin_login(body: AdminIn, request: Request):
        current = request.app.state.settings
        if not current.admin_token:
            return {"ok": True, "admin": False, "required": False}
        if not token_matches(body.token, current.admin_token):
            raise HTTPException(401, "invalid admin token")
        response = JSONResponse({"ok": True, "admin": True, "required": True})
        secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
        response.set_cookie(
            ADMIN_COOKIE,
            body.token,
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=12 * 60 * 60,
            path="/",
        )
        return response

    @app.post("/v1/admin/logout")
    def admin_logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(ADMIN_COOKIE, path="/")
        return response

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

    @app.post("/v1/customers/{customer_id}/language", dependencies=[Depends(admin_dep)])
    def customer_language(customer_id: int, body: LanguageIn, conn=Depends(get_conn)):
        row = get_customer(conn, customer_id)
        if row is None:
            raise HTTPException(404, "customer not found")
        try:
            clean_language(body.language)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        save_language(conn, customer_id, body.language, channel="dashboard")
        updated = get_customer(conn, customer_id)
        return _public(updated)

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

    @app.post("/v1/customers/{customer_id}/winback", dependencies=[Depends(try_dep)])
    def winback(customer_id: int, request: Request, conn=Depends(get_conn)):
        try:
            result = draft_winback(
                conn,
                app.state.log,
                _llm_for_request(request, "chat"),
                customer_id,
                max_tokens=settings.max_tokens,
            )
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _agent_body(result)

    @app.post("/v1/customers/{customer_id}/reply", dependencies=[Depends(try_dep)])
    def reply(customer_id: int, body: ReplyIn, request: Request, conn=Depends(get_conn)):
        try:
            result = handle_reply(
                conn,
                app.state.log,
                _llm_for_request(request, "chat"),
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
        return {"entries": scrub_wamids(entries[-limit:]), "count": len(entries)}

    @app.post("/v1/actions/verify")
    def verify_actions():
        result = app.state.log.verify()
        return {"ok": result.ok, "entries": result.entries, "error": result.error, "seq": result.seq}

    @app.post("/v1/demo/reset", dependencies=[Depends(admin_dep)])
    def reset_demo():
        current = app.state.settings
        if current.database_url:
            conn = open_db(current)
            try:
                reset_and_seed(conn, postgres=True)
            finally:
                conn.close()
            app.state.log.clear()
            database = "postgres"
        else:
            path = generate(current.db_path)
            if current.log_path.exists():
                current.log_path.unlink()
            database = str(path)
        try:
            clear_promo_cards(current.output_dir)
        except OSError:
            pass
        store_for(current).reset()
        return {
            "ok": True,
            "synthetic": True,
            "database": database,
            "customers": DEFAULT_CUSTOMERS,
            "seed": DEFAULT_SEED,
        }

    @app.get("/v1/whatsapp/status")
    def whatsapp_status():
        return status_view(settings, app.state.channel, store_for(settings))

    @app.post("/v1/whatsapp/channel", dependencies=[Depends(admin_dep)])
    def whatsapp_channel(body: ChannelIn):
        app.state.channel = body.channel
        app.state.settings = settings.with_channel(body.channel)
        return status_view(app.state.settings, app.state.channel, store_for(app.state.settings))

    @app.post("/v1/whatsapp/customer", dependencies=[Depends(admin_dep)])
    def whatsapp_customer(body: DemoCustomerIn):
        try:
            set_demo_customer(app.state.settings, body.customer_id)
        except LookupError:
            raise HTTPException(404, "customer not found") from None
        return status_view(app.state.settings, app.state.channel, store_for(app.state.settings))

    @app.post("/v1/customers/{customer_id}/whatsapp", dependencies=[Depends(admin_dep)])
    def whatsapp_winback(customer_id: int, request: Request):
        try:
            return send_winback(
                app.state.settings,
                app.state.channel,
                app.state.log,
                _llm_for_request(request, "chat"),
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

    @app.post("/v1/offers/{offer_id}/whatsapp", dependencies=[Depends(admin_dep)])
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
            if item.kind not in {"text", "audio", "interactive"}:
                ignored += 1
                continue
            public = public_wamid(item.message_id)
            if not store.claim(public["wamid_hash"]):
                duplicates.append(public["wamid_tail"])
                continue
            store.note_inbound(stamp_from_message(item.timestamp, moment))
            accepted.append(item)
        if accepted:
            # 200 goes out before the reply is generated. On Vercel Python,
            # FastAPI BackgroundTasks keep running after the response (the
            # waitUntil equivalent, vercel/vercel#14011), so Meta's webhook
            # timeout is not spent on the model or transcription call.
            background.add_task(
                process_messages,
                current,
                app.state.channel,
                app.state.log,
                CappedLLM(current, app.state.live_llm, app.state.mock_llm),
                store,
                accepted,
                http=app.state.whatsapp_http,
                asr_http=app.state.http_client,
                max_tokens=current.max_tokens,
            )
        return {
            "status": "ok",
            "processed": bool(accepted),
            "accepted": [public_wamid(item.message_id)["wamid_tail"] for item in accepted],
            "duplicates": duplicates,
            "ignored": ignored,
            "statuses": status_count,
        }

    @app.post("/v1/voice/transcribe", dependencies=[Depends(try_dep)])
    def voice_transcribe(
        http_request: Request,
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
        mode = _llm_mode_for_request(http_request, "voice")
        try:
            transcript = transcribe_audio(
                raw,
                filename=audio.filename or "",
                content_type=audio.content_type or "",
                mode=mode,
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
                _llm_for_request(http_request, "chat"),
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

    @app.post("/v1/offers/{offer_id}/promo-card", dependencies=[Depends(try_dep)])
    def promo_card(offer_id: int, request: Request, conn=Depends(get_conn)):
        offer = load_offer_for_card(conn, offer_id)
        if offer is None:
            raise HTTPException(404, "offer not found")
        mode = _llm_mode_for_request(request, "promo")
        try:
            card = build_promo_image(
                offer,
                mode=mode,
                api_key=settings.meta_api_key,
                base_url=settings.meta_base_url,
                model=settings.image_model,
                client=app.state.http_client,
            )
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        path = remember_promo(conn, card.image, settings.output_dir, offer_id)
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

    @app.get("/media/promo/{name}")
    def promo_media(name: str):
        offer_id = _offer_id_from_name(name)
        if offer_id is None:
            raise HTTPException(404, "promo card not found")
        blob = None
        try:
            conn = open_db(settings)
            try:
                blob = load_promo_blob(conn, offer_id)
            finally:
                conn.close()
        except Exception:
            blob = None
        if blob:
            return Response(content=blob, media_type="image/webp")
        path = media_root / "promo" / name
        if path.is_file():
            return FileResponse(path)
        raise HTTPException(404, "promo card not found")

    app.mount("/media", StaticFiles(directory=str(media_root)), name="media")
    return app


def __getattr__(name: str):
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)


def _prepare_storage(settings: Settings, generate_if_missing: bool) -> Settings:
    """SQLite file locally. Postgres when DATABASE_URL is set, seeded on an empty book."""
    if settings.database_url:
        if generate_if_missing:
            try:
                conn = open_db(settings)
                try:
                    seed_if_empty(conn)
                finally:
                    conn.close()
            except Exception:
                # The process still boots. /healthz returns 503 until the database answers.
                return settings
        return settings
    try:
        _prepare_sqlite(settings, generate_if_missing)
        return settings
    except OSError:
        from dataclasses import replace

        fallback = replace(
            settings,
            db_path=Path("/tmp/convoplex.sqlite"),
            log_path=Path("/tmp/convoplex-actions.jsonl"),
            output_dir=Path("/tmp/convoplex-output"),
        )
        _prepare_sqlite(fallback, generate_if_missing)
        return fallback


def _prepare_sqlite(settings: Settings, generate_if_missing: bool) -> None:
    if generate_if_missing and not settings.db_path.exists():
        generate(settings.db_path)
        return
    if not settings.db_path.exists():
        from convoplex.db import init_db

        init_db(settings.db_path)
        return
    conn = open_db(settings)
    conn.close()


def _ensure_output_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / "promo").mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path("/tmp/convoplex-output")
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "promo").mkdir(parents=True, exist_ok=True)
        return fallback


def _stamp(response) -> None:
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value


def _llm_mode_for_request(request: Request, task: str) -> str:
    current = request.app.state.settings
    try:
        return select_mode(current, admin=is_admin(request, current), task=task)
    except PromoCapError as exc:
        raise HTTPException(429, str(exc)) from exc


def _llm_for_request(request: Request, task: str):
    mode = _llm_mode_for_request(request, task)
    if mode == "live":
        return request.app.state.live_llm or build_llm(request.app.state.settings)
    return request.app.state.mock_llm


def _offer_id_from_name(name: str) -> int | None:
    prefix = "offer-"
    suffix = ".webp"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    raw = name[len(prefix) : -len(suffix)]
    if not raw.isdigit():
        return None
    return int(raw)


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
        "language_label": language_label(row.get("language")),
        "language_opted_in": bool(row.get("language_opted_in")),
        "language_opted_in_at": row.get("language_opted_in_at"),
        "language_channel": row.get("language_channel"),
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
    digest = payload.get("wamid_hash")
    status = statuses.get(digest) if digest else None
    if status is None:
        status = payload.get("status")
    return {
        "kind": payload.get("kind"),
        "status": status,
        "to_masked": payload.get("to_masked"),
        "wamid_hash": digest,
        "wamid_tail": payload.get("wamid_tail"),
        "note": redact_wamids(payload.get("note") or ""),
        "caption": redact_wamids(payload.get("caption") or ""),
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
