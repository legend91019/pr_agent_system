"""治理门禁：黑/白名单、min_stars 硬门槛、人工覆盖、提案链。"""

from __future__ import annotations

import yaml

from pr_agent import governance
from pr_agent.config import ProjectsConfig, load_projects_config

from tests.conftest import build_workspace


def _projects(**overrides) -> ProjectsConfig:
    payload = {"whitelist": [], "blacklist": [], "principles": {"min_stars": 100}}
    payload.update(overrides)
    return ProjectsConfig.model_validate(payload)


def _record(repo="A/B", stars=None):
    record = {"repo": repo}
    if stars is not None:
        record["evidence"] = {"stars": stars}
    return record


def test_blacklist_rejected():
    config = _projects(blacklist=[{"repo": "A/B", "reason": "noisy"}])
    decision = governance.check_repo(config, _record("A/B"))
    assert not decision.allowed and decision.kind == "blacklist"


def test_approved_whitelist_passes():
    config = _projects(whitelist=[{"repo": "A/B", "approved": True, "added_by": "human"}])
    assert governance.check_repo(config, _record("A/B")).allowed


def test_unapproved_whitelist_is_not_effective():
    config = _projects(whitelist=[{"repo": "A/B", "approved": False, "added_by": "agent"}])
    decision = governance.check_repo(config, _record("A/B"))
    assert not decision.allowed


def test_missing_star_evidence_is_unknown_not_pass():
    config = _projects()
    decision = governance.check_repo(config, _record("A/B"))
    assert not decision.allowed
    assert "UNKNOWN" in decision.reason


def test_star_below_threshold_rejected():
    config = _projects()
    decision = governance.check_repo(config, _record("A/B", stars=99))
    assert not decision.allowed and decision.kind == "principles"


def test_star_above_threshold_passes():
    config = _projects()
    decision = governance.check_repo(config, _record("A/B", stars=150))
    assert decision.allowed and decision.stars == 150


def test_allow_unlisted_is_human_override():
    config = _projects()
    decision = governance.check_repo(config, _record("A/B"), allow_unlisted=True)
    assert decision.allowed and decision.kind == "override"


def test_propose_then_approve(tmp_path):
    ws = build_workspace(tmp_path)
    entry = governance.propose(ws.paths, "New/Repo", reason="发现潜力")
    assert entry.approved is False
    assert [item.repo for item in governance.pending_proposals(ws.paths)] == ["New/Repo"]
    governance.approve(ws.paths, "New/Repo")
    config = load_projects_config(ws.paths)
    assert "new/repo" in config.approved_repos()
    log = (ws.paths.logs_dir / "governance.jsonl").read_text(encoding="utf-8")
    assert "propose" in log and "approve" in log


def test_collect_discoveries(tmp_path):
    ws = build_workspace(tmp_path)
    directory = ws.paths.tasks_dir / "Super__Repo__7" / "discoveries"
    directory.mkdir(parents=True)
    (directory / "other-project.md").write_text("发现另一个值得投入的仓库", encoding="utf-8")
    found = governance.collect_discoveries(ws.paths.tasks_dir)
    assert found and found[0]["task_id"] == "Super__Repo__7"


# 原则加权评分（score_record）已按用户裁决（2026-10-18）下线：
# 选题由 agent/人工判断；硬门禁见上方 check_repo 用例，竞态上限在 test_jobs.run_day 覆盖。
