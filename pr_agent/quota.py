"""额度记账（DEVELOPMENT_PLAN §9.3）。

以任务数为唯一可本地可靠计数的量：窗口 / 日 / 周三种预算 + 冷却期。
记账文件 logs/usage.json，只含任务 id 与时间戳，不含任何密钥。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .events import SHANGHAI, now_iso, week_key
from .locks import FileLock
from .paths import Paths

RETENTION_DAYS = 60


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass
class UsageLedger:
    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    cooldowns: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "UsageLedger":
        if not path.is_file():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 账本损坏不能让额度变成"无限"，按空账本处理并保留原文件供人工检查
            return cls(path=path)
        return cls(
            path=path,
            records=[item for item in raw.get("records", []) if isinstance(item, dict)],
            cooldowns=dict(raw.get("cooldowns", {})),
        )

    # --- 写入 ---
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "records": self.records, "cooldowns": self.cooldowns}
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _mutate(self, action) -> None:
        lock = FileLock(self.path.with_suffix(".json.lock"), owner="usage", timeout=10.0)
        with lock:
            fresh = UsageLedger.load(self.path)
            self.records = fresh.records
            self.cooldowns = fresh.cooldowns
            action()
            self._prune()
            self._save()

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        kept = []
        for record in self.records:
            stamp = _parse(record.get("at"))
            if stamp is None or stamp >= cutoff:
                kept.append(record)
        self.records = kept

    def record(self, agent: str, task_id: str, *, at: str | None = None, outcome: str = "ok") -> None:
        stamp = at or now_iso()

        def action() -> None:
            self.records.append(
                {"agent": agent, "task_id": task_id, "at": stamp, "outcome": outcome}
            )

        self._mutate(action)

    def mark_cooldown(self, agent: str, minutes: int, *, at: str | None = None) -> None:
        if minutes <= 0:
            return
        base = _parse(at) or datetime.now(timezone.utc)
        until = (base + timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        def action() -> None:
            self.cooldowns[agent] = until

        self._mutate(action)

    # --- 查询 ---
    def usage_in_current_window(self, agent: str, quota, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        records = [item for item in self.records if item.get("agent") == agent]
        if quota.budget_type == "window" and quota.window_hours:
            start = moment - timedelta(hours=quota.window_hours)
            return sum(1 for item in records if (_parse(item.get("at")) or start) > start)
        if quota.budget_type == "daily":
            today = moment.astimezone(SHANGHAI).strftime("%Y-%m-%d")
            return sum(
                1
                for item in records
                if (_parse(item.get("at")) or moment).astimezone(SHANGHAI).strftime("%Y-%m-%d") == today
            )
        if quota.budget_type == "weekly":
            key = week_key(moment)
            return sum(1 for item in records if week_key(_parse(item.get("at")) or moment) == key)
        return len(records)

    def cooldown_until(self, agent: str) -> datetime | None:
        return _parse(self.cooldowns.get(agent))

    def quota_left(self, agent: AgentConfig, now: datetime | None = None) -> int | None:
        """剩余可执行任务数；None 表示无额度限制。"""
        moment = now or datetime.now(timezone.utc)
        quota = agent.quota
        if quota.cooldown_minutes:
            last = max(
                (_parse(item.get("at")) or moment for item in self.records if item.get("agent") == agent.name),
                default=None,
            )
            if last is not None and moment < last + timedelta(minutes=quota.cooldown_minutes):
                return 0
        until = self.cooldown_until(agent.name)
        if until is not None and moment < until:
            return 0
        limit: int | None = None
        if quota.budget_type == "window":
            limit = quota.max_tasks_per_window
        elif quota.budget_type == "daily":
            limit = quota.max_tasks_per_day
        elif quota.budget_type == "weekly":
            limit = quota.max_tasks_per_week
        if limit is None:
            return None
        return max(0, limit - self.usage_in_current_window(agent.name, quota, moment))

    def describe(self, agent: AgentConfig, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        left = self.quota_left(agent, moment)
        return {
            "agent": agent.name,
            "budget_type": agent.quota.budget_type,
            "used": self.usage_in_current_window(agent.name, agent.quota, moment),
            "left": left,
            "cooldown_until": self.cooldowns.get(agent.name),
        }

    def snapshot(self, agents: dict[str, AgentConfig], now: datetime | None = None) -> list[dict[str, Any]]:
        return [self.describe(agent, now) for agent in agents.values()]


def load_ledger(paths: Paths) -> UsageLedger:
    return UsageLedger.load(paths.usage_ledger)
