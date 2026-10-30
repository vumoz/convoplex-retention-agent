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
    "formal english": "en",
    "standard english": "en",
    "ingilishi": "en",
    "broken": "pcm",
    "broken english": "pcm",
    "pidgin": "pcm",
    "naija": "pcm",
    "igbo": "ig",
    "ibo": "ig",
    "asusu igbo": "ig",
    "igbo language": "ig",
    "yoruba": "yo",
    "ede yoruba": "yo",
    "yoruba language": "yo",
    "hausa": "ha",
    "hausa language": "ha",
    "harshen hausa": "ha",
}

# Ordinary words that sit one or two edits from a language name. Never fuzzy-match these.
_STOPWORDS = frozenset(
    {
        "yes",
        "rice",
        "house",
        "hello",
        "broke",
        "how",
        "much",
        "last",
        "price",
        "the",
        "bag",
        "shop",
        "send",
        "okay",
        "thanks",
        "thank",
        "you",
        "today",
        "now",
    }
)

# Canonical names used only for edit-distance. Exact aliases are checked first.
_FUZZY = (
    ("yoruba", "yo"),
    ("igbo", "ig"),
    ("hausa", "ha"),
    ("english", "en"),
    ("pidgin", "pcm"),
    ("broken", "pcm"),
    ("naija", "pcm"),
)

# Whole phrases and tokens a voice note often writes instead of the language name.
# "house a" is Hausa. Checked before the ordinary word "house", which is not.
_MISHEARD = {
    "ausa": "ha",
    "hausaa": "ha",
    "house a": "ha",
    "housea": "ha",
    "how sa": "ha",
    "yaruba": "yo",
    "yuroba": "yo",
    "your uba": "yo",
    "eegbo": "ig",
    "ig bo": "ig",
}

_SWITCH_CUES = frozenset(
    {"speak", "switch", "change", "talk", "use", "want", "language", "lang", "asusu", "ede", "yare"}
)

_PLEASE = re.compile(r"^(?:please |abeg |biko )+|(?: please| abeg| biko)+$")

_PIDGIN_WORD = re.compile(r"\bpidgin\b", re.IGNORECASE)

_REFUSAL = re.compile(
    r"\b(?:no sabi|don'?t speak|do not speak|cannot speak|can'?t speak|i no fit speak)\b"
    r".{0,40}\b(?:yoruba|igbo|ibo|hausa|english|broken english|pidgin)\b"
    r"|\b(?:yoruba|igbo|ibo|hausa|english|broken english|pidgin)\b.{0,40}"
    r"\b(?:no sabi|don'?t speak|do not speak|cannot speak|can'?t speak|i no fit speak)\b",
    re.IGNORECASE,
)

VOICE_RETRY = {
    "en": "I didn't catch that voice note. Please type it or send it again.",
    "pcm": "I no hear that voice note well. Abeg type am or send am again.",
    "ig": "Anụghị m voice note ah nke ọma. Biko dee ya ma ọ bụ zigharịa ya.",
    "yo": "Mi ò gbọ́ voice note náà kedere. Jọ̀wọ́ kọ ọ́ tàbí rán an padà.",
    "ha": "Ban ji voice note ɗin ba. Don Allah a rubuta shi ko a sake aikawa.",
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
    folded = _PLEASE.sub("", folded).strip()
    if not folded:
        return None
    if folded in MENU_PHRASES:
        return {"action": "menu"}
    coded = _MISHEARD.get(folded) or _ALIASES.get(folded)
    if coded:
        return {"action": "set", "language": coded}
    match = _CHANGE.match(folded)
    if match:
        name = match.group("name").strip()
        if name in MENU_PHRASES:
            return {"action": "menu"}
        picked = _ALIASES.get(name) or _fuzzy_language(name)
        if picked:
            return {"action": "set", "language": picked}
    words = folded.split()
    if 0 < len(words) <= 4:
        picked = _fuzzy_language(folded)
        if picked:
            return {"action": "set", "language": picked}
    return None


_EXTRA_ASK = re.compile(
    r"\b(how much|how many|how|what|when|where|why|which|price|stock|deliver|order|buy|cost|naira|send|picture|photo)\b",
    re.IGNORECASE,
)


def message_is_language_only(value: str | None) -> bool:
    """True when the customer is only asking to change language, not also about the shop."""
    if not (value or "").strip():
        return False
    if message_asks_something_else(value):
        return False
    choice = interpret_language(value)
    if choice and choice["action"] == "set":
        return True
    return requested_language(value) is not None


def message_asks_something_else(value: str | None) -> bool:
    """True when the same message also asks a shop question, not only for a language."""
    return bool(_EXTRA_ASK.search(value or ""))


def requested_language(value: str | None) -> str | None:
    """A language the customer is asking to switch to, including a longer sentence.

    Short voice mistakes are handled by interpret_language. A longer request such as
    "please talk to me in Yoruba from now on" still names one language next to a cue.
    """
    choice = interpret_language(value)
    if choice and choice["action"] == "set":
        return choice["language"]
    folded = _PLEASE.sub("", fold_language_text(value)).strip()
    words = folded.split()
    if not words or len(words) > 12:
        return None
    if not any(word in _SWITCH_CUES for word in words):
        return None
    found = None
    for index in range(len(words) - 1):
        pair = f"{words[index]} {words[index + 1]}"
        coded = _MISHEARD.get(pair) or _ALIASES.get(pair)
        if coded:
            found = coded
    for word in words:
        coded = _ALIASES.get(word) or _MISHEARD.get(word) or _fuzzy_language(word)
        if coded:
            found = coded
    return found


def explicit_language(value: str | None) -> str | None:
    """The language an explicit request names, including a voice mishearing.

    The target is chosen before a reply is written. A sentence that only
    contains 'broken' as an ordinary word is not a request.
    """
    choice = interpret_language(value)
    if choice and choice.get("action") == "set":
        return choice["language"]
    return requested_language(value)


def _fuzzy_language(name: str) -> str | None:
    """Small edit distance for a single short token or a 1–4 word utterance."""
    if not name or name in _STOPWORDS or name in MENU_PHRASES:
        return None
    if len(name) < 4 or len(name.split()) > 4:
        return None
    limit = 1 if len(name) <= 4 else 2
    best_code = None
    best_distance = limit + 1
    for target, code in _FUZZY:
        distance = _edit_distance(name, target)
        if distance < best_distance:
            best_code = code
            best_distance = distance
    if best_distance <= limit:
        return best_code
    return None


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


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


def scrub_customer_text(text: str) -> str:
    """Customer-facing copy says Broken English, never pidgin."""
    return _PIDGIN_WORD.sub("Broken English", text or "")


def guard_reply(text: str, language: str | None) -> str:
    """Drop a refusal to speak a supported language. The reply stays in that language."""
    cleaned = scrub_customer_text(text)
    if _REFUSAL.search(cleaned):
        try:
            code = clean_language(language)
        except ValueError:
            code = "en"
        return CONFIRM[code]
    return cleaned


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
