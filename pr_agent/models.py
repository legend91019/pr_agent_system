"""任务记录（tasks/<task_id>/task.json）。

task_id = <owner>__<repo>__<issue>（§9.2）。任务目录可重建，状态真相在池子。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import now_iso

TASK_STATUSES = {
    "packed",          # 已打包，尚未执行
    "running",         # agent 正在执行
    "ready",           # agent 完成，等待人工在 GitHub 执行（assisted）或已创建 PR（full_autonomy）
    "handoff_pending", # assisted 交付包已就绪
    "handoff_done",    # 人工已执行并回填
    "blocked",         # 失败/超时/证据缺失，必须人工介入
    "canceled",        # 人工取消
}
HANDOFF_STATES = {"pending_handoff", "executed", "canceled"}


def make_task_id(repo: str, issue_number: int) -> str:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"非法 repo：{repo}")
    return f"{owner}__{name}__{issue_number}"


@dataclass
class Task:
    task_id: str
    issue_id: str
    repo: str
    issue_number: int
    mode: str
    agent: str
    adapter: str
    model: str | None
    worktree: str
    branch: str
    base_sha: str
    base_branch: str
    prompt_path: str
    prompt_sha256: str
    status: str = "packed"
    handoff_state: str | None = None
    exit_code: int | None = None
    dossier: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in TASK_STATUSES:
            raise ValueError(f"非法任务状态：{self.status}")
        if self.handoff_state is not None and self.handoff_state not in HANDOFF_STATES:
            raise ValueError(f"非法 handoff 状态：{self.handoff_state}")

    # --- 持久化 ---
    def to_dict(self) -> dict[str, Any]:
        payload = {key: value for key, value in self.__dict__.items()}
        return payload

    def save(self, tasks_dir: Path) -> Path:
        directory = tasks_dir / self.task_id
        directory.mkdir(parents=True, exist_ok=True)
        self.updated_at = now_iso()
        path = directory / "task.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    @classmethod
    def load(cls, task_dir: Path) -> "Task":
        payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        return cls(**payload)

    def note(self, message: str) -> None:
        self.notes.append(f"{now_iso()} {message}")


def load_task(task_dir: Path) -> Task:
    return Task.load(task_dir)


def list_tasks(tasks_dir: Path) -> list[Task]:
    tasks: list[Task] = []
    if not tasks_dir.is_dir():
        return tasks
    for directory in sorted(tasks_dir.iterdir()):
        if not directory.is_dir() or not (directory / "task.json").is_file():
            continue
        try:
            tasks.append(Task.load(directory))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return tasks
