"""WorkBuddy adapter 桩（P4）。

接口待定：尚无稳定的 headless 非交互契约（DEVELOPMENT_PLAN §8.6）。
在接口确定前，本 adapter 明确不可用：
- `available()` 永远返回 False，并在 doctor 中报告原因；
- `build_command()` 直接抛错，避免"看起来能跑"的静默降级。
"""

from __future__ import annotations

from ..errors import AdapterError
from .base import Adapter


class WorkBuddyAdapter(Adapter):
    name = "workbuddy"
    command_name = "workbuddy"
    prompt_transport = "stdin"

    def probe(self, *, refresh: bool = False) -> tuple[bool, str]:
        return False, "接口待定（DEVELOPMENT_PLAN §8.6 / P4），本版本不参与分配"

    def build_command(self, *, prompt, model, full_autonomy, max_turns, prompt_path) -> list[str]:
        raise AdapterError(
            "WorkBuddy adapter 接口待定（P4），拒绝生成命令",
            detail="请勿在 agents.yaml 中 enable 该 agent；待 headless 契约确定后再实现。",
        )
