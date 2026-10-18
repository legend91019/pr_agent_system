"""路径与资产自检。

写入边界（DEVELOPMENT_PLAN §2.2）：
- pr_agent 只写本仓库的 tasks/、logs/、config/，以及 open_coding 的
  pool/*.jsonl（经 pool.py）、pr_reviews/、worktrees/。
- open_coding 的 workflow/、docs/、ISSUE_SELECTION_RULES.md、GITHUB_TOKEN_SETUP.md、
  scripts/ 源码为只读权威，本模块只读取与自检。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import AssetMissingError

DEFAULT_WORKSPACE = Path(r"D:\Desktop\personal_agent")
DEFAULT_OPEN_CODING = Path(r"D:\Desktop\open_coding")

# 键 -> open_coding 内相对路径（§2.3 复用资产清单的机器可读形式）
REQUIRED_ASSETS: dict[str, str] = {
    "pool_cli": "scripts/pool.py",
    "issue_audit": "scripts/issue_audit.py",
    "canonical_prompt": "workflow/prompt.md",
    "workflow_states": "workflow/states.md",
    "workflow_schema": "workflow/schema.json",
    "workflow_config": "workflow/config.yaml",
    "dossier_renderer": "scripts/render_pr_review.py",
    "token_setup": "GITHUB_TOKEN_SETUP.md",
    "operator_guide": "workflow/operator-guide.md",
    "automation_contract": "workflow/automation.md",
    "selection_rules": "ISSUE_SELECTION_RULES.md",
}


@dataclass(frozen=True)
class Paths:
    """所有落位路径的唯一来源。"""

    workspace: Path
    open_coding: Path

    # --- 本仓库 ---
    @property
    def package_dir(self) -> Path:
        return Path(__file__).resolve().parent

    @property
    def tasks_dir(self) -> Path:
        return self.workspace / "tasks"

    @property
    def logs_dir(self) -> Path:
        return self.workspace / "logs"

    @property
    def config_dir(self) -> Path:
        return self.workspace / "config"

    @property
    def agents_config(self) -> Path:
        return self.config_dir / "agents.yaml"

    @property
    def projects_config(self) -> Path:
        return self.config_dir / "projects.yaml"

    @property
    def usage_ledger(self) -> Path:
        return self.logs_dir / "usage.json"

    @property
    def breaker_state(self) -> Path:
        return self.logs_dir / "breaker.json"

    @property
    def daemon_log(self) -> Path:
        return self.logs_dir / "daemon.log"

    @property
    def locks_dir(self) -> Path:
        return self.logs_dir / "locks"

    # --- open_coding（只读权威 + 受控写入的产物目录）---
    @property
    def pool_cli(self) -> Path:
        return self.open_coding / "scripts" / "pool.py"

    @property
    def renderer(self) -> Path:
        return self.open_coding / "scripts" / "render_pr_review.py"

    @property
    def canonical_prompt(self) -> Path:
        return self.open_coding / "workflow" / "prompt.md"

    @property
    def pool_issues(self) -> Path:
        return self.open_coding / "pool" / "issues.jsonl"

    @property
    def pool_events(self) -> Path:
        return self.open_coding / "pool" / "events.jsonl"

    @property
    def repos_dir(self) -> Path:
        return self.open_coding / "repos"

    @property
    def worktrees_dir(self) -> Path:
        override = os.environ.get("PR_AGENT_WORKTREES")
        if override:
            return Path(override)
        return self.open_coding / "worktrees"

    @property
    def reviews_dir(self) -> Path:
        return self.open_coding / "pr_reviews"

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def repo_dir(self, repo: str) -> Path:
        return self.repos_dir / repo.split("/")[-1]

    def dossier_path(self, repo: str, issue_number: int) -> Path:
        return self.reviews_dir / f"{repo.replace('/', '__')}__{issue_number}.md"

    # --- 生命周期 ---
    def ensure_local_dirs(self) -> None:
        for path in (self.tasks_dir, self.logs_dir, self.config_dir, self.locks_dir,
                     self.worktrees_dir, self.reviews_dir):
            path.mkdir(parents=True, exist_ok=True)

    def missing_assets(self) -> list[tuple[str, Path]]:
        return [
            (key, self.open_coding / relative)
            for key, relative in REQUIRED_ASSETS.items()
            if not (self.open_coding / relative).is_file()
        ]

    def require_assets(self) -> None:
        missing = self.missing_assets()
        if missing:
            listing = "\n".join(f"- {key}: {path}" for key, path in missing)
            raise AssetMissingError(
                f"open_coding 权威资产缺失，任务必须 BLOCKED：{self.open_coding}",
                detail=listing,
                evidence={"missing": [{"key": k, "path": str(p)} for k, p in missing]},
            )


def load_paths(workspace: str | Path | None = None, open_coding: str | Path | None = None) -> Paths:
    """解析工作区与 open_coding 根目录（命令行 > 环境变量 > 默认）。"""
    workspace_path = Path(
        workspace or os.environ.get("PR_AGENT_WORKSPACE") or DEFAULT_WORKSPACE
    ).expanduser().resolve()
    open_coding_path = Path(
        open_coding or os.environ.get("PR_AGENT_OPEN_CODING") or DEFAULT_OPEN_CODING
    ).expanduser().resolve()
    return Paths(workspace=workspace_path, open_coding=open_coding_path)
