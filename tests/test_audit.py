import json

from app import audit


def test_write_audit_shared_module(tmp_path):
    target = tmp_path / "audit.jsonl"
    audit.write_audit({"action": "upload", "user": "zhang"}, audit_file=target)
    record = json.loads(target.read_text(encoding="utf-8").strip())
    assert record["action"] == "upload"
    assert record["ts"]
