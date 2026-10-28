"""Write docs/language-samples.md.

Uses Muse Spark when META_API_KEY is set. Otherwise it records the deterministic
mock copy and says the live call was not made. The key is never printed.
"""

from __future__ import annotations

import os
from pathlib import Path

from convoplex.actionlog import ActionLog
from convoplex.config import Settings
from convoplex.context import agent_context
from convoplex.db import init_db
from convoplex.integrations.images import overlay_copy
from convoplex.integrations.voice import transcribe_request_fields
from convoplex.languages import CONFIRM, DOC_LABELS, HINT, LABELS
from convoplex.llm import MockMuse
from convoplex.messages import question_message, winback_message

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "language-samples.md"


def _shop(path: Path):
    conn = init_db(path)
    conn.execute(
        """
        INSERT INTO retailers (name, city, area, category_focus, phone)
        VALUES ('Mama Nkechi Provisions', 'Lagos', 'Ikeja', 'provisions', '+2348000001001')
        """
    )
    conn.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (1, 'RICE-50', 'Mama Gold Rice 50kg', 'provisions', 'bag', 78000, 20)
        """
    )
    conn.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language, archetype)
        VALUES (1, 'Chinedu Okonkwo', '+2348030001101', 'Lagos', 'Ikeja', 'en', 'lapsed')
        """
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('scenario_as_of', '2026-10-27')")
    conn.commit()
    return conn


def _mock_block(conn) -> list[str]:
    lines = [
        "## Deterministic mock copy",
        "",
        "This is what the offline agent sends when Muse is not called. "
        "It is a template, not a native-speaker review. Prices stay in ₦ and the product name is unchanged.",
        "",
    ]
    offer = {
        "offer_price_ngn": 70200,
        "list_price_ngn": 78000,
        "product_name": "Mama Gold Rice 50kg",
        "discount_percent": 10,
    }
    for code in ("en", "pcm", "ig", "yo", "ha"):
        ctx = agent_context(conn, 1, "winback", language=code)
        lines.append(f"### {DOC_LABELS[code]} (`{code}`)")
        lines.append("")
        lines.append("Win-back:")
        lines.append("")
        lines.append(winback_message(ctx, offer))
        lines.append("")
        lines.append("Reply to a price question:")
        lines.append("")
        lines.append(question_message(ctx, "Mama Gold Rice 50kg", 70200, 20))
        lines.append("")
        lines.append("Switch confirmation:")
        lines.append("")
        lines.append(CONFIRM[code])
        lines.append("")
        card = overlay_copy(
            {
                "retailer_name": "Mama Nkechi Provisions",
                "city": "Lagos",
                "product_name": "Mama Gold Rice 50kg",
                "offer_price_ngn": 70200,
                "list_price_ngn": 78000,
                "discount_percent": 10,
                "language": code,
            }
        )
        lines.append(f"Promo overlay: {card['off']} · {card['price']} · {card['cta']}")
        lines.append("")
    lines.append(f"First-contact hint (English, once): {HINT}")
    lines.append("")
    return lines


def _live_block() -> list[str]:
    key = (os.environ.get("META_API_KEY") or "").strip()
    if not key:
        return [
            "## Live Muse Spark",
            "",
            "Not run. `META_API_KEY` was not set in this environment, so no model call was made "
            "and nothing below should be read as Muse output. Re-run `python scripts/language_samples.py` "
            "on a machine that has the key. The script keeps the spend to one win-back and one price-question "
            "reply per language.",
            "",
        ]
    from convoplex.agent import draft_winback, handle_reply
    from convoplex.llm import LiveMuse

    folder = ROOT / "output" / "language-samples"
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "shop.sqlite"
    if db.exists():
        db.unlink()
    conn = _shop(db)
    log = ActionLog(folder / "actions.jsonl")
    settings = Settings(
        db_path=db,
        log_path=folder / "actions.jsonl",
        output_dir=folder,
        llm_mode="live",
        meta_api_key=key,
    )
    llm = LiveMuse(api_key=key, base_url=settings.meta_base_url, model=settings.chat_model)
    lines = [
        "## Live Muse Spark",
        "",
        "One synthetic customer, Mama Gold Rice 50kg, offer produced by the tool loop. "
        "Victor should correct any Igbo, Yoruba, or Hausa that sounds stiff, mixed, or wrong. "
        "The key is not stored here.",
        "",
    ]
    try:
        for code in ("en", "pcm", "ig", "yo", "ha"):
            conn.execute("DELETE FROM order_items")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM offers")
            conn.execute(
                "UPDATE customers SET language = ?, language_opted_in = 1, language_channel = 'seed' WHERE id = 1",
                (code,),
            )
            conn.commit()
            if log.path.exists():
                log.path.unlink()
            win = draft_winback(conn, log, llm, 1, max_tokens=settings.max_tokens, language=code)
            reply = handle_reply(
                conn,
                log,
                llm,
                1,
                "How much is it?",
                max_tokens=settings.max_tokens,
                language=code,
            )
            lines.append(f"### {DOC_LABELS[code]} (`{code}`)")
            lines.append("")
            lines.append("Win-back:")
            lines.append("")
            lines.append(win.message)
            lines.append("")
            lines.append("Reply:")
            lines.append("")
            lines.append(reply.message)
            lines.append("")
    finally:
        conn.close()
    return lines


def _voice_block() -> list[str]:
    fields = transcribe_request_fields(b"RIFF", "muse-voice-transcribe-1.0")
    request = fields["request"][1]
    return [
        "## Muse Voice",
        "",
        "Not probed with audio. The transcribe request this repo sends has no language field:",
        "",
        "```json",
        request,
        "```",
        "",
        "It is `model`, `audioEncoding` `WAV`, and `mode` `PUSH_TO_TALK`, plus the wav file. "
        "There is no `ig` / `yo` / `ha` / `pcm` switch in that body, and this run did not have "
        "`META_API_KEY` or a legally generated speech clip to upload. A tone or silence would not "
        "show whether those languages are recognized. Until a short synthetic clip is sent with the key, "
        "treat voice notes in Igbo, Yoruba, Hausa, and Broken English as unverified. "
        "The transcript, whatever language it comes back in, is then handled in the customer's saved language.",
        "",
    ]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    conn = _shop(Path("/tmp/language-sample-shop.sqlite"))
    try:
        parts = [
            "# Language samples",
            "",
            "Customer-facing name for `pcm` is Broken English. This note says Broken English (Nigerian Pidgin).",
            "",
            "Labels: " + ", ".join(f"{LABELS[code]} (`{code}`)" for code in LABELS) + ".",
            "",
            * _voice_block(),
            * _live_block(),
            * _mock_block(conn),
            "## What to watch",
            "",
            "The Igbo, Yoruba, and Hausa lines were drafted as short shop messages, not by a native editor. "
            "Tone, diacritics, and word choice may be off. Product names and ₦ amounts are copied from the catalogue on purpose. "
            "A reply should stay in one language unless the customer mixes languages first.",
            "",
        ]
    finally:
        conn.close()
    # The mock model is imported so a future edit can swap in a dry run without a network call.
    MockMuse()
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
