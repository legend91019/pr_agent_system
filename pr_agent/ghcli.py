"""gh CLI 只读封装。

硬约束（DEVELOPMENT_PLAN §13）：
- 只读查询白名单：`auth status`、`pr list`、`pr view`、`pr checks`、`repo view`。
- 调度器自身永不留言、永不认领、永不 push、永不建 PR、永不合并/批准。
- 不读取、不打印任何 token；`gh auth status` 的输出仅用于判断登录态。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .errors import BlockedError

READONLY_PREFIXES = (
    ("auth", "status"),
    ("pr", "list"),
    ("pr", "view"),
    ("pr", "checks"),
    ("repo", "view"),
)


@dataclass
class GhResult:
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


def available() -> bool:
    return shutil.which("gh") is not None


def run_readonly(args: list[str], *, timeout: float = 60.0) -> GhResult:
    if not any(tuple(args[:2]) == prefix for prefix in READONLY_PREFIXES):
        raise BlockedError(
            f"拒绝执行非只读 gh 命令：{' '.join(args)}",
            detail="调度器只允许 §13 白名单内的只读查询；GitHub 写操作必须由 agent(full_autonomy) 或人工执行。",
        )
    if not available():
        raise BlockedError("找不到 gh CLI，请先安装并登录（见 GITHUB_TOKEN_SETUP.md）")
    env = os.environ.copy()
    env["GH_PAGER"] = ""
    env["GH_PROMPT_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    started = time.monotonic()
    completed = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )
    return GhResult(["gh", *args], completed.returncode, completed.stdout or "", completed.stderr or "",
                    time.monotonic() - started)


def auth_status() -> tuple[bool, str]:
    if not available():
        return False, "未在 PATH 中找到 gh CLI"
    try:
        result = run_readonly(["auth", "status"])
    except (BlockedError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.ok:
        return True, "已登录"
    return False, (result.stderr or result.stdout)[-1500:]


def find_pr(repo: str, branch: str) -> dict | None:
    """按 head 分支查找 PR（只读）。找不到返回 None，不猜测。"""
    result = run_readonly([
        "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
        "--json", "number,url,state,headRefName,isDraft", "--limit", "20",
    ])
    if result.ok:
        matches = _loads(result.stdout)
        for item in matches:
            if item.get("headRefName") == branch:
                return item
    # fork 场景 headRefName 为 `owner:branch`，需要扫描匹配
    listing = run_readonly([
        "pr", "list", "--repo", repo, "--state", "all",
        "--json", "number,url,state,headRefName,isDraft", "--limit", "100",
    ])
    if not listing.ok:
        return None
    for item in _loads(listing.stdout):
        head = str(item.get("headRefName", ""))
        if head == branch or head.endswith(f":{branch}"):
            return item
    return None


def pr_view(repo: str, number: int) -> dict:
    result = run_readonly([
        "pr", "view", str(number), "--repo", repo,
        "--json", "number,url,state,isDraft,mergeable,reviewDecision,statusCheckRollup,updatedAt,comments,headRefName,baseRefName",
    ])
    if not result.ok:
        raise BlockedError(
            f"gh pr view 失败：{repo}#{number}",
            detail=(result.stderr or result.stdout)[-1500:],
            evidence=result.as_evidence(),
        )
    return _loads(result.stdout)


def _loads(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []
