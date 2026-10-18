"""assisted 交付包（handoff）校验与回填（DEVELOPMENT_PLAN §6.3）。

四件套缺一即 BLOCKED：report.md / comment.md / pr_body.md / commands.md。
人工执行后用 `pr_agent task handoff-done` 回填 URL，调度器才把池记录推进到 PR_OPEN → PENDING_HUMAN。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import BlockedError
from .events import now_iso, pool_summary_event
from .models import Task
from .paths import Paths
from .poolctl import Pool

REQUIRED_FILES = ("report.md", "comment.md", "pr_body.md", "commands.md")
URL_PATTERN = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/(?:pull|issues)/\d+(?:[#?][^\s]*)?$")


def marker_for(repo: str, issue_number: int) -> str:
    return f"<!-- agent-pr-workflow: {repo}#{issue_number} -->"


def handoff_dir(task_dir: Path) -> Path:
    return task_dir / "handoff"


def validate(task_dir: Path, *, repo: str, issue_number: int) -> dict:
    directory = handoff_dir(task_dir)
    files: dict[str, dict] = {}
    problems: list[str] = []
    warnings: list[str] = []
    for name in REQUIRED_FILES:
        path = directory / name
        if not path.is_file():
            problems.append(f"缺少 {name}")
            files[name] = {"exists": False}
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{name} 无法读取：{exc}")
            files[name] = {"exists": True, "readable": False}
            continue
        files[name] = {"exists": True, "bytes": path.stat().st_size, "chars": len(text)}
        if not text.strip():
            problems.append(f"{name} 为空")

    comment = _read(directory / "comment.md")
    if comment and marker_for(repo, issue_number) not in comment:
        problems.append(f"comment.md 缺少防重 marker：{marker_for(repo, issue_number)}")

    body = _read(directory / "pr_body.md")
    if body:
        if not re.search(rf"(?:Fixes|Closes|Resolves)\s+#{issue_number}\b", body, re.IGNORECASE):
            problems.append(f"pr_body.md 未链接 Issue（需 Fixes/Closes/Resolves #{issue_number}）")
        lower = body.lower()
        if "generated-by" not in lower and "ai" not in lower:
            warnings.append("pr_body.md 未看到 AI 贡献披露（Generated-by / AI 说明），请按目标仓库规则确认")

    commands = _read(directory / "commands.md")
    if commands:
        for needle in ("git push", "gh pr create"):
            if needle not in commands:
                problems.append(f"commands.md 缺少 `{needle}` 命令")
        if "pr_body.md" not in commands:
            problems.append("commands.md 未引用 pr_body.md（--body-file 路径）")

    report = _read(directory / "report.md")
    if report and len(report.strip()) < 120:
        warnings.append("report.md 过短，可能未覆盖改动摘要/验证结果/风险")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "files": files,
        "validated_at": now_iso(),
    }


def write_state(task_dir: Path, state: dict) -> Path:
    directory = handoff_dir(task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "handoff.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def finish(
    *,
    paths: Paths,
    pool: Pool,
    task: Task,
    task_dir: Path,
    comment_url: str,
    pr_url: str,
) -> dict:
    """人工执行后的回填：PR_READY → PR_OPEN → PENDING_HUMAN。"""
    for label, url in (("comment_url", comment_url), ("pr_url", pr_url)):
        if not URL_PATTERN.match(url or ""):
            raise BlockedError(
                f"回填的 {label} 不是合法 GitHub URL：{url}",
                detail="需形如 https://github.com/OWNER/REPO/pull/123 或 /issues/123",
            )
    if task.handoff_state == "executed":
        return {"already": True, "handoff_state": task.handoff_state, "note": "已回填，跳过（幂等）"}

    state_path = handoff_dir(task_dir) / "handoff.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    state.update({
        "state": "executed",
        "comment_url": comment_url,
        "pr_url": pr_url,
        "executed_at": now_iso(),
    })
    write_state(task_dir, state)

    pool.transition(
        task.issue_id, "PR_OPEN", actor="human",
        evidence={"comment_url": comment_url, "pr_url": pr_url, "task_id": task.task_id, "mode": "assisted"},
    )
    pool.transition(
        task.issue_id, "PENDING_HUMAN", actor="pr-agent",
        evidence={"pr_url": pr_url, "dossier": task.dossier, "task_id": task.task_id},
    )
    pool_summary_event(
        paths, issue_id=task.issue_id, kind="HANDOFF_EXECUTED", task_id=task.task_id,
        evidence={"comment_url": comment_url, "pr_url": pr_url},
    )
    task.handoff_state = "executed"
    task.status = "handoff_done"
    task.note(f"人工回填：{comment_url} / {pr_url}")
    task.save(paths.tasks_dir)
    return {"already": False, "handoff_state": "executed", "comment_url": comment_url, "pr_url": pr_url}


def cancel(*, paths: Paths, pool: Pool, task: Task, task_dir: Path, reason: str) -> dict:
    """人工放弃：handoff_state=canceled，池记录 PR_READY → BLOCKED → STALE（pool.py 仅允许此路径）。"""
    if task.handoff_state == "canceled":
        return {"already": True}
    if not reason.strip():
        raise BlockedError("取消必须记录原因（--reason）")
    state_path = handoff_dir(task_dir) / "handoff.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    state.update({"state": "canceled", "reason": reason, "canceled_at": now_iso()})
    write_state(task_dir, state)
    pool.transition(task.issue_id, "BLOCKED", actor="human",
                    evidence={"reason": reason, "task_id": task.task_id, "stage": "handoff_canceled"})
    pool.transition(task.issue_id, "STALE", actor="pr-agent",
                    evidence={"reason": f"handoff canceled: {reason}", "task_id": task.task_id})
    pool_summary_event(paths, issue_id=task.issue_id, kind="HANDOFF_CANCELED",
                       task_id=task.task_id, evidence={"reason": reason})
    task.handoff_state = "canceled"
    task.status = "canceled"
    task.note(f"人工取消：{reason}")
    task.save(paths.tasks_dir)
    return {"already": False, "handoff_state": "canceled", "reason": reason}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
