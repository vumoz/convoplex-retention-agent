import json

from convoplex.actionlog import GENESIS, ActionLog, compute_hash


def test_chain_links_and_detects_tampering(log_path):
    log = ActionLog(log_path)
    assert log.verify().ok is True
    assert log.verify().entries == 0
    first = log.append(kind="message", actor="system", payload={"text": "hello"}, ts="2026-10-27T00:00:00+00:00")
    second = log.append(kind="tool_call", actor="agent", payload={"name": "get_price"}, ts="2026-10-27T00:00:01+00:00")
    assert first["prev_hash"] == GENESIS
    assert second["prev_hash"] == first["hash"]
    assert first["hash"] == compute_hash(first)
    verified = log.verify()
    assert verified.ok is True
    assert verified.entries == 2

    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"] = {"text": "rewritten"}
    lines[0] = json.dumps(tampered)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    failed = log.verify()
    assert failed.ok is False
    assert "hash mismatch" in (failed.error or "")


def test_hash_covers_prev_hash_and_payload():
    base = {
        "seq": 1,
        "ts": "2026-10-27T00:00:00+00:00",
        "kind": "message",
        "actor": "system",
        "payload": {"text": "hello"},
        "prev_hash": GENESIS,
    }
    changed_link = {**base, "prev_hash": "1" * 64}
    changed_text = {**base, "payload": {"text": "goodbye"}}
    assert compute_hash(base) != compute_hash(changed_link)
    assert compute_hash(base) != compute_hash(changed_text)
    assert (
        compute_hash(base)
        == "3483db6ba2bc34b078cb0a9276a978156fe20093670a794ab2c492e2a0183956"
    )
