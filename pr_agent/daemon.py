"""内置守护进程：单进程 sleep 循环，按时间表拉起三个作业（§7.3）。

不使用 heartbeat/cron 元数据，不修改 open_coding 的任何自动化开关；
是否启用由人工通过 `pr_agent daemon` 显式启动。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import AgentsConfig, ProjectsConfig
from .events import SHANGHAI, now_iso
from .jobs import run_job
from .paths import Paths

JOB_INTERVALS = {"pool-maint": 6, "monitor": 6}
RUN_DAY_TIME = (9, 5)


def _log(paths: Paths, message: str) -> None:
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    with (paths.logs_dir / "daemon.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def _state_path(paths: Paths) -> Path:
    return paths.logs_dir / "daemon-state.json"


def _load_state(paths: Paths) -> dict:
    path = _state_path(paths)
    if not path.is_file():
        return {"last_run": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_run": {}}


def _save_state(paths: Paths, state: dict) -> None:
    path = _state_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def due_jobs(now: datetime, state: dict) -> list[str]:
    """判断当前时刻哪些作业到期（本地 Asia/Shanghai 时间）。"""
    due: list[str] = []
    for job, interval in JOB_INTERVALS.items():
        last = _parse(state.get("last_run", {}).get(job))
        if last is None or now - last >= timedelta(hours=interval):
            due.append(job)
    last_daily = _parse(state.get("last_run", {}).get("run-day"))
    scheduled = now.replace(hour=RUN_DAY_TIME[0], minute=RUN_DAY_TIME[1], second=0, microsecond=0)
    if now.weekday() < 5 and now >= scheduled:
        if last_daily is None or last_daily.date() < now.date():
            due.append("run-day")
    return due


def run_daemon(
    paths: Paths,
    config: AgentsConfig,
    projects: ProjectsConfig,
    *,
    once: bool = False,
    max_hours: float | None = None,
    dry_run: bool = False,
    poll_seconds: float = 30.0,
) -> int:
    paths.ensure_local_dirs()
    _log(paths, f"daemon 启动 once={once} dry_run={dry_run} max_hours={max_hours}")
    started = time.monotonic()
    executed: set[str] = set()
    try:
        while True:
            now = datetime.now(SHANGHAI)
            state = _load_state(paths)
            jobs = ["pool-maint", "run-day", "monitor"] if once else due_jobs(now, state)
            if once:
                jobs = [job for job in jobs if job not in executed]
            for job in jobs:
                _log(paths, f"job {job} 开始 dry_run={dry_run}")
                try:
                    result = run_job(job, paths, config, projects, dry_run=dry_run)
                    _log(paths, f"job {job} 结束：{result.status} {result.message}")
                except Exception as exc:  # 守护进程必须存活并保留原始错误
                    _log(paths, f"job {job} 异常：{type(exc).__name__}: {exc}")
                state = _load_state(paths)
                state.setdefault("last_run", {})[job] = now.replace(microsecond=0).isoformat()
                _save_state(paths, state)
                executed.add(job)
            if once:
                _log(paths, "daemon once 模式完成")
                return 0
            if max_hours is not None and (time.monotonic() - started) / 3600 >= max_hours:
                _log(paths, "daemon 达到 max_hours，退出")
                return 0
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        _log(paths, "daemon 收到中断，退出")
        return 130
