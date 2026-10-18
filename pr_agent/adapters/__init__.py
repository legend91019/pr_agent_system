"""adapter 注册表。"""

from __future__ import annotations

from ..config import AgentConfig
from ..errors import ConfigError
from .base import Adapter, Event
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .opencode import OpenCodeAdapter
from .workbuddy import WorkBuddyAdapter

_REGISTRY: dict[str, type[Adapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "gemini": GeminiAdapter,
    "codex": CodexAdapter,
    "opencode": OpenCodeAdapter,
    "workbuddy": WorkBuddyAdapter,
}


def build_adapter(agent: AgentConfig) -> Adapter:
    try:
        factory = _REGISTRY[agent.adapter]
    except KeyError as exc:
        raise ConfigError(f"未注册的 adapter：{agent.adapter}") from exc
    return factory(agent)


def probe_all(agents: dict[str, AgentConfig]) -> list[dict]:
    report = []
    for name, agent in agents.items():
        adapter = build_adapter(agent)
        ok, detail = adapter.probe(refresh=True)
        report.append({
            "agent": name,
            "adapter": agent.adapter,
            "enabled": agent.enabled,
            "available": ok,
            "detail": detail,
            "transport": adapter.prompt_transport,
        })
    return report


__all__ = ["Adapter", "Event", "build_adapter", "probe_all"]
