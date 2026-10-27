"""End-to-end win-back on the synthetic book.

Uses Muse Spark when META_API_KEY is set (or --live). Otherwise the deterministic mock.
"""

from __future__ import annotations

from dataclasses import dataclass

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import list_at_risk
from convoplex.db import SYNTHETIC_LABEL, connect, scenario_as_of
from convoplex.llm import LLMError, LiveMuse, MockMuse
from convoplex.metrics import snapshot
from convoplex.synthetic.generate import generate

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
    order_id: int
    naira_recovered: int
    win_back_rate: float
    offers_sent: int
    log_entries: int
    log_ok: bool
    llm_mode: str
    scenario_as_of: str


def run_demo(settings: Settings, *, reset_data: bool = False) -> DemoResult:
    if reset_data or not settings.db_path.exists():
        generate(settings.db_path)
        if settings.log_path.exists():
            settings.log_path.unlink()
    llm = LiveMuse(
        api_key=settings.meta_api_key,
        base_url=settings.meta_base_url,
        model=settings.chat_model,
    ) if settings.llm_mode == "live" else MockMuse()
    log = ActionLog(settings.log_path)
    conn = connect(settings.db_path)
    try:
        as_of = scenario_as_of(conn)
        chosen = _pick_customer(conn)
        win = draft_winback(conn, log, llm, chosen["id"], max_tokens=settings.max_tokens)
        if not win.offer or not win.offer.get("ok"):
            raise RuntimeError("Win-back did not create an offer. Check stock for the favourite product.")
        reply = handle_reply(
            conn,
            log,
            llm,
            chosen["id"],
            DEMO_REPLY,
            max_tokens=settings.max_tokens,
        )
        if reply.intent != "accept" or not reply.order or not reply.order.get("ok"):
            raise RuntimeError(f"Customer acceptance did not place an order (intent={reply.intent}).")
        totals = snapshot(conn)
        verified = log.verify()
        return DemoResult(
            customer_id=chosen["id"],
            customer_name=chosen["full_name"],
            language=chosen["language"],
            segment=chosen["segment"],
            score=chosen["score"],
            reasons=chosen["reasons"],
            winback_message=win.message,
            offer_id=int(win.offer["offer_id"]),
            offer_price_ngn=int(win.offer["offer_price_ngn"]),
            reply=DEMO_REPLY,
            intent=reply.intent or "",
            confirmation=reply.message,
            order_id=int(reply.order["order_id"]),
            naira_recovered=int(totals["naira_recovered"]),
            win_back_rate=float(totals["win_back_rate"]),
            offers_sent=int(totals["offers_sent"]),
            log_entries=verified.entries,
            log_ok=verified.ok,
            llm_mode=settings.llm_mode,
            scenario_as_of=as_of.isoformat(),
        )
    finally:
        conn.close()
        close = getattr(llm, "close", None)
        if close:
            close()


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
    return "\n".join(
        [
            "Convoplex Retention Agent",
            SYNTHETIC_LABEL,
            f"LLM: {result.llm_mode}",
            f"Scenario date: {result.scenario_as_of}",
            "",
            f"Customer: {result.customer_name} (id {result.customer_id}, Pidgin, {result.segment}, score {result.score})",
            "Why they are on the list:",
            reasons,
            "",
            "Win-back message:",
            result.winback_message,
            f"Offer #{result.offer_id} at ₦{result.offer_price_ngn:,}",
            "",
            f"Customer replies: {result.reply}",
            "Agent:",
            result.confirmation,
            f"Order #{result.order_id}",
            "",
            f"Metrics: {result.offers_sent} offer(s) sent, win-back rate {rate}, ₦{result.naira_recovered:,} recovered",
            f"Action log: {result.log_entries} entries, verify {'OK' if result.log_ok else 'FAILED'}",
        ]
    )
