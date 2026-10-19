"""Small shared helpers: timestamps, JSON IO, child-output decoding."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def now_iso() -> str:
    # Microseconds keep same-second updates distinguishable in bundle merges and
    # repro-run dedup keys (two runs can finish within one second).
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def dump_json(payload: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def decode_bytes(data: bytes) -> str:
    """UTF-8 → GBK → lossy UTF-8; never assume the child used UTF-8 (LabBox lesson)."""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
