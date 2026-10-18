"""Git 操作：worktree 创建、base SHA、真实 diff 采集。

约束：本模块永不 push、永不切换/修改 main/master、永不删除他人分支。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import BlockedError


@dataclass
class GitResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_evidence(self) -> dict:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
        }


def git(args: list[str], cwd: Path, *, timeout: float = 180.0, check: bool = False) -> GitResult:
    argv = ["git", *args]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise BlockedError(
            f"git 命令超时：{' '.join(args)}",
            detail=str(exc),
            evidence={"argv": argv, "cwd": str(cwd)},
        ) from exc
    except OSError as exc:
        raise BlockedError(f"无法执行 git：{exc}", evidence={"argv": argv}) from exc
    result = GitResult(argv, completed.returncode, completed.stdout or "", completed.stderr or "",
                       time.monotonic() - started)
    if check and not result.ok:
        raise BlockedError(
            f"git 命令失败：{' '.join(args)}",
            detail=(result.stderr or result.stdout)[-4000:],
            evidence=result.as_evidence(),
        )
    return result


def is_repo(path: Path) -> bool:
    return path.is_dir() and git(["rev-parse", "--is-inside-work-tree"], path).ok


def resolve_repo_dir(root: Path, repo: str) -> Path:
    return root / repo.split("/")[-1]


def ensure_repo(path: Path, repo: str) -> Path:
    if not is_repo(path):
        raise BlockedError(
            f"本地仓库不存在或不是 git 仓库：{path}",
            detail=f"请先执行 `pr_agent repo add {repo}`（gh repo clone），或手工 clone 到该路径。",
            evidence={"repo": repo, "path": str(path)},
        )
    return path


def head_sha(path: Path, rev: str = "HEAD") -> str:
    result = git(["rev-parse", rev], path)
    if not result.ok:
        raise BlockedError(f"无法解析 git rev：{rev}", detail=result.stderr, evidence=result.as_evidence())
    return result.stdout.strip()


def commit_exists(path: Path, sha: str) -> bool:
    return git(["cat-file", "-e", f"{sha}^{{commit}}"], path).ok


def default_branch(path: Path) -> str:
    head = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    if head.ok and head.stdout.strip():
        return head.stdout.strip().split("/", 1)[-1]
    for candidate in ("main", "master"):
        if git(["rev-parse", "--verify", f"origin/{candidate}"], path).ok or \
           git(["rev-parse", "--verify", candidate], path).ok:
            return candidate
    raise BlockedError("无法判断默认分支（origin/HEAD、main、master 均不可用）", evidence={"path": str(path)})


def worktrees(path: Path) -> list[dict[str, str]]:
    result = git(["worktree", "list", "--porcelain"], path)
    if not result.ok:
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def create_worktree(repo_dir: Path, worktree_path: Path, branch: str, base: str) -> dict[str, str]:
    """幂等创建 worktree：同路径同分支已存在则复用，其它冲突一律失败。"""
    for entry in worktrees(repo_dir):
        existing = Path(entry.get("worktree", "")).resolve()
        if existing == worktree_path.resolve():
            if entry.get("branch", "").endswith(branch):
                return {"worktree": str(worktree_path), "branch": branch, "reused": "true"}
            raise BlockedError(
                f"worktree 路径已被其它分支占用：{worktree_path}",
                detail=f"现有分支：{entry.get('branch')}；请求分支：{branch}",
                evidence={"worktree": str(worktree_path), "branch": entry.get("branch")},
            )
    if worktree_path.exists() and any(worktree_path.iterdir()):
        raise BlockedError(
            f"worktree 目录已存在且非空，但不是注册的 worktree：{worktree_path}",
            evidence={"worktree": str(worktree_path)},
        )
    if git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo_dir).ok:
        raise BlockedError(
            f"分支已存在，拒绝覆盖：{branch}",
            detail="为避免串用上下文，请人工确认后删除该分支或改用新的 task_id。",
            evidence={"branch": branch},
        )
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = git(["worktree", "add", "-b", branch, str(worktree_path), base], repo_dir)
    if not result.ok:
        raise BlockedError(
            f"创建 worktree 失败：{worktree_path}",
            detail=(result.stderr or result.stdout)[-4000:],
            evidence=result.as_evidence(),
        )
    return {"worktree": str(worktree_path), "branch": branch, "reused": "false"}


def collect_diff(worktree: Path, base: str) -> dict:
    """采集 base..HEAD 与工作区未提交改动的真实证据。"""
    head = head_sha(worktree)
    name_status = git(["diff", "--name-status", f"{base}..HEAD"], worktree)
    numstat = git(["diff", "--numstat", f"{base}..HEAD"], worktree)
    committed = git(["diff", "--binary", f"{base}..HEAD"], worktree)
    status = git(["status", "--porcelain=v1"], worktree)
    working = git(["diff"], worktree)
    staged = git(["diff", "--cached"], worktree)

    files: list[dict[str, str]] = []
    for line in name_status.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0], "path": parts[-1]})

    added = deleted = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            deleted += int(parts[1])

    untracked = [line[3:] for line in status.stdout.splitlines() if line.startswith("??")]
    dirty_lines = [line for line in status.stdout.splitlines() if line.strip()]
    patch_parts = []
    if committed.stdout.strip():
        patch_parts.append(f"# committed: {base}..HEAD\n{committed.stdout}")
    if staged.stdout.strip():
        patch_parts.append(f"# staged (vs HEAD)\n{staged.stdout}")
    if working.stdout.strip():
        patch_parts.append(f"# unstaged working tree\n{working.stdout}")
    return {
        "head_sha": head,
        "base_sha": base,
        "committed_files": files,
        "committed_changed_lines": added + deleted,
        "committed_added_lines": added,
        "committed_deleted_lines": deleted,
        "working_tree_dirty": bool(dirty_lines),
        "working_tree_status": dirty_lines,
        "untracked_files": untracked,
        "patch": "\n\n".join(patch_parts),
        "commands": [r.as_evidence() for r in (name_status, numstat, status)],
    }


def commit_all(worktree: Path, message: str) -> GitResult:
    """仅在人工显式要求时使用；调度器自身不提交。"""
    git(["add", "-A"], worktree, check=True)
    return git(["commit", "-m", message], worktree)
