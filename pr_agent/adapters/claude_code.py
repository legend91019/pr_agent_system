"""Claude Code headless adapter（P1 主用）。

命令：
  claude -p --output-format stream-json --verbose --model <model>
         --permission-mode bypassPermissions|acceptEdits --max-turns <N>
prompt 走 stdin（避免 Windows 命令行长度限制）。

权限模式：
- full_autonomy → bypassPermissions（允许 gh 写操作由 prompt 契约约束）
- assisted      → acceptEdits（本地编辑放行，GitHub 写由 prompt 契约禁止）
"""

from __future__ import annotations

import json

from .base import Adapter


class ClaudeCodeAdapter(Adapter):
    name = "claude_code"
    command_name = "claude"
    prompt_transport = "stdin"

    def build_command(
        self,
        *,
        prompt: str,
        model: str | None,
        full_autonomy: bool,
        max_turns: int,
        prompt_path,
    ) -> list[str]:
        argv = [
            self.executable,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(max_turns),
            "--permission-mode", "bypassPermissions" if full_autonomy else "acceptEdits",
        ]
        if model:
            argv.extend(["--model", model])
        return argv

    def parse_line(self, line: str) -> list[tuple[str, dict]]:
        text = line.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [("unknown_line", {"reason": "非 JSON 行", "text": text[:4000]})]
        if not isinstance(payload, dict):
            return [("unknown_line", {"reason": "JSON 非对象", "text": text[:4000]})]

        kind = payload.get("type")
        events: list[tuple[str, dict]] = []
        if kind == "system":
            events.append(("start", {
                "subtype": payload.get("subtype"),
                "session_id": payload.get("session_id"),
                "tools": payload.get("tools"),
                "model": payload.get("model"),
            }))
            return events
        if kind in {"assistant", "user"}:
            message = payload.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    events.append(("assistant_message", {"role": kind, "text": block.get("text", "")[:8000]}))
                elif block_type == "tool_use":
                    events.append(("tool_use", {
                        "tool": block.get("name"),
                        "input": _small(block.get("input")),
                        "tool_use_id": block.get("id"),
                    }))
                elif block_type == "tool_result":
                    events.append(("command", {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": _flatten(block.get("content")),
                    }))
            if not events:
                events.append(("assistant_message", {"role": kind, "text": "", "empty": True}))
            usage = message.get("usage")
            if usage:
                events.append(("usage", {"usage": usage, "model": message.get("model")}))
            return events
        if kind == "result":
            events.append(("result", {
                "subtype": payload.get("subtype"),
                "is_error": bool(payload.get("is_error")),
                "num_turns": payload.get("num_turns"),
                "duration_ms": payload.get("duration_ms"),
                "total_cost_usd": payload.get("total_cost_usd"),
                "result": str(payload.get("result", ""))[:8000],
                "session_id": payload.get("session_id"),
            }))
            return events
        return [("unknown_line", {"reason": f"未映射类型 {kind}", "text": text[:4000]})]


def _flatten(content) -> str:
    if isinstance(content, str):
        return content[:6000]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
        return "\n".join(parts)[:6000]
    return json.dumps(content, ensure_ascii=False)[:6000]


def _small(value, limit: int = 3000):
    if isinstance(value, dict):
        return {key: (str(item)[:1500] if not isinstance(item, (dict, list)) else _small(item, limit))
                for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        return [_small(item, limit) for item in value[:30]]
    if isinstance(value, str):
        return value[:limit]
    return value
