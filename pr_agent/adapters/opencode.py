"""OpenCode adapter（P2）。

命令：opencode run -m <model> "<prompt>"
- `opencode serve` HTTP 通道留待后续（本计划不实现 HTTP 客户端）。
- 文本输出按行映射为 assistant_message；本机未安装，运行时行为尚未实测。
"""

from __future__ import annotations

from .base import Adapter


class OpenCodeAdapter(Adapter):
    name = "opencode"
    command_name = "opencode"
    prompt_transport = "argv"

    def build_command(self, *, prompt, model, full_autonomy, max_turns, prompt_path) -> list[str]:
        argv = [self.executable, "run"]
        if model:
            argv.extend(["-m", model])
        argv.append(prompt)
        return argv
