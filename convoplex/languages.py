"""Customer language codes and WhatsApp opt-in.

English (`en`) is the default when a customer has not chosen. `pcm` is what
customers see as Broken English. Docs that face a judge can say Broken English
(Nigerian Pidgin). The word "Pidgin" is not used on the dashboard or in a reply.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

LANGUAGES = ("en", "pcm", "ig", "yo", "ha")

# Customer-facing labels. Do not say "Pidgin" here.
LABELS = {
    "en": "English",
    "pcm": "Broken English",
    "ig": "Igbo",
    "yo": "Yoruba",
    "ha": "Hausa",
}

# README and other judge-facing copy.
DOC_LABELS = {
    "en": "English",
    "pcm": "Broken English (Nigerian Pidgin)",
    "ig": "Igbo",
    "yo": "Yoruba",
    "ha": "Hausa",
}

HINT = (
    "Reply LANGUAGE any time to pick English, Broken English, Igbo, Yoruba, or Hausa."
)

CONFIRM = {
    "en": "I'll use English from here. Reply LANGUAGE if you want a different one.",
    "pcm": "I go use Broken English from here. Reply LANGUAGE if you want another one.",
    "ig": "Aga m eji Igbo eme n'ihu. Zaa LANGUAGE ma ịchọrọ asụsụ ọzọ.",
    "yo": "Màá máa lo Yorùbá láti ìsinsìnyí. Dáhùn LANGUAGE tí o bá fẹ́ èdè mìíràn.",
    "ha": "Zan yi amfani da Hausa daga yanzu. Amsa LANGUAGE idan kana son wani yare.",
}

# Characters the promo card and these replies must be able to draw.
GLYPHS = "ọẹṣụịńɓɗƙƴ₦ÈèáéíóúÁÉÍÓÚ"

MENU_PHRASES = frozenset(
    {
        "language",
        "lang",
        "languages",
        "change language",
        "switch language",
        "choose language",
        "pick language",
    }
)

_ALIASES = {
    "english": "en",
    "ingilishi": "en",
    "broken": "pcm",
    "broken english": "pcm",
    "pidgin": "pcm",
    "naija": "pcm",
    "igbo": "ig",
    "asusu igbo": "ig",
    "yoruba": "yo",
    "ede yoruba": "yo",
    "hausa": "ha",
    "harshen hausa": "ha",
}

_CHANGE = re.compile(
    r"^(?:please |abeg |biko )?"
    r"(?:change|switch|set|use|speak|i want|want|make am)"
    r"(?: language| my language)?(?: to)? (?P<name>.+)$"
)


def clean_language(value: str | None) -> str:
    code = (value or "").strip().lower()
    if code not in LANGUAGES:
        raise ValueError("language must be one of en, pcm, ig, yo, or ha")
    return code


def language_label(code: str | None) -> str:
    try:
        return LABELS[clean_language(code)]
    except ValueError:
        return LABELS["en"]


def fold_language_text(value: str | None) -> str:
    """Lowercase, strip accents, and collapse punctuation so 'Yorùbá' matches 'yoruba'."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _alias(name: str) -> str | None:
    folded = fold_language_text(name)
    if folded in MENU_PHRASES:
        return None
    return _ALIASES.get(folded)


def interpret_language(value: str | None) -> dict | None:
    """A language switch, a request for the menu, or None when this is a normal message.

    Matching is on the whole message, or on a short 'change language to …' command.
    A longer sentence that merely contains a word like 'broken' is left alone.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("lang_"):
        code = raw.removeprefix("lang_")
        if code in LANGUAGES:
            return {"action": "set", "language": code}
    folded = fold_language_text(raw)
    if not folded:
        return None
    if folded in MENU_PHRASES:
        return {"action": "menu"}
    coded = _ALIASES.get(folded)
    if coded:
        return {"action": "set", "language": coded}
    match = _CHANGE.match(folded)
    if match:
        name = match.group("name").strip()
        if name in MENU_PHRASES:
            return {"action": "menu"}
        picked = _ALIASES.get(name)
        if picked:
            return {"action": "set", "language": picked}
    return None


def language_list_interactive() -> dict:
    """WhatsApp interactive list. Row titles stay within the 24-character limit."""
    rows = [
        {"id": "lang_en", "title": "English", "description": "Nigeria's official language"},
        {"id": "lang_pcm", "title": "Broken English", "description": "How many of us talk"},
        {"id": "lang_ig", "title": "Igbo", "description": "Asụsụ Igbo"},
        {"id": "lang_yo", "title": "Yoruba", "description": "Èdè Yorùbá"},
        {"id": "lang_ha", "title": "Hausa", "description": "Harshen Hausa"},
    ]
    return {
        "type": "list",
        "body": {
            "text": "Pick a language. You can change it any time by replying with its name.",
        },
        "action": {"button": "Languages", "sections": [{"title": "Languages", "rows": rows}]},
    }


def save_language(conn, customer_id: int, language: str, *, channel: str, when: str | None = None) -> str:
    code = clean_language(language)
    stamp = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    conn.execute(
        """
        UPDATE customers
        SET language = ?,
            language_opted_in = 1,
            language_opted_in_at = ?,
            language_channel = ?
        WHERE id = ?
        """,
        (code, stamp, channel, customer_id),
    )
    conn.commit()
    return code


def take_language_hint(conn, customer_id: int) -> str | None:
    """The one-line hint for a customer who is still on default English. Empty after the first time."""
    row = conn.execute(
        """
        SELECT language, language_opted_in, language_hint_sent
        FROM customers WHERE id = ?
        """,
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    if row["language"] != "en" or int(row["language_opted_in"] or 0) or int(row["language_hint_sent"] or 0):
        return None
    conn.execute(
        "UPDATE customers SET language_hint_sent = 1 WHERE id = ?",
        (customer_id,),
    )
    conn.commit()
    return HINT
