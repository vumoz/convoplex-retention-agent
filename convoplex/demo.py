"""End-to-end win-back on the synthetic book.

Uses Muse Spark when META_API_KEY is set (or --live). Otherwise the deterministic mock.
"""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

from convoplex.actionlog import ActionLog, open_action_log
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import get_customer, list_at_risk
from convoplex.db import SYNTHETIC_LABEL, open_db, scenario_as_of
from convoplex.integrations.images import (
    build_promo_image,
    clear_promo_cards,
    load_offer_for_card,
    remember_promo,
)
from convoplex.integrations.voice import transcribe_audio
from convoplex.llm import LLMError, LiveMuse, MockMuse
from convoplex.metrics import snapshot
from convoplex.synthetic.generate import generate, reset_and_seed, seed_if_empty

DEMO_REPLY = "Yes o, no wahala. Abeg send am today."


@dataclass
class DemoResult:
    customer_id: int
    customer_name: str
    language: str
    segment: str
    score: float
    reasons: list[str]
    winback_message: str
    offer_id: int
    offer_price_ngn: int
    reply: str
    intent: str
    confirmation: str
    order_id: int | None
    turns: list
    naira_recovered: int
    win_back_rate: float
    offers_sent: int
    log_entries: int
    log_ok: bool
    llm_mode: str
    scenario_as_of: str
    voice_transcript: str | None = None
    voice_model: str | None = None
    voice_latency_ms: int | None = None
    promo_path: str | None = None
    promo_url: str | None = None


def run_demo(
    settings: Settings,
    *,
    reset_data: bool = False,
    customer_id: int | None = None,
    language: str | None = None,
    replies: list[str] | None = None,
    voice_path: str | None = None,
    promo_card: bool = False,
) -> DemoResult:
    if settings.database_url:
        holder = open_db(settings)
        try:
            if reset_data:
                reset_and_seed(holder, postgres=True)
                open_action_log(settings).clear()
                try:
                    clear_promo_cards(settings.output_dir)
                except OSError:
                    pass
            else:
                seed_if_empty(holder)
        finally:
            holder.close()
    elif reset_data or not settings.db_path.exists():
        generate(settings.db_path)
        if settings.log_path.exists():
            settings.log_path.unlink()
        clear_promo_cards(settings.output_dir)
    llm = LiveMuse(
        api_key=settings.meta_api_key,
        base_url=settings.meta_base_url,
        model=settings.chat_model,
    ) if settings.llm_mode == "live" else MockMuse()
    log = open_action_log(settings)
    conn = open_db(settings)
    try:
        as_of = scenario_as_of(conn)
        if customer_id is None:
            chosen = _pick_customer(conn)
        else:
            chosen = get_customer(conn, customer_id)
            if chosen is None:
                raise LookupError(f"Unknown customer {customer_id}")
        scripted = replies is None and not voice_path
        reply_list = [DEMO_REPLY] if scripted else list(replies or [])
        if not reply_list and not voice_path:
            raise ValueError("Pass at least one --reply or a --voice note.")
        win = draft_winback(
            conn,
            log,
            llm,
            chosen["id"],
            max_tokens=settings.max_tokens,
            language=language,
        )
        if not win.offer or not win.offer.get("ok"):
            raise RuntimeError("Win-back did not create an offer. Check stock for the favourite product.")
        turns: list[dict] = []
        last = None
        for text in reply_list:
            last = _reply_turn(
                conn, log, llm, chosen["id"], text, max_tokens=settings.max_tokens, language=language
            )
            turns.append(last)
        voice_note = None
        if voice_path:
            voice_note = _transcribe_voice(settings, log, chosen["id"], voice_path)
            last = _reply_turn(
                conn,
                log,
                llm,
                chosen["id"],
                voice_note.text,
                max_tokens=settings.max_tokens,
                language=language,
                via="voice",
                voice_model=voice_note.model,
                voice_latency_ms=voice_note.latency_ms,
            )
            turns.append(last)
        if scripted and (last["intent"] != "accept" or not last.get("order_id")):
            raise RuntimeError(f"Customer acceptance did not place an order (intent={last['intent']}).")
        promo_path = None
        promo_url = None
        if promo_card:
            promo_path, promo_url = _write_promo_card(settings, conn, log, int(win.offer["offer_id"]))
        totals = snapshot(conn, log.read_all())
        verified = log.verify()
        order_id = next((turn["order_id"] for turn in reversed(turns) if turn["order_id"]), None)
        return DemoResult(
            customer_id=chosen["id"],
            customer_name=chosen["full_name"],
            language=win.language,
            segment=chosen["segment"],
            score=chosen["score"],
            reasons=chosen["reasons"],
            winback_message=win.message,
            offer_id=int(win.offer["offer_id"]),
            offer_price_ngn=int(win.offer["offer_price_ngn"]),
            reply=turns[-1]["reply"],
            intent=turns[-1]["intent"],
            confirmation=turns[-1]["message"],
            order_id=order_id,
            turns=turns,
            naira_recovered=int(totals["naira_recovered"]),
            win_back_rate=float(totals["win_back_rate"]),
            offers_sent=int(totals["offers_sent"]),
            log_entries=verified.entries,
            log_ok=verified.ok,
            llm_mode=settings.llm_mode,
            scenario_as_of=as_of.isoformat(),
            voice_transcript=None if voice_note is None else voice_note.text,
            voice_model=None if voice_note is None else voice_note.model,
            voice_latency_ms=None if voice_note is None else voice_note.latency_ms,
            promo_path=promo_path,
            promo_url=promo_url,
        )
    finally:
        conn.close()
        close = getattr(llm, "close", None)
        if close:
            close()


def _reply_turn(conn, log, llm, customer_id, text, *, max_tokens, language, via="text", voice_model=None, voice_latency_ms=None):
    last = handle_reply(
        conn,
        log,
        llm,
        customer_id,
        text,
        max_tokens=max_tokens,
        language=language,
    )
    placed = int(last.order["order_id"]) if last.order and last.order.get("ok") else None
    return {
        "reply": text,
        "intent": last.intent or "",
        "message": last.message,
        "order_id": placed,
        "via": via,
        "voice_model": voice_model,
        "voice_latency_ms": voice_latency_ms,
    }


def _usage_zeros() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


def _transcribe_voice(settings: Settings, log: ActionLog, customer_id: int, voice_path: str):
    path = Path(voice_path)
    if not path.is_file():
        raise ValueError(f"Voice note not found: {path}")
    spoken = transcribe_audio(
        path.read_bytes(),
        filename=path.name,
        content_type="",
        mode=settings.llm_mode,
        api_key=settings.meta_api_key,
        base_url=settings.meta_base_url,
        model=settings.asr_model,
    )
    log.append(
        kind="model_call",
        actor="agent",
        payload={
            "customer_id": customer_id,
            "task": "transcribe",
            "model": spoken.model,
            "transcript": spoken.text,
            "latency_ms": spoken.latency_ms,
            "audio_format": spoken.source_format,
            "audio_duration_ms": spoken.audio_duration_ms,
            "finish_reason": "stop",
            "attempt": 1,
            "max_tokens": None,
            "usage": _usage_zeros(),
        },
    )
    return spoken


def _write_promo_card(settings: Settings, conn, log: ActionLog, offer_id: int) -> tuple[str, str]:
    offer = load_offer_for_card(conn, offer_id)
    if offer is None:
        raise RuntimeError(f"Offer {offer_id} disappeared before the promo card could be drawn.")
    card = build_promo_image(
        offer,
        mode=settings.llm_mode,
        api_key=settings.meta_api_key,
        base_url=settings.meta_base_url,
        model=settings.image_model,
    )
    path = remember_promo(conn, card.image, settings.output_dir, offer_id)
    url = f"/media/promo/{path.name}"
    log.append(
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
    return str(path), url


def _pick_customer(conn) -> dict:
    rows = list_at_risk(conn, limit=500)
    ranked = [
        row
        for row in rows
        if row["language"] == "pcm"
        and row["segment"] == "lapsed"
        and row.get("favourite_product")
        and row["favourite_product"]["stock_qty"] > 0
    ]
    if not ranked:
        raise RuntimeError("No lapsed Pidgin-speaking customer with stock on hand. Regenerate the database.")
    # Equal lapse scores are common at the top of the list. Call the customer
    # who has spent the most, so the offer is the valuable reorder, not an
    # arbitrary id.
    ranked.sort(key=lambda row: (-row["score"], -row["monetary_ngn"], row["id"]))
    return ranked[0]


def format_demo(result: DemoResult) -> str:
    reasons = "\n".join(f"  - {reason}" for reason in result.reasons)
    rate = f"{result.win_back_rate:.0%}"
    spoken = "Pidgin" if result.language == "pcm" else "English"
    lines = [
        "Convoplex Retention Agent",
        SYNTHETIC_LABEL,
        f"LLM: {result.llm_mode}",
        f"Scenario date: {result.scenario_as_of}",
        "",
        (
            f"Customer: {result.customer_name} "
            f"(id {result.customer_id}, {spoken}, {result.segment}, score {result.score})"
        ),
        "Why they are on the list:",
        reasons,
        "",
        "Win-back message:",
        result.winback_message,
        f"Offer #{result.offer_id} at ₦{result.offer_price_ngn:,}",
        "",
    ]
    for turn in result.turns:
        if turn.get("via") == "voice":
            lines.append(
                f"Customer voice note ({turn.get('voice_model')}, {turn.get('voice_latency_ms')} ms): {turn['reply']}"
            )
        else:
            lines.append(f"Customer replies: {turn['reply']}")
        lines.append(f"Agent ({turn['intent']}):")
        lines.append(turn["message"])
        if turn["order_id"]:
            lines.append(f"Order #{turn['order_id']}")
        else:
            lines.append("No order placed.")
        lines.append("")
    if result.promo_path:
        lines.append(f"Promo card: {result.promo_path}")
        lines.append("")
    lines.append(
        f"Metrics: {result.offers_sent} offer(s) sent, win-back rate {rate}, ₦{result.naira_recovered:,} recovered"
    )
    lines.append(
        f"Action log: {result.log_entries} entries, verify {'OK' if result.log_ok else 'FAILED'}"
    )
    return "\n".join(lines)
