"""Victor's Broken English notes are in the reply prompt and the doc."""

from pathlib import Path

from convoplex.prompts import _RULES

_DOC = Path(__file__).resolve().parent.parent / "docs" / "broken-english-notes.md"


def test_reply_prompt_has_the_glossary_and_the_bereavement_rule():
    rules = _RULES.lower()
    assert "broken english (nigerian pidgin) glossary" in rules
    assert "i wan buy" in rules
    assert "wan = want" in rules
    assert "abeg = please" in rules
    assert "wahala = trouble" in rules
    assert "sabi = know" in rules
    assert "chop = eat" in rules
    assert "how much e be" in rules
    assert "kpai" in rules
    assert "e don kpai" in rules
    assert "don die" in rules
    assert "sympathy" in rules
    assert "do not push the offer" in rules
    assert "the only name you may use in a message is broken english" in rules


def test_broken_english_notes_match_the_prompt():
    text = _DOC.read_text(encoding="utf-8").lower()
    assert "native speaker" in text
    assert "victor" in text
    assert "i wan buy" in text
    assert "e don kpai" in text
    assert "abeg" in text and "please" in text
    assert "wahala" in text and "trouble" in text
    assert "sabi" in text and "know" in text
    assert "chop" in text and "eat" in text
    assert "how much e be" in text
    assert "sympathy" in text
    assert "do not push the offer" in text
    assert "founder" not in text
    assert "owner" not in text
