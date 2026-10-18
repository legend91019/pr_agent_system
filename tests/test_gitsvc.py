"""git 操作：worktree 幂等、分支冲突、真实 diff 采集。"""

from __future__ import annotations

import subprocess

import pytest

from pr_agent import gitsvc
from pr_agent.errors import BlockedError

from tests.conftest import build_workspace


def _commit(path, name="x.py", content="v = 2\n", message="change"):
    (path / name).write_text(content, encoding="utf-8")
    gitsvc.git(["add", "-A"], path, check=True)
    gitsvc.git(["-c", "user.email=a@b.c", "-c", "user.name=A", "commit", "-m", message], path, check=True)


def test_create_worktree_and_reuse(tmp_path):
    ws = build_workspace(tmp_path)
    target = ws.paths.worktrees_dir / "Super__Repo__7"
    created = gitsvc.create_worktree(ws.repo_dir, target, "fix/issue-7", ws.base_sha)
    assert created["reused"] == "false"
    assert (target / "README.md").is_file()
    again = gitsvc.create_worktree(ws.repo_dir, target, "fix/issue-7", ws.base_sha)
    assert again["reused"] == "true"


def test_existing_branch_is_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    gitsvc.git(["branch", "fix/issue-7", ws.base_sha], ws.repo_dir, check=True)
    with pytest.raises(BlockedError):
        gitsvc.create_worktree(ws.repo_dir, ws.paths.worktrees_dir / "t", "fix/issue-7", ws.base_sha)


def test_collect_diff_covers_commit_and_worktree(tmp_path):
    ws = build_workspace(tmp_path)
    target = ws.paths.worktrees_dir / "Super__Repo__7"
    gitsvc.create_worktree(ws.repo_dir, target, "fix/issue-7", ws.base_sha)
    _commit(target, "package.py", "def value():\n    return 2\n")
    (target / "untracked.txt").write_text("hi", encoding="utf-8")
    diff = gitsvc.collect_diff(target, ws.base_sha)
    assert diff["committed_changed_lines"] >= 2
    assert diff["committed_files"][0]["path"] == "package.py"
    assert diff["working_tree_dirty"] is True
    assert "untracked.txt" in diff["untracked_files"]
    assert "-    return 1" in diff["patch"] and "+    return 2" in diff["patch"]


def test_ensure_repo_rejects_missing(tmp_path):
    with pytest.raises(BlockedError):
        gitsvc.ensure_repo(tmp_path / "nope", "A/B")


def test_default_branch_fallback(tmp_path):
    ws = build_workspace(tmp_path)
    assert gitsvc.default_branch(ws.repo_dir) in {"master", "main"}
