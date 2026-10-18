"""分配算法（DEVELOPMENT_PLAN §9.3，P1 规则版，不上 ML）。

score(agent, task) = enabled && available && quota_left && model_fit(规模 → default/large)
选择 = fallback_chain 中首个 score>0；全满 → 入队等待（不占生产名额）。
分配决策连同理由写事件，可审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .adapters import build_adapter
from .config import AgentsConfig
from .quota import UsageLedger

LARGE_TASK_LINES = 400


@dataclass
class Decision:
    agent: str
    model: str | None
    large_model: bool
    reason: str
    rejected: list[dict] = field(default_factory=list)

    def as_event(self) -> dict:
        return {
            "agent": self.agent,
            "model": self.model,
            "large_model": self.large_model,
            "reason": self.reason,
            "rejected": self.rejected,
        }


def allocate(
    config: AgentsConfig,
    usage: UsageLedger,
    *,
    scope_lines: int | None = None,
    availability: Callable[[str], tuple[bool, str]] | None = None,
    exclude: set[str] | None = None,
) -> Decision | None:
    """按 fallback_chain 顺序选择第一个可用 agent；全不可用返回 None（调用方入队）。"""
    rejected: list[dict] = []
    excluded = exclude or set()
    large = bool(scope_lines is not None and scope_lines > LARGE_TASK_LINES)

    for order, name in enumerate(config.fallback_chain):
        agent = config.agents.get(name)
        if agent is None:
            rejected.append({"agent": name, "reason": "undefined_agent"})
            continue
        if name in excluded:
            rejected.append({"agent": name, "reason": "excluded_by_caller"})
            continue
        if not agent.enabled:
            rejected.append({"agent": name, "reason": "disabled"})
            continue
        if availability is not None:
            ok, detail = availability(name)
        else:
            ok, detail = build_adapter(agent).probe()
        if not ok:
            rejected.append({"agent": name, "reason": "unavailable", "detail": detail})
            continue
        left = usage.quota_left(agent)
        if left == 0:
            rejected.append({"agent": name, "reason": "quota_exhausted"})
            continue
        model = agent.model_for(large=large)
        if large and "large" not in agent.models:
            rejected.append({"agent": name, "reason": "no_large_model_configured_using_default"})
        return Decision(
            agent=name,
            model=model,
            large_model=large and "large" in agent.models,
            reason=(
                f"fallback_chain 第 {order + 1} 位；额度剩余 {left if left is not None else '不限'}；"
                f"规模 {scope_lines if scope_lines is not None else 'UNKNOWN'} 行 → "
                f"{'large' if large and 'large' in agent.models else 'default'} 模型"
            ),
            rejected=rejected,
        )
    return None


def plan_only_reason(decision: Decision | None) -> str:
    if decision is None:
        return "所有 agent 均不可用（禁用/不可执行/额度耗尽），任务入队等待。"
    return f"分配 {decision.agent}（{decision.model or '默认模型'}）：{decision.reason}"
