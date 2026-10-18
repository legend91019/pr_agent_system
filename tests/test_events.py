"""事件日志：append-only、截断、摘要事件 schema。"""

from __future__ import annotations

import json

from pr_agent.events import (MAX_FIELD_CHARS, TaskEvents, append_jsonl, pool_summary_event,
                             read_jsonl)

from tests.conftest import build_workspace


def test_append_and_read(tmp_path):
    path = tmp_path / "events.jsonl"
    append_jsonl(path, {"type": "a", "中文": "值"})
    append_jsonl(path, {"type": "b"})
    records = read_jsonl(path)
    assert [item["type"] for item in records] == ["a", "b"]
    assert records[0]["中文"] == "值"


def test_long_field_truncated(tmp_path):
    path = tmp_path / "e.jsonl"
    append_jsonl(path, {"text": "x" * (MAX_FIELD_CHARS + 50)})
    record = read_jsonl(path)[0]
    assert "truncated" in record["text"]


def test_unparsable_line_is_preserved(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text("{broken\n", encoding="utf-8")
    records = read_jsonl(path)
    assert records[0]["type"] == "unparsable_line"


def test_task_events_writer(tmp_path):
    events = TaskEvents(tmp_path, "t1")
    events.emit("start", {"a": 1})
    events.emit("exit", {"returncode": 0}, adapter="claude_code")
    records = read_jsonl(tmp_path / "events.jsonl")
    assert [item["type"] for item in records] == ["start", "exit"]
    assert records[1]["adapter"] == "claude_code"
    assert events.count == 2


def test_pool_summary_event_schema(tmp_path):
    ws = build_workspace(tmp_path)
    event = pool_summary_event(ws.paths, issue_id="Super/Repo#7", kind="TASK_PACKED", task_id="T1")
    assert set(event) >= {"event_id", "issue_id", "actor", "at", "kind", "evidence"}
    assert event["source"] == "pr_agent" and event["task_id"] == "T1"
    lines = (ws.paths.pool_events).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["kind"] == "TASK_PACKED"
