"""分配算法：顺序、额度、可用性、模型规模。"""

from __future__ import annotations

from pr_agent.allocate import allocate
from pr_agent.config import AgentsConfig
from pr_agent.quota import UsageLedger


def _config(chain, agents) -> AgentsConfig:
    return AgentsConfig.model_validate({"mode": "assisted", "max_parallel": 2,
                                        "fallback_chain": chain, "agents": agents})


def _agent(adapter="claude_code", enabled=True, quota=None, models=None):
    return {"enabled": enabled, "adapter": adapter,
            "quota": quota or {"budget_type": "none"},
            "models": models if models is not None else {"default": "m1", "large": "m2"}}


def test_picks_first_available_in_chain(tmp_path):
    config = _config(["a", "b"], {"a": _agent("claude_code"), "b": _agent("gemini")})
    ledger = UsageLedger.load(tmp_path / "u.json")
    decision = allocate(config, ledger, availability=lambda name: (True, "ok"))
    assert decision.agent == "a"
    assert decision.model == "m1"


def test_unavailable_agent_falls_back(tmp_path):
    config = _config(["a", "b"], {"a": _agent("claude_code"), "b": _agent("gemini")})
    ledger = UsageLedger.load(tmp_path / "u.json")
    decision = allocate(config, ledger,
                        availability=lambda name: (name == "b", "a 未安装"))
    assert decision.agent == "b"
    assert decision.rejected[0]["reason"] == "unavailable"


def test_quota_exhausted_falls_back(tmp_path):
    config = _config(["a", "b"], {
        "a": _agent("claude_code", quota={"budget_type": "daily", "max_tasks_per_day": 1}),
        "b": _agent("gemini"),
    })
    ledger = UsageLedger.load(tmp_path / "u.json")
    ledger.record("a", "t1")
    decision = allocate(config, ledger, availability=lambda name: (True, "ok"))
    assert decision.agent == "b"
    assert decision.rejected[0]["reason"] == "quota_exhausted"


def test_all_exhausted_returns_none(tmp_path):
    config = _config(["a"], {"a": _agent("claude_code",
                                         quota={"budget_type": "daily", "max_tasks_per_day": 1})})
    ledger = UsageLedger.load(tmp_path / "u.json")
    ledger.record("a", "t1")
    assert allocate(config, ledger, availability=lambda name: (True, "ok")) is None


def test_large_model_for_big_scope(tmp_path):
    config = _config(["a"], {"a": _agent("claude_code")})
    ledger = UsageLedger.load(tmp_path / "u.json")
    small = allocate(config, ledger, scope_lines=399, availability=lambda name: (True, "ok"))
    large = allocate(config, ledger, scope_lines=401, availability=lambda name: (True, "ok"))
    assert small.model == "m1" and small.large_model is False
    assert large.model == "m2" and large.large_model is True


def test_exclude_caller_agent(tmp_path):
    config = _config(["a", "b"], {"a": _agent("claude_code"), "b": _agent("gemini")})
    ledger = UsageLedger.load(tmp_path / "u.json")
    decision = allocate(config, ledger, availability=lambda name: (True, "ok"), exclude={"a"})
    assert decision.agent == "b"
