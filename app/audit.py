from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
AUDIT_FILE = BASE_DIR / "logs" / "audit.jsonl"
# 审计落盘后是否 fsync：政务场景要求「写入即可靠」，默认开启；高吞吐场景可用 AUDIT_FSYNC=0 关闭。
AUDIT_FSYNC = os.getenv("AUDIT_FSYNC", "1").strip().lower() not in {"0", "false", "no"}

_AUDIT_LOCK = threading.Lock()


def write_audit(record: dict, audit_file: Path | None = None) -> None:
    """
    追加一行 JSON 审计日志。

    - 时间带本机时区偏移，跨时区/容器 UTC 下仍能对齐业务时间；
    - 进程内加锁，避免 API 与 worker 多线程并发写入时行内交错；
    - 默认 fsync，保证进程崩溃/断电后已返回的问答仍有审计。

    留存、归档、防篡改、独立加密存储属于 P1。
    """
    target = Path(audit_file or AUDIT_FILE)
    payload = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with _AUDIT_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            if AUDIT_FSYNC:
                os.fsync(f.fileno())
