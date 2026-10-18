"""事件日志（DEVELOPMENT_PLAN §8/§11）。

分工：
- `tasks/<task_id>/events.jsonl`：完整 agent 事件流（审计用，含原始行）。
- `open_coding/pool/events.jsonl`：摘要级事件（start/transition/result），状态真相在池子。
两处都不含密钥。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import Paths

SHANGHAI = timezone(timedelta(hours=8))  # Windows 无系统 tzdata，用固定偏移（中国无夏令时）
MAX_FIELD_CHARS = 20_000


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_local() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


def week_key(moment: datetime | None = None) -> str:
    local = (moment or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"\n…[truncated {len(value) - MAX_FIELD_CHARS} chars]"
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item) for item in value[:200]]
    return value


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """单次 append 写入一行；失败即抛出（不吞错）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_truncate(record), ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            records.append({"type": "unparsable_line", "line": number, "raw": line})
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


class TaskEvents:
    """一个任务的事件流写入器。"""

    def __init__(self, task_dir: Path, task_id: str) -> None:
        self.path = task_dir / "events.jsonl"
        self.task_id = task_id
        self.count = 0

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, adapter: str = "pr-agent") -> None:
        self.count += 1
        append_jsonl(
            self.path,
            {
                "ts": now_iso(),
                "task_id": self.task_id,
                "adapter": adapter,
                "type": event_type,
                "payload": payload or {},
            },
        )


def pool_summary_event(
    paths: Paths,
    *,
    issue_id: str,
    kind: str,
    evidence: dict[str, Any] | None = None,
    task_id: str | None = None,
    actor: str = "pr-agent",
) -> dict[str, Any]:
    """写摘要级事件到 open_coding/pool/events.jsonl（只在任务真正推进时调用）。"""
    at = now_iso()
    event: dict[str, Any] = {
        "event_id": f"{issue_id}@{at}@{kind}",
        "issue_id": issue_id,
        "actor": actor,
        "at": at,
        "kind": kind,
        "evidence": _truncate(evidence or {}),
        "source": "pr_agent",
    }
    if task_id:
        event["task_id"] = task_id
    append_jsonl(paths.pool_events, event)
    return event
