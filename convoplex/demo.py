"""End-to-end win-back on the synthetic book.

Uses Muse Spark when META_API_KEY is set (or --live). Otherwise the deterministic mock.
"""

from __future__ import annotations

from dataclasses import dataclass

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.config import Settings
from convoplex.context import get_customer, list_at_risk
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
    order_id: int | None
    turns: list
    naira_recovered: int
    win_back_rate: float
    offers_sent: int
    log_entries: int
    log_ok: bool
    llm_mode: str
    scenario_as_of: str


def run_demo(
    settings: Settings,
    *,
    reset_data: bool = False,
    customer_id: int | None = None,
    language: str | None = None,
    replies: list[str] | None = None,
) -> DemoResult:
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
        if customer_id is None:
            chosen = _pick_customer(conn)
        else:
            chosen = get_customer(conn, customer_id)
            if chosen is None:
                raise LookupError(f"Unknown customer {customer_id}")
        scripted = replies is None
        reply_list = [DEMO_REPLY] if scripted else list(replies)
        if not reply_list:
            raise ValueError("Pass at least one --reply.")
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
            last = handle_reply(
                conn,
                log,
                llm,
                chosen["id"],
                text,
                max_tokens=settings.max_tokens,
                language=language,
            )
            placed = int(last.order["order_id"]) if last.order and last.order.get("ok") else None
            turns.append(
                {
                    "reply": text,
                    "intent": last.intent or "",
                    "message": last.message,
                    "order_id": placed,
                }
            )
        if scripted and (last.intent != "accept" or not last.order or not last.order.get("ok")):
            raise RuntimeError(f"Customer acceptance did not place an order (intent={last.intent}).")
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
        lines.append(f"Customer replies: {turn['reply']}")
        lines.append(f"Agent ({turn['intent']}):")
        lines.append(turn["message"])
        if turn["order_id"]:
            lines.append(f"Order #{turn['order_id']}")
        else:
            lines.append("No order placed.")
        lines.append("")
    lines.append(
        f"Metrics: {result.offers_sent} offer(s) sent, win-back rate {rate}, ₦{result.naira_recovered:,} recovered"
    )
    lines.append(
        f"Action log: {result.log_entries} entries, verify {'OK' if result.log_ok else 'FAILED'}"
    )
    return "\n".join(lines)
