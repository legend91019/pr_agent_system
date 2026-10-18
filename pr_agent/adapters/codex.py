"""Codex CLI adapter（P2，安装后启用）。

命令：codex exec --json [--full-auto|--sandbox workspace-write] "<prompt>"
- 全自动模式才给 `--full-auto`；assisted 用 `--sandbox workspace-write`。
- 事件映射按 `--json` 的 JSONL 输出做**保守**映射：无法识别的行落 `unknown_line`，不猜测语义。
- 本机未安装（enabled:false），运行时行为尚未实测。
"""

from __future__ import annotations

import json

from .base import Adapter


class CodexAdapter(Adapter):
    name = "codex"
    command_name = "codex"
    prompt_transport = "argv"

    def build_command(self, *, prompt, model, full_autonomy, max_turns, prompt_path) -> list[str]:
        argv = [self.executable, "exec", "--json"]
        if full_autonomy:
            argv.append("--full-auto")
        else:
            argv.extend(["--sandbox", "workspace-write"])
        if model:
            argv.extend(["--model", model])
        argv.append(prompt)
        return argv

    def parse_line(self, line: str) -> list[tuple[str, dict]]:
        text = line.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [("assistant_message", {"text": text[:4000], "format": "plain"})]
        if not isinstance(payload, dict):
            return [("unknown_line", {"text": text[:4000]})]
        kind = str(payload.get("type") or payload.get("msg", {}).get("type") or "")
        if "completed" in kind or "result" in kind:
            return [("result", {"raw_type": kind, "payload": payload})]
        if "message" in kind:
            msg = payload.get("message") or payload.get("msg") or {}
            content = msg.get("content") if isinstance(msg, dict) else ""
            return [("assistant_message", {"text": str(content)[:8000], "raw_type": kind})]
        if "exec" in kind or "command" in kind:
            return [("command", {"raw_type": kind, "payload": payload})]
        return [("unknown_line", {"raw_type": kind, "text": text[:4000]})]
