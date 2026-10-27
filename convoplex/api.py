"""FastAPI surface for the retention agent.

Voice notes, promo cards, and the WhatsApp webhook are stubbed. Their routes
validate the contract and point at the clients in convoplex.integrations.
"""

from __future__ import annotations

import json
from datetime import date

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from convoplex import __version__
from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import get_customer, list_at_risk
from convoplex.db import SYNTHETIC_LABEL, connect, scenario_as_of
from convoplex.integrations.images import image_request_body, load_offer_for_card, promo_prompt
from convoplex.integrations.voice import ffmpeg_to_wav_command
from convoplex.integrations.whatsapp import parse_inbound, verify_subscription
from convoplex.llm import LLMError, LiveMuse, MockMuse
from convoplex.metrics import snapshot
from convoplex.synthetic.generate import generate


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


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

    app = FastAPI(
        title="Convoplex Retention Agent",
        version=__version__,
        summary="Win back lapsed customers for Nigerian retailers.",
        description=(
            "Synthetic-data core built during the Meta AI Developer Hackathon, "
            "27 Oct to 4 Nov 2026. Muse Spark drafts WhatsApp-style win-backs. "
            "Voice, promo images, and WhatsApp delivery are stubbed."
        ),
    )
    app.state.settings = settings
    app.state.log = ActionLog(settings.log_path)
    app.state.llm = build_llm(settings)

    def get_settings(request: Request) -> Settings:
        return request.app.state.settings

    def get_conn(current: Settings = Depends(get_settings)):
        conn = connect(current.db_path)
        try:
            yield conn
        finally:
            conn.close()

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
            "meta_key_set": bool(settings.meta_api_key),
            "scenario_as_of": as_of,
            "synthetic": True,
            "label": label,
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
        return snapshot(conn)

    @app.get("/v1/actions")
    def actions(limit: int = Query(100, ge=1, le=1000)):
        entries = app.state.log.read_all()
        return {"entries": entries[-limit:], "count": len(entries)}

    @app.post("/v1/actions/verify")
    def verify_actions():
        result = app.state.log.verify()
        return {"ok": result.ok, "entries": result.entries, "error": result.error, "seq": result.seq}

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
                    "status": "stub",
                    "message": "Set WHATSAPP_VERIFY_TOKEN to practise the Cloud API handshake. Sending is not enabled.",
                },
            )
        challenge = verify_subscription(hub_mode, hub_verify_token, hub_challenge, expected)
        if challenge is None:
            raise HTTPException(403, "verification failed")
        return PlainTextResponse(challenge)

    @app.post("/v1/whatsapp/webhook")
    async def whatsapp_receive(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, "webhook body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "webhook body must be a JSON object")
        inbound = parse_inbound(payload)
        return {
            "status": "stub",
            "processed": False,
            "received": len(inbound),
            "messages": [
                {
                    "message_id": item.message_id,
                    "from": item.from_phone,
                    "kind": item.kind,
                    "text": item.text,
                    "media_id": item.media_id,
                }
                for item in inbound
            ],
            "message": "Webhook parsed. Live replies and sends are a later hackathon day.",
        }

    @app.post("/v1/voice/transcribe")
    async def voice_transcribe(
        request: str = Form(...),
        audio: UploadFile = File(...),
    ):
        try:
            body = json.loads(request)
        except json.JSONDecodeError:
            raise HTTPException(400, "request part must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "request part must be a JSON object")
        raw = await audio.read()
        filename = (audio.filename or "").lower()
        content_type = (audio.content_type or "").lower()
        is_ogg = filename.endswith(".ogg") or "ogg" in content_type
        is_wav = filename.endswith(".wav") or "wav" in content_type or raw[:4] == b"RIFF"
        if not is_ogg and not is_wav:
            raise HTTPException(400, "audio must be WAV or OGG")
        return JSONResponse(
            status_code=501,
            content={
                "status": "stub",
                "validated": True,
                "upstream": f"{settings.meta_base_url}/asr/transcribe",
                "model": body.get("model") or settings.asr_model,
                "conversion_required": bool(is_ogg),
                "ffmpeg": ffmpeg_to_wav_command("in.ogg", "out.wav") if is_ogg else None,
                "bytes": len(raw),
                "message": (
                    "Voice-note replies are stubbed for a later hackathon day. "
                    "The multipart shape was validated. convoplex.integrations.voice is ready to call the API."
                ),
            },
        )

    @app.post("/v1/offers/{offer_id}/promo-card")
    def promo_card(offer_id: int, conn=Depends(get_conn)):
        offer = load_offer_for_card(conn, offer_id)
        if offer is None:
            raise HTTPException(404, "offer not found")
        prompt = promo_prompt(offer)
        return JSONResponse(
            status_code=501,
            content={
                "status": "stub",
                "validated": True,
                "upstream": f"{settings.meta_base_url}/images/generations",
                "request": image_request_body(prompt, settings.image_model),
                "message": (
                    "Promo cards are stubbed for a later hackathon day. "
                    "convoplex.integrations.images will decode data[0].b64_json as WEBP."
                ),
            },
        )

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
