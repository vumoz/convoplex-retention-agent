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


def test_generate_clears_the_action_log(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "actions.jsonl"
    log_path.write_text('{"old": true}\n', encoding="utf-8")
    monkeypatch.setenv("CONVOPLEX_DB", str(tmp_path / "fresh.sqlite"))
    monkeypatch.setenv("CONVOPLEX_LOG", str(log_path))
    assert main(["generate", "--customers", "20", "--seed", "1"]) == 0
    assert "Wrote synthetic database" in capsys.readouterr().out
    assert not log_path.exists()


def test_scenario_replies_can_decline_without_an_order(tmp_path, monkeypatch, capsys):
    from convoplex.context import list_at_risk
    from convoplex.db import connect
    from convoplex.synthetic.generate import generate

    db_path = tmp_path / "scenario.sqlite"
    log_path = tmp_path / "scenario.jsonl"
    generate(db_path, n_customers=40, seed=3)
    conn = connect(db_path)
    ranked = [
        row
        for row in list_at_risk(conn, limit=40)
        if row.get("favourite_product") and row["favourite_product"]["stock_qty"] > 0
    ]
    customer_id = ranked[0]["id"]
    conn.close()
    monkeypatch.setenv("CONVOPLEX_DB", str(db_path))
    monkeypatch.setenv("CONVOPLEX_LOG", str(log_path))
    code = main(
        [
            "demo",
            "--mock",
            "--customer",
            str(customer_id),
            "--lang",
            "en",
            "--reply",
            "No thank you, maybe next month",
            "--reply",
            "How much is it?",
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "English" in printed
    assert "No order placed." in printed
    assert "Agent (decline):" in printed
    assert "Agent (question):" in printed
    assert "Order #" not in printed
    missing = main(["demo", "--mock", "--customer", "999999"])
    assert missing == 1
    assert "Unknown customer" in capsys.readouterr().err


def test_cli_voice_note_and_promo_card(tmp_path, monkeypatch, capsys):
    import io
    import struct
    import wave

    from convoplex.context import list_at_risk
    from convoplex.db import connect
    from convoplex.synthetic.generate import generate

    db_path = tmp_path / "voice.sqlite"
    log_path = tmp_path / "voice.jsonl"
    output = tmp_path / "output"
    generate(db_path, n_customers=40, seed=4)
    conn = connect(db_path)
    ranked = [
        row
        for row in list_at_risk(conn, limit=40)
        if row.get("favourite_product") and row["favourite_product"]["stock_qty"] > 0
    ]
    customer_id = ranked[0]["id"]
    conn.close()
    wav_path = tmp_path / "note.wav"
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * 160)
    wav_path.write_bytes(buffer.getvalue())
    monkeypatch.setenv("CONVOPLEX_DB", str(db_path))
    monkeypatch.setenv("CONVOPLEX_LOG", str(log_path))
    monkeypatch.setenv("CONVOPLEX_OUTPUT", str(output))
    code = main(
        ["demo", "--mock", "--customer", str(customer_id), "--voice", str(wav_path), "--promo-card"]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "Customer voice note (mock, 0 ms):" in printed
    assert "Yes o, no wahala" in printed
    assert "Promo card:" in printed
    assert "verify OK" in printed
    assert list((output / "promo").glob("*.webp"))
    missing = main(
        ["demo", "--mock", "--customer", str(customer_id), "--voice", str(tmp_path / "missing.ogg")]
    )
    assert missing == 1
    assert "Voice note not found" in capsys.readouterr().err


def test_cli_demo_and_verify(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONVOPLEX_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.setenv("CONVOPLEX_LOG", str(tmp_path / "cli.jsonl"))
    assert main(["demo", "--mock"]) == 0
    printed = capsys.readouterr().out
    assert "SYNTHETIC DATA" in printed
    assert "verify OK" in printed
    assert main(["verify"]) == 0
    assert "OK" in capsys.readouterr().out
