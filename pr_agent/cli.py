"""命令行界面（DEVELOPMENT_PLAN §9.1）。

设计原则：
- 状态只读池子；所有写操作都要么经 pool.py，要么是显式人工动作（handoff-done / cancel / whitelist approve）。
- 失败不吞：打印原始错误与证据路径，退出码非 0。
"""

from __future__ import annotations

import json
import subprocess
import sys

import typer

from . import __version__, ghcli, governance, handoff, schedule
from .adapters import probe_all
from .config import AgentsConfig, ProjectsConfig, load_agents_config, load_projects_config
from .daemon import run_daemon
from .errors import PrAgentError
from .events import read_jsonl
from .jobs import CircuitBreaker, pool_maint, run_job
from .locks import active_slots
from .models import list_tasks, load_task
from .paths import Paths, load_paths
from .poolctl import Pool
from .quota import load_ledger
from .runner import Runner, cancel_task

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="pr_agent：多 coding agent PR 调度系统（人工终审）")
task_app = typer.Typer(no_args_is_help=True, help="任务级操作")
job_app = typer.Typer(no_args_is_help=True, help="定时作业（幂等）")
whitelist_app = typer.Typer(no_args_is_help=True, help="白名单治理")
repo_app = typer.Typer(no_args_is_help=True, help="本地仓库准备")
app.add_typer(task_app, name="task")
app.add_typer(job_app, name="job")
app.add_typer(whitelist_app, name="whitelist")
app.add_typer(repo_app, name="repo")

GLOBAL = {"workspace": None, "open_coding": None}


@app.callback()
def _main(
    workspace: str = typer.Option(None, "--workspace", help="工作区根目录（默认 D:\\Desktop\\personal_agent）"),
    open_coding: str = typer.Option(None, "--open-coding", help="open_coding 根目录（只读权威）"),
    version: bool = typer.Option(False, "--version", help="显示版本"),
) -> None:
    if version:
        typer.echo(f"pr_agent {__version__}")
        raise typer.Exit()
    GLOBAL["workspace"] = workspace
    GLOBAL["open_coding"] = open_coding


def _ctx(mode: str | None = None) -> tuple[Paths, AgentsConfig, ProjectsConfig]:
    paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
    config = load_agents_config(paths, mode=mode)
    projects = load_projects_config(paths)
    return paths, config, projects


def _echo(payload, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(exc: PrAgentError, as_json: bool) -> None:
    """失败一律非 0：JSON 模式额外给结构化错误，供自动化消费；人读信息走 stderr。"""
    if as_json:
        typer.echo(json.dumps({
            "status": "blocked",
            "error": exc.message,
            "detail": exc.detail,
            "evidence": exc.evidence,
        }, ensure_ascii=False, indent=2))
    typer.secho(f"[中止] {exc.message}", fg="red", err=True)
    if exc.detail:
        typer.secho(exc.detail, err=True)


def _exit_for(status: str) -> int:
    return {"ok": 0, "dry_run": 0, "ready": 0, "handoff_pending": 0, "handoff_done": 0,
            "packed": 0, "queued": 2, "skipped": 2, "partial": 2, "blocked": 3,
            "canceled": 0}.get(status, 0)


# ---------------------------------------------------------------- status / doctor
def render_status(paths: Paths, config: AgentsConfig, projects: ProjectsConfig | None = None) -> str:
    lines = [f"pr_agent {__version__}",
             f"工作区：{paths.workspace}",
             f"open_coding：{paths.open_coding}",
             f"权限模式：{config.mode}（max_parallel={config.max_parallel}）",
             f"fallback_chain：{' → '.join(config.fallback_chain)}"]
    if config.config_warnings:
        lines.append("配置提醒：" + "；".join(config.config_warnings))
    missing = paths.missing_assets()
    lines.append("资产自检：" + ("OK" if not missing else "缺失 " + ", ".join(key for key, _ in missing)))

    try:
        health = Pool(paths).health()
        status_counts = health.get("by_status", {})
        lines.append(f"池子合计：{health.get('total', 0)}")
        lines.append("  状态分布：" + ("，".join(f"{k}={v}" for k, v in status_counts.items()) or "空"))
    except PrAgentError as exc:
        lines.append(f"池子：UNKNOWN（{exc.message}）")

    ledger = load_ledger(paths)
    quota_lines = [
        f"  {item['agent']}: {item['budget_type']}，已用 {item['used']}，剩余 "
        f"{'不限' if item['left'] is None else item['left']}"
        + (f"，冷却至 {item['cooldown_until']}" if item.get("cooldown_until") else "")
        for item in ledger.snapshot(config.agents)
    ]
    lines.append("额度：")
    lines.extend(quota_lines)

    tasks = list_tasks(paths.tasks_dir)
    active = [task for task in tasks if task.status in {"packed", "running", "ready", "handoff_pending"}]
    lines.append(f"任务：{len(tasks)} 个（在途 {len(active)}）")
    for task in active[:10]:
        lines.append(f"  - {task.task_id} [{task.status}/{task.handoff_state or '-'}] {task.agent} {task.mode}")
    slots = active_slots(paths)
    lines.append(f"并发槽占用：{len(slots)}/{config.max_parallel}" + (f"（{', '.join(slots)}）" if slots else ""))
    breaker = CircuitBreaker(paths.breaker_state).state()
    if breaker.get("opened_on"):
        lines.append(f"断路器：熔断中（{breaker['opened_on']}，连续失败 {breaker.get('consecutive_failures')}）")
    if projects is not None:
        pending = projects.pending_proposals()
        if pending:
            lines.append("待批准白名单提案：" + "，".join(entry.repo for entry in pending))
    return "\n".join(lines)


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """池子/额度/任务/模式 一屏。"""
    try:
        paths, config, projects = _ctx()
    except PrAgentError as exc:
        typer.secho(f"[配置错误] {exc}", fg="red", err=True)
        raise typer.Exit(1)
    text = render_status(paths, config, projects)
    if as_json:
        payload = {"workspace": str(paths.workspace), "mode": config.mode,
                   "fallback_chain": config.fallback_chain,
                   "missing_assets": [key for key, _ in paths.missing_assets()],
                   "tasks": [task.to_dict() for task in list_tasks(paths.tasks_dir)]}
        _echo(payload, True)
    else:
        typer.echo(text)


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """环境/资产/adapter 自检（不产生任何外部动作）。"""
    paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
    report: dict = {"python": sys.version.split()[0], "workspace": str(paths.workspace),
                    "open_coding": str(paths.open_coding)}
    report["missing_assets"] = [{"key": key, "path": str(path)} for key, path in paths.missing_assets()]
    try:
        config = load_agents_config(paths)
        report["config"] = {"ok": True, "mode": config.mode, "warnings": config.config_warnings}
        report["adapters"] = probe_all(config.agents)
        report["frontend_gaps"] = config.frontend.describe_gap()
    except PrAgentError as exc:
        report["config"] = {"ok": False, "error": str(exc), "detail": exc.detail}
    try:
        projects = load_projects_config(paths)
        report["projects"] = {"ok": True, "whitelist": [e.repo for e in projects.whitelist],
                              "blacklist": [e.repo for e in projects.blacklist],
                              "pending": [e.repo for e in projects.pending_proposals()]}
    except PrAgentError as exc:
        report["projects"] = {"ok": False, "error": str(exc)}
    ok, detail = ghcli.auth_status()
    report["gh"] = {"available": ghcli.available(), "authenticated": ok, "detail": detail[-500:]}
    try:
        workflow = json.loads((paths.open_coding / "workflow" / "config.yaml").read_text(encoding="utf-8"))
        report["workflow"] = {"execution_mode": workflow.get("execution_mode"),
                              "automation_enabled": workflow.get("automation_enabled")}
    except (OSError, json.JSONDecodeError) as exc:
        report["workflow"] = {"error": str(exc)}
    report["writable"] = _writable(paths)
    _echo(report, as_json)
    if report["missing_assets"]:
        raise typer.Exit(3)


def _writable(paths: Paths) -> dict:
    result = {}
    for label, path in (("tasks", paths.tasks_dir), ("logs", paths.logs_dir),
                        ("worktrees", paths.worktrees_dir), ("reviews", paths.reviews_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".pr_agent_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            result[label] = True
        except OSError as exc:
            result[label] = f"不可写：{exc}"
    return result


# ---------------------------------------------------------------- pick / run / cancel
@app.command()
def pick(
    repo: str = typer.Argument(..., help="OWNER/REPO"),
    issue: int = typer.Argument(..., help="Issue 编号"),
    mode: str = typer.Option(None, "--mode", help="assisted | full_autonomy（覆盖配置）"),
    agent: str = typer.Option(None, "--agent", help="指定 agent（默认按 fallback_chain 分配）"),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="人工覆盖白名单门禁（落事件）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不创建 worktree/不改池状态"),
    no_run: bool = typer.Option(False, "--no-run", help="只打包，不执行"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """人工选题 → 打包（→ 立即执行，除非 --no-run/--dry-run）。"""
    try:
        paths, config, projects = _ctx(mode)
        runner = Runner(paths, config, projects)
        picked = runner.pick(repo, issue, mode=mode, agent_name=agent,
                            allow_unlisted=allow_unlisted, dry_run=dry_run)
        if picked.status == "dry_run":
            _echo(picked.as_dict(), as_json)
            raise typer.Exit(0)
        if picked.status != "packed" or picked.task is None:
            _echo(picked.as_dict(), as_json)
            raise typer.Exit(_exit_for(picked.status))
        if no_run:
            _echo({"status": "packed", "task": picked.task.to_dict()}, as_json)
            raise typer.Exit(0)
        result = runner.run_task(picked.task)
        _echo(result, as_json)
        raise typer.Exit(_exit_for(str(result.get("status"))))
    except PrAgentError as exc:
        _fail(exc, as_json)
        raise typer.Exit(1)


@app.command()
def run(
    task_id: str = typer.Argument(..., help="任务 ID（tasks/<task_id>）"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """执行已打包的任务（不重新打包、不重新分配）。"""
    try:
        paths, config, projects = _ctx()
        task_dir = paths.task_dir(task_id)
        if not (task_dir / "task.json").is_file():
            raise PrAgentError(f"任务不存在：{task_id}")
        task = load_task(task_dir)
        if task.status in {"canceled", "handoff_done"}:
            raise PrAgentError(f"任务状态为 {task.status}，不再执行")
        if task.status != "packed":
            raise PrAgentError(
                f"任务状态为 {task.status}，run 只执行 packed 任务",
                detail="失败任务请先用 pool.py refresh 重新验证，再用 `pr_agent pick` 重新打包。",
            )
        runner = Runner(paths, config, projects)
        result = runner.run_task(task, dry_run=dry_run)
        _echo(result, as_json)
        raise typer.Exit(_exit_for(str(result.get("status"))))
    except PrAgentError as exc:
        _fail(exc, as_json)
        raise typer.Exit(1)


@app.command()
def cancel(
    task_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="取消原因（必填，落事件）"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """取消任务（尽力终止子进程；池记录转 BLOCKED）。"""
    try:
        paths, _, _ = _ctx()
        task = load_task(paths.task_dir(task_id))
        result = cancel_task(paths, Pool(paths), task, reason=reason)
        _echo(result, as_json)
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------- task 子命令
@task_app.command("handoff-done")
def task_handoff_done(
    task_id: str = typer.Argument(...),
    comment_url: str = typer.Option(..., "--comment-url"),
    pr_url: str = typer.Option(..., "--pr-url"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """assisted：人工在 GitHub 执行后回填 URL，池记录推进到 PR_OPEN → PENDING_HUMAN。"""
    try:
        paths, _, _ = _ctx()
        task_dir = paths.task_dir(task_id)
        task = load_task(task_dir)
        if task.mode != "assisted":
            raise PrAgentError(f"任务模式为 {task.mode}，handoff-done 只适用于 assisted")
        result = handoff.finish(paths=paths, pool=Pool(paths), task=task, task_dir=task_dir,
                                comment_url=comment_url, pr_url=pr_url)
        _echo(result, as_json)
    except PrAgentError as exc:
        _fail(exc, as_json)
        raise typer.Exit(1)


@task_app.command("cancel")
def task_cancel(
    task_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """assisted：人工放弃交付（handoff canceled → 池记录 BLOCKED → STALE）。"""
    try:
        paths, _, _ = _ctx()
        task_dir = paths.task_dir(task_id)
        task = load_task(task_dir)
        result = handoff.cancel(paths=paths, pool=Pool(paths), task=task, task_dir=task_dir, reason=reason)
        _echo(result, as_json)
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


@task_app.command("show")
def task_show(
    task_id: str = typer.Argument(...),
    events: int = typer.Option(20, "--events", help="显示最近 N 条事件"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """查看任务记录、handoff 校验与最近事件。"""
    try:
        paths, _, _ = _ctx()
        task_dir = paths.task_dir(task_id)
        task = load_task(task_dir)
        validation = handoff.validate(task_dir, repo=task.repo, issue_number=task.issue_number)
        stream = read_jsonl(task_dir / "events.jsonl")
        payload = {"task": task.to_dict(), "handoff": validation, "events": stream[-events:]}
        if as_json:
            _echo(payload, True)
            return
        typer.echo(f"任务：{task.task_id}")
        typer.echo(f"  状态：{task.status}；handoff：{task.handoff_state or '-'}；模式：{task.mode}")
        typer.echo(f"  agent：{task.agent}（{task.model or '默认模型'}）；exit={task.exit_code}")
        typer.echo(f"  worktree：{task.worktree}")
        typer.echo(f"  分支：{task.branch}；base={task.base_sha}")
        typer.echo(f"  dossier：{task.dossier or '（未生成）'}")
        typer.echo(f"  handoff 校验：{'PASS' if validation['ok'] else 'BLOCKED'}")
        for item in validation["problems"]:
            typer.echo(f"    - 问题：{item}")
        for item in validation["warnings"]:
            typer.echo(f"    - 提醒：{item}")
        typer.echo("  最近事件：")
        for event in stream[-events:]:
            typer.echo(f"    {event.get('ts', '')} {event.get('type', '')}")
    except (PrAgentError, FileNotFoundError) as exc:
        typer.secho(f"[中止] {exc}", fg="red", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------- job / daemon / schedule
@job_app.command("pool-maint")
def job_pool_maint(
    dry_run: bool = typer.Option(False, "--dry-run"),
    limit: int = typer.Option(8, "--limit", help="本次最多刷新多少候选"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """池子维护：刷新 DISCOVERED/STALE/UNKNOWN，凑够 6 个新鲜 VERIFIED。"""
    try:
        paths, config, projects = _ctx()
        result = pool_maint(paths, config, projects, dry_run=dry_run, limit=limit)
        _echo(result.as_dict(), as_json)
        raise typer.Exit(_exit_for(result.status))
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


@job_app.command("run-day")
def job_run_day(
    dry_run: bool = typer.Option(False, "--dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """PR 生产：plan-week → 今日至多 1 个任务。"""
    try:
        paths, config, projects = _ctx()
        result = run_job("run-day", paths, config, projects, dry_run=dry_run)
        _echo(result.as_dict(), as_json)
        raise typer.Exit(_exit_for(result.status))
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


@job_app.command("monitor")
def job_monitor(
    dry_run: bool = typer.Option(False, "--dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """PR 监控：只读记录 CI/评论/状态，绝不合并或批准。"""
    try:
        paths, config, _ = _ctx()
        result = run_job("monitor", paths, config, load_projects_config(paths), dry_run=dry_run)
        _echo(result.as_dict(), as_json)
        raise typer.Exit(_exit_for(result.status))
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


@app.command()
def daemon(
    once: bool = typer.Option(False, "--once", help="立即执行一遍全部作业后退出"),
    max_hours: float = typer.Option(None, "--max-hours"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """内置守护：按时间表拉起 pool-maint/run-day/monitor。"""
    try:
        paths, config, projects = _ctx()
        code = run_daemon(paths, config, projects, once=once, max_hours=max_hours, dry_run=dry_run)
        raise typer.Exit(code)
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


@app.command("install-schedule")
def install_schedule(
    confirm: bool = typer.Option(False, "--confirm", help="确认写入 Windows 任务计划"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """注册三个 Windows 计划任务（默认只打印命令）。"""
    paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
    result = schedule.install(paths, confirm=confirm, dry_run=dry_run)
    _echo(result, as_json)


@app.command("uninstall-schedule")
def uninstall_schedule(
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """移除三个计划任务（默认只打印命令）。"""
    result = schedule.uninstall(confirm=confirm, dry_run=dry_run)
    _echo(result, as_json)


# ---------------------------------------------------------------- 治理 / 仓库
@whitelist_app.command("propose")
def whitelist_propose(
    repo: str = typer.Argument(...),
    reason: str = typer.Option("", "--reason"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """写入待批准提案（approved=false，人工批准后才参与分配）。"""
    try:
        paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
        entry = governance.propose(paths, repo, reason=reason)
        _echo({"repo": entry.repo, "approved": entry.approved, "added_by": entry.added_by}, as_json)
    except (PrAgentError, ValueError) as exc:
        typer.secho(f"[中止] {exc}", fg="red", err=True)
        raise typer.Exit(1)


@whitelist_app.command("approve")
def whitelist_approve(
    repo: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """人工批准提案（approved=true）。"""
    try:
        paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
        entry = governance.approve(paths, repo)
        _echo({"repo": entry.repo, "approved": entry.approved}, as_json)
    except (PrAgentError, ValueError) as exc:
        typer.secho(f"[中止] {exc}", fg="red", err=True)
        raise typer.Exit(1)


@whitelist_app.command("list")
def whitelist_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """列出白/黑名单与待批准提案。"""
    try:
        paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
        projects = load_projects_config(paths)
        payload = {
            "whitelist": [entry.model_dump() for entry in projects.whitelist],
            "blacklist": [entry.model_dump() for entry in projects.blacklist],
            "pending": [entry.repo for entry in projects.pending_proposals()],
        }
        _echo(payload, as_json)
    except PrAgentError as exc:
        typer.secho(f"[中止] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)


@repo_app.command("add")
def repo_add(
    repo: str = typer.Argument(..., help="OWNER/REPO"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """用 gh 克隆仓库到 open_coding/repos/<name>（只读准备，不改远端）。"""
    paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
    destination = paths.repo_dir(repo)
    if destination.exists():
        _echo({"status": "exists", "path": str(destination)}, as_json)
        return
    command = ["gh", "repo", "clone", repo, str(destination)]
    if dry_run:
        _echo({"status": "dry_run", "command": command}, as_json)
        return
    if not ghcli.available():
        typer.secho("[中止] 找不到 gh CLI", fg="red", err=True)
        raise typer.Exit(1)
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if completed.returncode != 0:
        typer.secho(f"[中止] gh repo clone 失败：{completed.stderr[-1000:]}", fg="red", err=True)
        raise typer.Exit(1)
    _echo({"status": "cloned", "path": str(destination)}, as_json)


@app.command()
def chat(
    message: str | None = typer.Argument(None, help="一次性提问；省略则进入交互模式"),
) -> None:
    """可选的前端 LLM 对话（读配置 frontend 段）。"""
    from .frontend import chat_loop

    try:
        paths, config, _ = _ctx()
    except PrAgentError as exc:
        typer.secho(f"[配置错误] {exc.message}", fg="red", err=True)
        raise typer.Exit(1)
    raise typer.Exit(chat_loop(paths, config, message))


@app.command()
def assets(as_json: bool = typer.Option(False, "--json")) -> None:
    """列出 open_coding 复用资产路径与存在性。"""
    from .paths import REQUIRED_ASSETS

    paths = load_paths(GLOBAL["workspace"], GLOBAL["open_coding"])
    payload = [
        {"key": key, "path": str(paths.open_coding / relative),
         "exists": (paths.open_coding / relative).is_file()}
        for key, relative in REQUIRED_ASSETS.items()
    ]
    _echo(payload, as_json)


if __name__ == "__main__":  # pragma: no cover
    app()
