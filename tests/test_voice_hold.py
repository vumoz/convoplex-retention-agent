"""A voice note that hears 'wan' as 'won't' must not cancel the offer."""

from convoplex.actionlog import ActionLog
from convoplex.agent import draft_winback, handle_reply
from convoplex.llm import Completion, MockMuse, classify_reply, purchase_is_ambiguous


class _DeclineModel:
    """The live model heard 'wan' as 'won't' and tried to cancel."""

    mode = "mock"

    def complete(self, messages, *, tools, max_tokens, reasoning_effort=None, retry_on_length=True):
        del messages, tools, max_tokens, reasoning_effort, retry_on_length
        return Completion(content="I don cancel rice.\nINTENT: decline", finish_reason="stop")

# The live muse-voice-transcribe-1.0 transcript of
# "I wan buy rice, Mama Gold, talk to me in pidgin English".
TRANSCRIPT = "I won't buy rice. Mama, goods talk to me for Pidgin English."


def test_the_live_wont_buy_transcript_is_not_a_decline():
    assert purchase_is_ambiguous(TRANSCRIPT)
    assert purchase_is_ambiguous("I wan buy rice")
    assert purchase_is_ambiguous("I wanna get the bag")
    assert purchase_is_ambiguous("I wont take it")
    assert classify_reply(TRANSCRIPT) == "question"
    assert classify_reply("No thank you, maybe next month") == "decline"
    assert not purchase_is_ambiguous("No thank you, maybe next month")


def test_voice_transcript_keeps_the_rice_offer_and_asks(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    draft_winback(shop, log, llm, 1, max_tokens=1000)
    result = handle_reply(
        shop,
        log,
        llm,
        1,
        TRANSCRIPT,
        max_tokens=1000,
        from_voice=True,
    )
    assert result.intent == "question"
    assert result.order is None
    assert result.language == "pcm"
    assert result.message == "You wan buy am abi you no want am? Mama Gold Rice 50kg na ₦70,200."
    assert "cancel" not in result.message.lower()
    assert "pidgin" not in result.message.lower()
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "sent"


def test_a_model_decline_on_this_transcript_does_not_release_the_hold(shop, log_path):
    log = ActionLog(log_path)
    draft_winback(shop, log, MockMuse(), 1, max_tokens=1000)
    result = handle_reply(
        shop,
        log,
        _DeclineModel(),
        1,
        TRANSCRIPT,
        max_tokens=1000,
        from_voice=True,
    )
    assert result.intent == "question"
    assert result.order is None
    assert "cancel" not in result.message.lower()
    assert result.message == "You wan buy am abi you no want am? Mama Gold Rice 50kg na ₦70,200."
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "sent"


def test_typed_pidgin_wont_buy_also_holds_the_offer(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    draft_winback(shop, log, llm, 1, max_tokens=1000)
    result = handle_reply(shop, log, llm, 1, "I wan buy rice", max_tokens=1000)
    assert result.intent == "question"
    assert result.order is None
    assert "You wan buy am abi you no want am?" in result.message
    assert "₦70,200" in result.message
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 1").fetchone()["status"] == "sent"


def test_typed_english_decline_still_releases_the_offer(shop, log_path):
    log = ActionLog(log_path)
    llm = MockMuse()
    draft_winback(shop, log, llm, 2, max_tokens=1000, language="en")
    result = handle_reply(
        shop,
        log,
        llm,
        2,
        "No thank you, maybe next month",
        max_tokens=1000,
    )
    assert result.intent == "decline"
    assert result.order is None
    assert "Do you want to buy it" not in result.message
    assert shop.execute("SELECT status FROM offers WHERE customer_id = 2").fetchone()["status"] == "declined"
