"""handoff 四件套校验与人工回填。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pr_agent import handoff
from pr_agent.errors import BlockedError
from pr_agent.models import Task

from tests.conftest import build_workspace

REPO = "Super/Repo"
ISSUE = 7


def _write(task_dir: Path, *, marker=True, fixes=True, commands=True, report=True) -> None:
    directory = task_dir / "handoff"
    directory.mkdir(parents=True, exist_ok=True)
    if report:
        (directory / "report.md").write_text("改动摘要：修复边界条件。验证：focused test 通过。风险：未跑集成测试。" * 2,
                                             encoding="utf-8")
    (directory / "comment.md").write_text(
        ("准备处理。\n\n" if marker else "") + f"<!-- agent-pr-workflow: {REPO}#{ISSUE} -->\n"
        if marker else "准备处理。\n", encoding="utf-8")
    (directory / "pr_body.md").write_text(
        (f"Fixes #{ISSUE}\n\n" if fixes else "") + "Generated-by: pr_agent\n", encoding="utf-8")
    (directory / "commands.md").write_text(
        ("git push -u origin fix/issue-7\ngh pr create --body-file handoff/pr_body.md\n"
         if commands else "手工执行\n"), encoding="utf-8")


def test_complete_handoff_passes(tmp_path):
    _write(tmp_path)
    result = handoff.validate(tmp_path, repo=REPO, issue_number=ISSUE)
    assert result["ok"], result["problems"]


def test_missing_files_blocked(tmp_path):
    (tmp_path / "handoff").mkdir()
    result = handoff.validate(tmp_path, repo=REPO, issue_number=ISSUE)
    assert not result["ok"]
    assert len(result["problems"]) >= 4


def test_missing_marker_blocked(tmp_path):
    _write(tmp_path, marker=False)
    result = handoff.validate(tmp_path, repo=REPO, issue_number=ISSUE)
    assert not result["ok"] and any("marker" in item for item in result["problems"])


def test_missing_fixes_link_blocked(tmp_path):
    _write(tmp_path, fixes=False)
    result = handoff.validate(tmp_path, repo=REPO, issue_number=ISSUE)
    assert any("未链接 Issue" in item for item in result["problems"])


def test_commands_without_push_blocked(tmp_path):
    _write(tmp_path, commands=False)
    result = handoff.validate(tmp_path, repo=REPO, issue_number=ISSUE)
    assert any("git push" in item for item in result["problems"])


class FakePool:
    def __init__(self) -> None:
        self.calls = []

    def transition(self, issue_id, to, *, actor="pr-agent", evidence=None):
        self.calls.append((issue_id, to, actor, evidence or {}))
        return {"id": issue_id, "status": to}


def _task(workspace) -> Task:
    return Task(
        task_id="Super__Repo__7", issue_id="Super/Repo#7", repo=REPO, issue_number=ISSUE,
        mode="assisted", agent="claude-code", adapter="claude_code", model="m",
        worktree=str(workspace.paths.worktrees_dir / "Super__Repo__7"),
        branch="fix/issue-7", base_sha="abc", base_branch="main",
        prompt_path="", prompt_sha256="", status="handoff_pending",
        handoff_state="pending_handoff", dossier=str(workspace.paths.dossier_path(REPO, ISSUE)),
    )


def test_finish_transitions_and_is_idempotent(tmp_path):
    ws = build_workspace(tmp_path)
    task_dir = ws.paths.task_dir("Super__Repo__7")
    _write(task_dir)
    pool = FakePool()
    task = _task(ws)
    result = handoff.finish(paths=ws.paths, pool=pool, task=task, task_dir=task_dir,
                            comment_url="https://github.com/Super/Repo/issues/7#issuecomment-1",
                            pr_url="https://github.com/Super/Repo/pull/12")
    assert result["handoff_state"] == "executed"
    assert [call[1] for call in pool.calls] == ["PR_OPEN", "PENDING_HUMAN"]
    again = handoff.finish(paths=ws.paths, pool=pool, task=task, task_dir=task_dir,
                           comment_url="https://github.com/Super/Repo/issues/7#issuecomment-1",
                           pr_url="https://github.com/Super/Repo/pull/12")
    assert again["already"] is True
    assert len(pool.calls) == 2
    state = json.loads((task_dir / "handoff" / "handoff.json").read_text(encoding="utf-8"))
    assert state["state"] == "executed"


def test_finish_rejects_bad_url(tmp_path):
    ws = build_workspace(tmp_path)
    task_dir = ws.paths.task_dir("Super__Repo__7")
    _write(task_dir)
    with pytest.raises(BlockedError):
        handoff.finish(paths=ws.paths, pool=FakePool(), task=_task(ws), task_dir=task_dir,
                       comment_url="not-a-url", pr_url="https://github.com/Super/Repo/pull/12")


def test_cancel_uses_allowed_path(tmp_path):
    ws = build_workspace(tmp_path)
    task_dir = ws.paths.task_dir("Super__Repo__7")
    pool = FakePool()
    result = handoff.cancel(paths=ws.paths, pool=pool, task=_task(ws), task_dir=task_dir, reason="方向不对")
    assert result["handoff_state"] == "canceled"
    assert [call[1] for call in pool.calls] == ["BLOCKED", "STALE"]
