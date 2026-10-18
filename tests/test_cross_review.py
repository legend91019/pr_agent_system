"""交叉 review：发现解析与跨厂商选择。"""

from __future__ import annotations

from pr_agent import cross_review
from pr_agent.config import AgentsConfig
from pr_agent.quota import UsageLedger

SAMPLE = """## Blocker
- 未处理空输入，可能抛 IndexError
## Major
- 缺少回归测试
## Minor/Nit
- 变量命名可以更清晰
## 结论
REQUEST_CHANGES
"""


def test_parse_findings():
    findings = cross_review.parse_findings(SAMPLE)
    assert findings["Blocker"] == ["未处理空输入，可能抛 IndexError"]
    assert findings["Major"] == ["缺少回归测试"]
    assert findings["Minor/Nit"] == ["变量命名可以更清晰"]


def test_parse_empty_sections():
    findings = cross_review.parse_findings("## Blocker\n- 无\n## Major\n- 无\n## 结论\nAPPROVE\n")
    assert findings == {"Blocker": [], "Major": [], "Minor/Nit": []}


def _config() -> AgentsConfig:
    return AgentsConfig.model_validate({
        "mode": "assisted", "max_parallel": 2,
        "fallback_chain": ["a", "b"],
        "agents": {
            "a": {"enabled": True, "adapter": "claude_code", "quota": {"budget_type": "none"}},
            "b": {"enabled": True, "adapter": "gemini", "quota": {"budget_type": "none"}},
        },
    })


def test_choose_reviewer_uses_other_vendor(tmp_path, monkeypatch):
    monkeypatch.setattr(cross_review, "build_adapter",
                        lambda agent: type("A", (), {"probe": staticmethod(lambda refresh=False: (True, "ok"))})())
    ledger = UsageLedger.load(tmp_path / "u.json")
    assert cross_review.choose_reviewer(_config(), "a", ledger) == "b"


def test_choose_reviewer_none_when_same_vendor_only(tmp_path, monkeypatch):
    monkeypatch.setattr(cross_review, "build_adapter",
                        lambda agent: type("A", (), {"probe": staticmethod(lambda refresh=False: (True, "ok"))})())
    config = AgentsConfig.model_validate({
        "mode": "assisted", "max_parallel": 1, "fallback_chain": ["a"],
        "agents": {"a": {"enabled": True, "adapter": "claude_code", "quota": {"budget_type": "none"}}},
    })
    ledger = UsageLedger.load(tmp_path / "u.json")
    assert cross_review.choose_reviewer(config, "a", ledger) is None


def test_summary_line():
    result = cross_review.ReviewResult("b", ["blocker"], {"Blocker": ["blocker"], "Major": [], "Minor/Nit": []},
                                       "REQUEST_CHANGES", "raw", "path")
    assert "b" in cross_review.summary_line(result)
    assert "Blocker 1" in cross_review.summary_line(result)
