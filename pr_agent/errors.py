"""统一错误类型。

原则（fail-closed）：
- 任何无法确认的状态都用 `BlockedError` 表达，绝不降级为"看起来成功"。
- 错误信息必须带原始证据（路径、命令、stderr 片段），便于人工审计。
"""

from __future__ import annotations


class PrAgentError(Exception):
    """所有 pr_agent 错误的基类。"""

    kind = "error"

    def __init__(self, message: str, *, detail: str = "", evidence: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.evidence = evidence or {}

    def __str__(self) -> str:  # pragma: no cover - 展示用
        if self.detail:
            return f"{self.message}\n{self.detail}"
        return self.message


class ConfigError(PrAgentError):
    """配置缺失/非法。加载期即失败，不猜默认值。"""

    kind = "config_error"


class AssetMissingError(PrAgentError):
    """open_coding 权威资产缺失。启动自检失败即 BLOCKED。"""

    kind = "asset_missing"


class BlockedError(PrAgentError):
    """任务无法继续，必须以 BLOCKED 落盘。"""

    kind = "blocked"


class LockBusyError(BlockedError):
    """锁被占用（同一 worktree/adapter/并发槽）。"""

    kind = "lock_busy"


class PoolError(PrAgentError):
    """pool.py 子进程失败或输出无法解析。"""

    kind = "pool_error"


class AdapterError(BlockedError):
    """adapter 不可用或执行失败。"""

    kind = "adapter_error"
