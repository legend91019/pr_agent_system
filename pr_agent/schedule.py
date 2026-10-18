"""Windows 任务计划注册（schtasks，§7.3）。

做法：先为每个作业生成一个 `.cmd` 包装脚本（`scripts\\job-<job>.cmd`），
再注册 `schtasks` 指向该脚本。直接往 `/TR` 里塞带引号的长命令在 Windows 上
极易被截断（嵌套引号 + `&&` + 重定向），包装脚本是唯一稳定做法。

安全约束：
- 默认只打印命令；真正注册需要显式 `--confirm`。
- 注册前提示 open_coding 当前 `execution_mode: manual_only`，启用定时前须人工确认放开。
- 不使用 SYSTEM 账户，任务以当前用户运行，避免权限放大。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PrAgentError
from .paths import Paths

TASK_PREFIX = "pr_agent-"

SCHEDULES: tuple[tuple[str, str, list[str]], ...] = (
    ("pool-maint", "pool-maint", ["/SC", "HOURLY", "/MO", "6", "/ST", "00:15"]),
    ("monitor", "monitor", ["/SC", "HOURLY", "/MO", "6", "/ST", "00:45"]),
    ("run-day", "run-day", ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "09:05"]),
)


@dataclass
class SchedulePlan:
    name: str
    job: str
    wrapper: Path
    script: str
    schedule: list[str] = field(default_factory=list)

    @property
    def command(self) -> list[str]:
        return ["schtasks", "/Create", "/TN", self.name, "/TR", _tr(self.wrapper), *self.schedule, "/F"]

    def display(self) -> str:
        return " ".join(_quote(item) for item in self.command)


def build_plans(paths: Paths, python: str | None = None) -> list[SchedulePlan]:
    exe = python or sys.executable
    plans: list[SchedulePlan] = []
    for name, job, schedule in SCHEDULES:
        wrapper = paths.workspace / "scripts" / f"job-{job}.cmd"
        script = "\r\n".join([
            "@echo off",
            "setlocal",
            f'cd /d "{paths.workspace}"',
            f'"{exe}" -m pr_agent job {job} >> "logs\\job-{job}.log" 2>&1',
            "endlocal",
            "",
        ])
        plans.append(SchedulePlan(
            name=f"{TASK_PREFIX}{name}", job=job, wrapper=wrapper, script=script, schedule=list(schedule),
        ))
    return plans


def install(paths: Paths, *, confirm: bool = False, dry_run: bool = True, python: str | None = None) -> dict:
    plans = build_plans(paths, python)
    if dry_run or not confirm:
        return {
            "status": "dry_run",
            "note": "未注册任何任务；确认后加 --confirm 执行。open_coding 当前 execution_mode=manual_only，启用定时前须人工确认放开。",
            "commands": [plan.display() for plan in plans],
            "wrappers": [{"path": str(plan.wrapper), "content": plan.script} for plan in plans],
        }
    paths.ensure_local_dirs()
    executed = []
    for plan in plans:
        plan.wrapper.parent.mkdir(parents=True, exist_ok=True)
        plan.wrapper.write_text(plan.script, encoding="utf-8")
        completed = subprocess.run(plan.command, capture_output=True, text=True, errors="replace")
        executed.append({
            "task": plan.name,
            "wrapper": str(plan.wrapper),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-1000:],
            "stderr": completed.stderr[-1000:],
        })
        if completed.returncode != 0:
            raise PrAgentError(
                f"注册计划任务失败：{plan.name}",
                detail=(completed.stderr or completed.stdout)[-2000:],
                evidence={"command": plan.display(), "wrapper": str(plan.wrapper)},
            )
    return {"status": "installed", "tasks": executed}


def uninstall(paths: Paths | None = None, *, confirm: bool = False, dry_run: bool = True) -> dict:
    names = [f"{TASK_PREFIX}{name}" for name, _, _ in SCHEDULES]
    commands = [f'schtasks /Delete /TN "{name}" /F' for name in names]
    if dry_run or not confirm:
        return {"status": "dry_run", "commands": commands}
    results = []
    for name in names:
        completed = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                                   capture_output=True, text=True, errors="replace")
        results.append({"task": name, "returncode": completed.returncode,
                        "output": (completed.stdout or completed.stderr)[-800:]})
    return {"status": "uninstalled", "tasks": results}


def _tr(path: Path) -> str:
    """`/TR` 取值：路径含空格时用 `cmd /c ""路径""` 形式，schtasks 才不会截断。"""
    text = str(path)
    if " " in text:
        return f'cmd /c ""{text}""'
    return f"cmd /c {text}"


def _quote(item: str) -> str:
    return f'"{item}"' if " " in item and not item.startswith('"') else item
