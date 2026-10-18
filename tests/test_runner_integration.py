"""runner 全链集成测试（仿真池 + 仿真 adapter）。

覆盖：打包 → 池迁移 → spawn 事件流 → diff/dossier/handoff → 状态推进 → 额度记账。
"""

from __future__ import annotations

import json

import pytest

from pr_agent import handoff
from pr_agent.errors import BlockedError
from pr_agent.models import Task
from pr_agent.runner import Runner
from pr_agent.poolctl import Pool

from tests.conftest import build_workspace


def _status(ws) -> str:
    record = Pool(ws.paths).record_for(ws.issue_id)
    return str(record["status"])


def test_assisted_happy_path(tmp_path, fake_build):
    ws = build_workspace(tmp_path)
    adapter = fake_build(write_handoff=True, write_dossier=True, commit=True)
    runner = Runner(ws.paths, ws.config, ws.projects)

    picked = runner.pick("Super/Repo", 7)
    assert picked.status == "packed"
    task = picked.task
    assert task is not None
    assert _status(ws) == "CODING"
    assert (ws.paths.task_dir(task.task_id) / "prompt.md").is_file()
    assert "权限模式契约" in (ws.paths.task_dir(task.task_id) / "prompt.md").read_text(encoding="utf-8")

    result = runner.run_task(task)
    assert result["status"] == "handoff_pending", result
    assert _status(ws) == "PR_READY"
    assert adapter.prompts and "## 任务上下文" in adapter.prompts[0]

    task_dir = ws.paths.task_dir(task.task_id)
    for name in ("report.md", "comment.md", "pr_body.md", "commands.md"):
        assert (task_dir / "handoff" / name).is_file()
    saved = Task.load(task_dir)
    assert saved.handoff_state == "pending_handoff"
    assert saved.exit_code == 0
    assert (task_dir / "diff.patch").is_file()
    assert "调度器补充证据" in (ws.paths.dossier_path("Super/Repo", 7)).read_text(encoding="utf-8")

    events = (task_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "task_start" in events and "task_result" in events
    pool_events = (ws.paths.pool_events).read_text(encoding="utf-8")
    assert "POOL_CODING" in pool_events and "HANDOFF_READY" in pool_events

    usage = json.loads(ws.paths.usage_ledger.read_text(encoding="utf-8"))
    assert usage["records"][0]["task_id"] == task.task_id
    assert usage["records"][0]["outcome"] == "ok"

    # 人工执行后回填 → PR_OPEN → PENDING_HUMAN
    done = handoff.finish(
        paths=ws.paths, pool=Pool(ws.paths), task=saved, task_dir=task_dir,
        comment_url="https://github.com/Super/Repo/issues/7#issuecomment-9",
        pr_url="https://github.com/Super/Repo/pull/21",
    )
    assert done["handoff_state"] == "executed"
    assert _status(ws) == "PENDING_HUMAN"


def test_dry_run_creates_nothing(tmp_path, fake_build):
    ws = build_workspace(tmp_path)
    fake_build()
    runner = Runner(ws.paths, ws.config, ws.projects)
    picked = runner.pick("Super/Repo", 7, dry_run=True)
    assert picked.status == "dry_run"
    assert not (ws.paths.tasks_dir / "Super__Repo__7").exists()
    assert _status(ws) == "VERIFIED"
    assert not any(ws.paths.worktrees_dir.iterdir())


def test_missing_handoff_blocks(tmp_path, fake_build):
    ws = build_workspace(tmp_path)
    fake_build(write_handoff=False, write_dossier=True)
    runner = Runner(ws.paths, ws.config, ws.projects)
    task = runner.pick("Super/Repo", 7).task
    result = runner.run_task(task)
    assert result["status"] == "blocked"
    assert "handoff" in result["reason"]
    assert _status(ws) == "BLOCKED"


def test_missing_dossier_blocks(tmp_path, fake_build):
    ws = build_workspace(tmp_path)
    fake_build(write_handoff=True, write_dossier=False)
    runner = Runner(ws.paths, ws.config, ws.projects)
    task = runner.pick("Super/Repo", 7).task
    result = runner.run_task(task)
    assert result["status"] == "blocked"
    assert "审查档案" in result["reason"]
    assert ws.paths.dossier_path("Super/Repo", 7).is_file()  # 骨架已生成，供人工查看


def test_nonzero_exit_blocks(tmp_path, fake_build):
    ws = build_workspace(tmp_path)
    fake_build(exit_code=1)
    runner = Runner(ws.paths, ws.config, ws.projects)
    task = runner.pick("Super/Repo", 7).task
    result = runner.run_task(task)
    assert result["status"] == "blocked"
    assert "exit=1" in result["reason"]
    assert _status(ws) == "BLOCKED"


def test_timeout_blocks(tmp_path, fake_build):
    ws = build_workspace(tmp_path)
    fake_build(timed_out=True)
    runner = Runner(ws.paths, ws.config, ws.projects)
    task = runner.pick("Super/Repo", 7).task
    result = runner.run_task(task)
    assert result["status"] == "blocked" and "timed_out=True" in result["reason"]


def test_stale_record_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    record = json.loads(ws.paths.pool_issues.read_text(encoding="utf-8"))
    record["last_verified_at"] = "2026-01-01T00:00:00Z"
    record["updated_at"] = "2026-01-01T00:00:00Z"
    ws.paths.pool_issues.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(BlockedError) as excinfo:
        Runner(ws.paths, ws.config, ws.projects).pick("Super/Repo", 7)
    assert "过期" in str(excinfo.value.message)


def test_scope_gate_must_pass(tmp_path):
    ws = build_workspace(tmp_path, scope="PENDING")
    with pytest.raises(BlockedError) as excinfo:
        Runner(ws.paths, ws.config, ws.projects).pick("Super/Repo", 7)
    assert "scope_gate" in str(excinfo.value.message)


def test_pick_requires_verified(tmp_path):
    ws = build_workspace(tmp_path, status="DISCOVERED")
    with pytest.raises(BlockedError):
        Runner(ws.paths, ws.config, ws.projects).pick("Super/Repo", 7)


def test_full_autonomy_happy_path(tmp_path, fake_build, monkeypatch):
    from pr_agent import runner as runner_mod

    ws = build_workspace(tmp_path)
    fake_build(write_handoff=True, write_dossier=True)
    monkeypatch.setattr(runner_mod.ghcli, "auth_status", lambda: (True, "已登录"))
    monkeypatch.setattr(runner_mod.ghcli, "find_pr",
                        lambda repo, branch: {"number": 33, "url": "https://github.com/Super/Repo/pull/33",
                                              "state": "OPEN", "headRefName": branch})
    runner = Runner(ws.paths, ws.config, ws.projects)
    task = runner.pick("Super/Repo", 7, mode="full_autonomy").task
    assert task.mode == "full_autonomy"
    result = runner.run_task(task)
    assert result["status"] == "ready"
    assert result["pr"]["number"] == 33
    assert _status(ws) == "PENDING_HUMAN"


def test_full_autonomy_requires_gh_auth(tmp_path, monkeypatch):
    from pr_agent import runner as runner_mod

    ws = build_workspace(tmp_path)
    monkeypatch.setattr(runner_mod.ghcli, "auth_status", lambda: (False, "未登录"))
    with pytest.raises(BlockedError) as excinfo:
        Runner(ws.paths, ws.config, ws.projects).pick("Super/Repo", 7, mode="full_autonomy")
    assert "gh 未登录" in str(excinfo.value.message)


def test_full_autonomy_without_pr_blocks(tmp_path, fake_build, monkeypatch):
    from pr_agent import runner as runner_mod

    ws = build_workspace(tmp_path)
    fake_build(write_dossier=True)
    monkeypatch.setattr(runner_mod.ghcli, "auth_status", lambda: (True, "已登录"))
    monkeypatch.setattr(runner_mod.ghcli, "find_pr", lambda repo, branch: None)
    runner = Runner(ws.paths, ws.config, ws.projects)
    task = runner.pick("Super/Repo", 7, mode="full_autonomy").task
    result = runner.run_task(task)
    assert result["status"] == "blocked"
    assert "未检测到 PR" in result["reason"]
    assert _status(ws) == "BLOCKED"
