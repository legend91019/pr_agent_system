"""额度记账：窗口/日/周 + 冷却。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pr_agent.config import AgentConfig
from pr_agent.events import week_key
from pr_agent.quota import UsageLedger


def _agent(**quota) -> AgentConfig:
    return AgentConfig.model_validate({"name": "a", "enabled": True, "adapter": "claude_code",
                                      "quota": quota})


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_unlimited_quota_returns_none(tmp_path):
    ledger = UsageLedger.load(tmp_path / "usage.json")
    assert ledger.quota_left(_agent(budget_type="none")) is None


def test_window_quota_counts_and_exhausts(tmp_path):
    now = datetime.now(timezone.utc)
    ledger = UsageLedger.load(tmp_path / "usage.json")
    ledger.records = [
        {"agent": "a", "task_id": "t1", "at": _iso(now - timedelta(hours=1))},
        {"agent": "a", "task_id": "t2", "at": _iso(now - timedelta(hours=3))},
        {"agent": "a", "task_id": "old", "at": _iso(now - timedelta(hours=9))},
    ]
    agent = _agent(budget_type="window", window_hours=5, max_tasks_per_window=3)
    assert ledger.quota_left(agent, now) == 1
    ledger.records.append({"agent": "a", "task_id": "t3", "at": _iso(now - timedelta(minutes=5))})
    assert ledger.quota_left(agent, now) == 0


def test_daily_quota_uses_shanghai_day(tmp_path):
    now = datetime.now(timezone.utc)
    ledger = UsageLedger.load(tmp_path / "usage.json")
    ledger.records = [{"agent": "a", "task_id": "t1", "at": _iso(now)}]
    agent = _agent(budget_type="daily", max_tasks_per_day=1)
    assert ledger.quota_left(agent, now) == 0
    assert ledger.usage_in_current_window("a", agent.quota, now) == 1


def test_weekly_quota(tmp_path):
    now = datetime.now(timezone.utc)
    ledger = UsageLedger.load(tmp_path / "usage.json")
    ledger.records = [{"agent": "a", "task_id": "t1", "at": _iso(now)}]
    agent = _agent(budget_type="weekly", max_tasks_per_week=2)
    assert week_key(now) == week_key()
    assert ledger.quota_left(agent, now) == 1


def test_cooldown_blocks_after_recent_use(tmp_path):
    now = datetime.now(timezone.utc)
    ledger = UsageLedger.load(tmp_path / "usage.json")
    ledger.records = [{"agent": "a", "task_id": "t1", "at": _iso(now - timedelta(minutes=10))}]
    agent = _agent(budget_type="none", cooldown_minutes=30)
    assert ledger.quota_left(agent, now) == 0
    assert ledger.quota_left(agent, now + timedelta(minutes=40)) is None


def test_record_and_cooldown_persist(tmp_path):
    path = tmp_path / "usage.json"
    ledger = UsageLedger.load(path)
    ledger.record("a", "task-1")
    ledger.mark_cooldown("a", 15)
    reloaded = UsageLedger.load(path)
    assert reloaded.records[0]["task_id"] == "task-1"
    assert reloaded.cooldown_until("a") is not None
