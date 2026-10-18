"""配置面板：默认值生成、交互流程、备份与回读校验。"""

from __future__ import annotations

from pathlib import Path

from pr_agent.config import load_agents_config, load_projects_config
from pr_agent.config_wizard import ScriptedIO, run_wizard
from pr_agent.paths import load_paths

PROBES = {
    "claude-code": (True, "claude 2.1.153"),
    "gemini": (False, "探测超时"),
    "codex": (False, "未安装"),
    "opencode": (False, "未安装"),
    "workbuddy": (False, "接口待定"),
}


def _paths(tmp_path):
    return load_paths(tmp_path / "ws", tmp_path / "oc")


def test_wizard_defaults_write_valid_config(tmp_path):
    """非交互（全默认）：只启用探测可用的 agent，生成合法配置并可被正式加载器回读。"""
    paths = _paths(tmp_path)
    summary = run_wizard(paths, ScriptedIO(), probes=PROBES, gh_check=lambda: (False, "未登录"))

    config = load_agents_config(paths)
    assert config.mode == "assisted"
    assert config.fallback_chain == ["claude-code"]  # 只有 claude-code 探测可用
    assert config.agents["claude-code"].quota.budget_type == "window"
    assert config.agents["claude-code"].models["default"] == "sonnet"

    projects = load_projects_config(paths)
    assert projects.principles.min_stars == 150
    assert projects.principles.max_recent_competing_prs == 3

    assert summary["mode"] == "assisted"
    assert summary["written"]["backup_dir"] is None  # 首次生成无备份


def test_wizard_interactive_full_flow(tmp_path):
    """脚本化交互：full_autonomy + 双 agent + 交叉 review + 白名单 + 自定义门槛。"""
    paths = _paths(tmp_path)
    answers = [
        "full_autonomy",                       # 权限模式
        "3", "5",                             # max_parallel / weekly_pr_cap
        # claude-code：启用、默认模型、large、window 额度
        True, "sonnet", "opus", "window", "5", "10",
        # gemini：启用、默认模型、无 large、daily 额度
        True, "gemini-2.5-pro", "", "daily", "20",
        # codex / opencode / workbuddy：不启用
        False, False, False,
        True,                                  # fallback 用建议顺序
        True, "gemini",                        # 交叉 review 启用，reviewer=gemini
        True, "https://api.example.com/v1", "gpt-x", "MY_KEY_ENV",  # 前端 LLM
        True, "Tencent/BrowserSkill", "重点深耕", False,             # 白名单一条后停止
        False,                                 # 黑名单不加
        "200", "2",                            # min_stars / 竞态上限
    ]
    summary = run_wizard(paths, ScriptedIO(answers), probes=PROBES,
                         gh_check=lambda: (False, "gh 未登录"))

    config = load_agents_config(paths)
    assert config.mode == "full_autonomy"
    assert config.max_parallel == 3 and config.weekly_pr_cap == 5
    assert config.fallback_chain == ["claude-code", "gemini"]
    assert config.agents["gemini"].quota.budget_type == "daily"
    assert config.cross_review.enabled and config.cross_review.reviewer == "gemini"
    assert config.frontend.base_url == "https://api.example.com/v1"
    assert config.frontend.api_key_env == "MY_KEY_ENV"

    projects = load_projects_config(paths)
    assert projects.approved_repos() == {"tencent/browserskill"}
    assert projects.principles.min_stars == 200
    assert projects.principles.max_recent_competing_prs == 2

    # full_autonomy 且 gh 未认证 → token 指引；前端 key 未设环境变量 → setx 指引
    assert any("store_github_token" in step for step in summary["next_steps"])
    assert any("MY_KEY_ENV" in step for step in summary["next_steps"])
    assert any("gh 未认证" in warning for warning in summary["warnings"])


def test_wizard_backs_up_existing_config(tmp_path):
    """覆盖已有配置前必须备份到 logs/config_backup/。"""
    paths = _paths(tmp_path)
    run_wizard(paths, ScriptedIO(), probes=PROBES)
    first = paths.agents_config.read_text(encoding="utf-8")

    summary = run_wizard(paths, ScriptedIO(), probes=PROBES)
    assert summary["written"]["backup_dir"]
    backup = Path(summary["written"]["backup_dir"])
    assert (backup / "agents.yaml").read_text(encoding="utf-8") == first
    assert (backup / "projects.yaml").is_file()

    events = (paths.logs_dir / "config_wizard.jsonl").read_text(encoding="utf-8")
    assert len(events.strip().splitlines()) == 2  # 每次面板运行落一条审计事件


def test_wizard_refuses_when_no_agent_enabled(tmp_path):
    """一家 agent 都不启用：明确报错，不写任何配置文件。"""
    paths = _paths(tmp_path)
    answers = [False, False, False, False, False]  # 五家全部不启用
    try:
        run_wizard(paths, ScriptedIO(answers), probes=PROBES)
        raise AssertionError("应当抛出 PrAgentError")
    except Exception as exc:  # PrAgentError
        assert "未启用任何 coding agent" in str(exc)
    assert not paths.agents_config.exists()
