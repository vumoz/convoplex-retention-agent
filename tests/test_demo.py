from convoplex.__main__ import main
from convoplex.config import Settings
from convoplex.demo import run_demo


def test_mock_demo_end_to_end(tmp_path):
    settings = Settings(
        db_path=tmp_path / "demo.sqlite",
        log_path=tmp_path / "actions.jsonl",
        llm_mode="mock",
    )
    result = run_demo(settings)
    assert result.llm_mode == "mock"
    assert result.language == "pcm"
    assert result.segment == "lapsed"
    assert "dey" in result.winback_message.lower()
    assert "₦" in result.winback_message
    assert result.intent == "accept"
    assert result.order_id > 0
    assert result.naira_recovered >= result.offer_price_ngn
    assert result.offers_sent == 1
    assert result.win_back_rate == 1.0
    assert result.log_ok is True
    assert result.log_entries > 0
    assert "SYNTHETIC" not in result.winback_message


def test_cli_demo_and_verify(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONVOPLEX_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.setenv("CONVOPLEX_LOG", str(tmp_path / "cli.jsonl"))
    assert main(["demo", "--mock"]) == 0
    printed = capsys.readouterr().out
    assert "SYNTHETIC DATA" in printed
    assert "verify OK" in printed
    assert main(["verify"]) == 0
    assert "OK" in capsys.readouterr().out
