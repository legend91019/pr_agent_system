"""配置加载与严格校验。"""

from __future__ import annotations

import yaml
import pytest

from pr_agent.config import load_agents_config, load_projects_config
from pr_agent.errors import ConfigError

from tests.conftest import build_workspace


def _write_agents(paths, payload: dict) -> None:
    paths.agents_config.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_valid_config_loads(workspace):
    assert workspace.config.mode == "assisted"
    assert workspace.config.max_parallel == 2
    assert workspace.config.agents["claude-code"].model_for(large=True) == "fake-large"
    assert workspace.projects.approved_repos() == {"super/repo"}


def test_disabled_agent_in_chain_is_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    raw = yaml.safe_load(ws.paths.agents_config.read_text(encoding="utf-8"))
    raw["agents"]["claude-code"]["enabled"] = False
    _write_agents(ws.paths, raw)
    with pytest.raises(ConfigError) as excinfo:
        load_agents_config(ws.paths)
    assert "必须 enabled=true" in str(excinfo.value)


def test_undefined_fallback_agent_is_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    raw = yaml.safe_load(ws.paths.agents_config.read_text(encoding="utf-8"))
    raw["fallback_chain"] = ["claude-code", "nope"]
    _write_agents(ws.paths, raw)
    with pytest.raises(ConfigError):
        load_agents_config(ws.paths)


def test_unknown_adapter_is_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    raw = yaml.safe_load(ws.paths.agents_config.read_text(encoding="utf-8"))
    raw["agents"]["claude-code"]["adapter"] = "magic_cli"
    _write_agents(ws.paths, raw)
    with pytest.raises(ConfigError):
        load_agents_config(ws.paths)


def test_missing_env_var_on_critical_path_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("PR_AGENT_TEST_TOKEN", raising=False)
    ws = build_workspace(tmp_path)
    raw = yaml.safe_load(ws.paths.agents_config.read_text(encoding="utf-8"))
    raw["agents"]["claude-code"]["models"]["default"] = "${PR_AGENT_TEST_TOKEN}"
    _write_agents(ws.paths, raw)
    with pytest.raises(ConfigError) as excinfo:
        load_agents_config(ws.paths)
    assert "PR_AGENT_TEST_TOKEN" in str(excinfo.value)


def test_frontend_env_missing_is_only_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("PR_AGENT_FRONTEND_BASE_URL", raising=False)
    monkeypatch.delenv("PR_AGENT_FRONTEND_MODEL", raising=False)
    ws = build_workspace(tmp_path)
    raw = yaml.safe_load(ws.paths.agents_config.read_text(encoding="utf-8"))
    raw["frontend"] = {"base_url": "${PR_AGENT_FRONTEND_BASE_URL}",
                       "model": "${PR_AGENT_FRONTEND_MODEL}",
                       "api_key_env": "PR_AGENT_FRONTEND_KEY"}
    _write_agents(ws.paths, raw)
    config = load_agents_config(ws.paths)
    assert config.config_warnings and "PR_AGENT_FRONTEND_BASE_URL" in config.config_warnings[0]
    assert not config.frontend.ready


def test_window_quota_requires_limits(tmp_path):
    ws = build_workspace(tmp_path)
    raw = yaml.safe_load(ws.paths.agents_config.read_text(encoding="utf-8"))
    raw["agents"]["claude-code"]["quota"] = {"budget_type": "window"}
    _write_agents(ws.paths, raw)
    with pytest.raises(ConfigError):
        load_agents_config(ws.paths)


def test_mode_override_is_validated(workspace):
    assert load_agents_config(workspace.paths, mode="full_autonomy").mode == "full_autonomy"
    with pytest.raises(ConfigError):
        load_agents_config(workspace.paths, mode="yolo")


def test_projects_whitelist_blacklist_overlap_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    payload = {
        "whitelist": [{"repo": "A/B", "approved": True}],
        "blacklist": [{"repo": "a/b", "reason": "no"}],
    }
    ws.paths.projects_config.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_projects_config(ws.paths)
    assert "同时出现在白名单与黑名单" in str(excinfo.value)


def test_projects_deprecated_keys_ignored_with_warning(tmp_path):
    """用户裁决（2026-10-18）：weights 等评分键已废弃——忽略并告警，而不是打不开配置。"""
    ws = build_workspace(tmp_path)
    payload = {"whitelist": [], "blacklist": [],
               "principles": {"min_stars": 150, "max_recent_competing_prs": 3,
                              "weights": {"potential": 0.5, "feasibility": 0.4, "speed": 0.2},
                              "require_backing": ["big-tech"],
                              "max_days_to_first_review": 21}}
    ws.paths.projects_config.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = load_projects_config(ws.paths)
    assert config.principles.min_stars == 150
    assert config.principles.max_recent_competing_prs == 3
    assert len(config.config_warnings) == 1
    assert "weights" in config.config_warnings[0]
    assert "require_backing" in config.config_warnings[0]


def test_unapproved_proposal_not_effective(tmp_path):
    ws = build_workspace(tmp_path)
    payload = {
        "whitelist": [{"repo": "A/B", "added_by": "agent", "approved": False}],
        "blacklist": [],
        "principles": {"min_stars": 10},
    }
    ws.paths.projects_config.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = load_projects_config(ws.paths)
    assert config.approved_repos() == set()
    assert [entry.repo for entry in config.pending_proposals()] == ["A/B"]


def test_empty_commented_list_is_empty_not_error(tmp_path):
    """YAML 中整段只有注释时解析为 None，应视为空列表而不是配置错误。"""
    ws = build_workspace(tmp_path)
    ws.paths.projects_config.write_text(
        "whitelist:/n  # 待补充\nblacklist:/n  # 待补充\nprinciples:/n  min_stars: 10\n",
        encoding="utf-8",
    )
    config = load_projects_config(ws.paths)
    assert config.whitelist == [] and config.blacklist == []


def test_empty_commented_list_is_empty_not_error(tmp_path):
    """YAML 中整段只有注释时解析为 None，应视为空列表而不是配置错误。"""
    ws = build_workspace(tmp_path)
    ws.paths.projects_config.write_text(
        "whitelist:\n  # 待补充\nblacklist:\n  # 待补充\nprinciples:\n  min_stars: 10\n",
        encoding="utf-8",
    )
    config = load_projects_config(ws.paths)
    assert config.whitelist == [] and config.blacklist == []
