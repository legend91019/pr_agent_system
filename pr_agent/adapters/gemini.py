"""Gemini CLI adapter（P2）。

命令：gemini -m <model> -p "<prompt>"
- prompt 以 argv 传入（DEVELOPMENT_PLAN §8 表列形式），超长时由基类拒绝而非截断。
- JSON 输出开关随本机版本漂移，本适配器不做猜测；按纯文本行映射为 assistant_message。
- 本机未安装（enabled:false），运行时行为尚未实测，需安装后跑 `pr_agent doctor`。
"""

from __future__ import annotations

from .base import Adapter


class GeminiAdapter(Adapter):
    name = "gemini"
    command_name = "gemini"
    prompt_transport = "argv"

    def build_command(self, *, prompt, model, full_autonomy, max_turns, prompt_path) -> list[str]:
        argv = [self.executable, "-m", model or "gemini-2.5-pro", "-p", prompt]
        return argv
