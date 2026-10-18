"""三个幂等定时作业（DEVELOPMENT_PLAN §7）。

| 作业 | 频率 | 语义 |
|---|---|---|
| pool-maint | 每 6 小时 | 刷新 DISCOVERED/STALE/UNKNOWN，凑够 6 个新鲜 VERIFIED（≥3 仓、单仓 ≤2）；不足如实报告，不降门禁 |
| run-day    | 周一至五 09:05 | plan-week --target 3 --reserve 6 → 今日至多 1 个任务；计划不完整则跳过 |
| monitor    | 每 6 小时 | 记录 CI/评论/竞态 PR 为事件；绝不合并/批准/回复 |

所有作业可 `--dry-run`：只打印将做之事，不产生外部动作。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ghcli, governance
from .config import AgentsConfig, ProjectsConfig
from .errors import BlockedError, PoolError, PrAgentError
from .events import SHANGHAI, now_iso, pool_summary_event, today_local, week_key
from .models import list_tasks
from .paths import Paths
from .poolctl import Pool
from .quota import load_ledger
from .runner import Runner

REFRESH_STATUSES = {"DISCOVERED", "STALE", "UNKNOWN"}
MONITOR_STATUSES = {"PR_OPEN", "PENDING_HUMAN", "CHANGES_REQUESTED"}
ACTIVE_TASK_STATUSES = {"running", "ready", "handoff_pending", "handoff_done"}
PR_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")


@dataclass
class JobResult:
    job: str
    status: str  # ok | skipped | partial | blocked | dry_run
    message: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"job": self.job, "status": self.status, "message": self.message,
                "evidence": self.evidence, "at": now_iso()}


# ---------------------------------------------------------------- 断路器
class CircuitBreaker:
    """两候选连续验证/执行失败 → 当日停产（继承 operator-guide）。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"consecutive_failures": 0, "opened_on": None, "history": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"consecutive_failures": 0, "opened_on": None, "history": [],
                    "warning": "breaker.json 损坏，按未熔断处理并保留原文件"}

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_open(self) -> bool:
        state = self._load()
        return state.get("opened_on") == today_local()

    def state(self) -> dict:
        return self._load()

    def record_failure(self, kind: str, reason: str, *, threshold: int = 2) -> dict:
        state = self._load()
        count = int(state.get("consecutive_failures", 0)) + 1
        history = list(state.get("history", []))[-20:]
        history.append({"at": now_iso(), "kind": kind, "reason": reason})
        opened = today_local() if count >= threshold else state.get("opened_on")
        state.update({"consecutive_failures": count, "opened_on": opened, "history": history})
        self._save(state)
        return state

    def reset(self) -> None:
        state = self._load()
        state.update({"consecutive_failures": 0, "opened_on": None})
        self._save(state)


# ---------------------------------------------------------------- pool-maint
def pool_maint(
    paths: Paths,
    config: AgentsConfig,
    projects: ProjectsConfig,
    *,
    dry_run: bool = False,
    limit: int = 8,
    pool: Pool | None = None,
    breaker: CircuitBreaker | None = None,
) -> JobResult:
    pool = pool or Pool(paths)
    breaker = breaker or CircuitBreaker(paths.breaker_state)
    paths.require_assets()
    records = [record for record in pool.show() if record.get("status") in REFRESH_STATUSES]
    records.sort(key=lambda item: (str(item.get("priority", "P9")), str(item.get("updated_at", ""))))
    planned = records[:limit]

    if dry_run:
        return JobResult("pool-maint", "dry_run", f"将刷新 {len(planned)} 个候选（共 {len(records)} 个待刷新）", {
            "commands": [
                f"python scripts/pool.py refresh --repo {r.get('repo')} --issue {r.get('issue_number')}"
                for r in planned if r.get("issue_number")
            ],
            "skipped_no_issue_number": [r.get("id") for r in planned if not r.get("issue_number")],
            "breaker": breaker.state(),
        })

    outcomes = []
    for record in planned:
        repo = str(record.get("repo") or "")
        number = record.get("issue_number")
        if not repo or not isinstance(number, int):
            outcomes.append({"id": record.get("id"), "result": "skipped",
                             "reason": "缺少可审计的 Issue 编号（DOGFOOD 观察需先建立公开 Issue）"})
            continue
        local_repo = paths.repo_dir(repo)
        try:
            updated = pool.refresh(repo, number, local_repo if local_repo.is_dir() else None)
            outcomes.append({"id": record.get("id"), "result": updated.get("status"),
                             "vetoes": updated.get("vetoes", [])})
            if updated.get("status") in {"UNKNOWN", "REJECTED"}:
                breaker.record_failure("verify", f"{record.get('id')} → {updated.get('status')}")
            else:
                breaker.reset()
        except PrAgentError as exc:
            outcomes.append({"id": record.get("id"), "result": "error", "error": str(exc)})
            breaker.record_failure("verify", f"{record.get('id')}: {exc}")

    health = pool.health()
    rc, plan, raw = pool.plan_week(target=3, reserve=6)
    fresh = int(plan.get("eligible_count", 0)) if plan else 0
    warnings = list(plan.get("warnings", [])) if plan else ["plan-week 输出不可解析"]
    discoveries = governance.collect_discoveries(paths.tasks_dir)
    status = "ok" if fresh >= 6 and not warnings else "partial"
    message = (
        f"刷新 {len(planned)} 个候选；新鲜 VERIFIED={fresh}/6；"
        + ("；".join(warnings) if warnings else "池子满足 6 个新鲜候选与多样性要求")
    )
    return JobResult("pool-maint", status, message, {
        "outcomes": outcomes,
        "health": health,
        "plan_warnings": warnings,
        "plan_exit": rc,
        "plan_stdout_tail": raw.stdout[-2000:],
        "discoveries": discoveries[:20],
        "breaker": breaker.state(),
    })


# ---------------------------------------------------------------- run-day
def run_day(
    paths: Paths,
    config: AgentsConfig,
    projects: ProjectsConfig,
    *,
    dry_run: bool = False,
    pool: Pool | None = None,
    breaker: CircuitBreaker | None = None,
    runner: Runner | None = None,
) -> JobResult:
    pool = pool or Pool(paths)
    breaker = breaker or CircuitBreaker(paths.breaker_state)
    paths.require_assets()

    if breaker.is_open():
        return JobResult("run-day", "skipped", "断路器已熔断（当日连续失败 ≥2），今日停产", {
            "breaker": breaker.state(),
        })

    tasks = list_tasks(paths.tasks_dir)
    week = week_key()
    this_week = [
        task for task in tasks
        if week_key(datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))) == week
        and task.status in ACTIVE_TASK_STATUSES
    ]
    if len(this_week) >= config.weekly_pr_cap:
        return JobResult("run-day", "skipped",
                         f"本周已启动 {len(this_week)} 个任务，达到周上限 {config.weekly_pr_cap}",
                         {"week": week, "tasks": [task.task_id for task in this_week]})
    active = [task for task in tasks if task.status in {"packed", "running"}]
    if len(active) >= config.max_parallel:
        return JobResult("run-day", "skipped",
                         f"并发已满（{len(active)}/{config.max_parallel}），今日不新增任务",
                         {"active": [task.task_id for task in active]})

    # §7.1：开始先刷新再决策——只刷新"接近过期"的 VERIFIED（有界、失败即熔断），
    # 避免拿半天前的审计结果做今天的生产决策。
    records_all = pool.show()
    near_expiry = _near_expiry(records_all, _freshness_hours(paths), limit=3)
    if dry_run and near_expiry:
        return JobResult("run-day", "dry_run",
                         f"将先刷新 {len(near_expiry)} 个临近过期候选，再跑 plan-week",
                         {"refreshes": [f"python scripts/pool.py refresh --repo {r.get('repo')} "
                                        f"--issue {r.get('issue_number')}" for r in near_expiry]})
    refresh_failures: list[dict] = []
    refreshed: list[dict] = []
    for record in near_expiry:
        local_repo = paths.repo_dir(str(record.get("repo") or ""))
        try:
            updated = pool.refresh(str(record["repo"]), int(record["issue_number"]),
                                   local_repo if local_repo.is_dir() else None)
            refreshed.append({"id": record.get("id"), "status": updated.get("status")})
        except PrAgentError as exc:
            refresh_failures.append({"id": record.get("id"), "error": str(exc)})
            breaker.record_failure("refresh", f"{record.get('id')}: {exc}")
    if len(refresh_failures) >= 2 or breaker.is_open():
        return JobResult("run-day", "skipped",
                         "开工前刷新失败（≥2 或断路器已开），按契约今日停产",
                         {"refreshed": refreshed, "failures": refresh_failures,
                          "breaker": breaker.state()})

    rc, plan, raw = pool.plan_week(target=3, reserve=6)
    selected = list(plan.get("selected", [])) if plan else []
    incomplete = plan is None or len(selected) < int(plan.get("target", 3)) or bool(plan.get("warnings"))
    if incomplete:
        return JobResult("run-day", "skipped",
                         "plan-week 不完整，按契约跳过今日生产（不降门禁凑数）",
                         {"exit": rc, "warnings": (plan or {}).get("warnings", []),
                          "stdout_tail": getattr(raw, "stdout", "")[-2000:],
                          "stderr_tail": getattr(raw, "stderr", "")[-2000:],
                          "prerefresh": refreshed})

    candidate = None
    gate_rejections = []
    active_issues = {task.issue_id for task in tasks if task.status in {"packed", "running", "handoff_pending"}}
    limit = projects.principles.max_recent_competing_prs
    for record in selected:
        if str(record.get("id")) in active_issues:
            gate_rejections.append({"id": record.get("id"), "reason": "已有在途任务"})
            continue
        gate = governance.check_repo(projects, record)
        if not gate.allowed:
            gate_rejections.append({"id": record.get("id"), "reason": gate.reason})
            continue
        competing = record.get("collision_prs")
        if isinstance(competing, list) and limit is not None and len(competing) > limit:
            gate_rejections.append({"id": record.get("id"),
                                    "reason": f"竞态 PR {len(competing)} 个 > 上限 {limit}（硬门禁）"})
            continue
        # 用户裁决（2026-10-18）：不做加权评分；按 plan-week 顺序取第一个合规候选，
        # 选题优先级由人工/前端 agent 经 `pr_agent pick` 显式指定。
        candidate = record
        break
    if candidate is None:
        return JobResult("run-day", "skipped", "计划内候选均被治理门禁或在途任务排除",
                         {"rejections": gate_rejections})

    repo = str(candidate["repo"])
    issue_number = int(candidate["issue_number"])
    if dry_run:
        return JobResult("run-day", "dry_run", f"将执行 {repo}#{issue_number}（不产生外部动作）", {
            "candidate": {"repo": repo, "issue_number": issue_number},
            "mode": config.mode,
            "rejections": gate_rejections,
            "plan_warnings": plan.get("warnings", []),
        })

    runner = runner or Runner(paths, config, projects, pool=pool, usage=load_ledger(paths))
    try:
        picked = runner.pick(repo, issue_number, notes=["run-day 自动派发"])
    except PrAgentError as exc:
        breaker.record_failure("run-day", f"pick 失败：{exc}")
        return JobResult("run-day", "blocked", f"打包失败：{exc}", {"error": str(exc)})
    if picked.status != "packed" or picked.task is None:
        return JobResult("run-day", "skipped", picked.message, picked.evidence or {})

    result = runner.run_task(picked.task)
    if result.get("status") == "blocked":
        breaker.record_failure("run-day", f"{picked.task.task_id}: {result.get('reason')}")
    elif result.get("status") in {"ready", "handoff_pending"}:
        breaker.reset()
    return JobResult(
        "run-day",
        "ok" if result.get("status") in {"ready", "handoff_pending"} else str(result.get("status")),
        f"{picked.task.task_id} → {result.get('status')}",
        {"task": picked.task.task_id, "result": result, "breaker": breaker.state()},
    )


# ---------------------------------------------------------------- monitor
def monitor(
    paths: Paths,
    config: AgentsConfig,
    *,
    dry_run: bool = False,
    pool: Pool | None = None,
    projects: ProjectsConfig | None = None,
) -> JobResult:
    pool = pool or Pool(paths)
    paths.require_assets()
    records = [record for record in pool.show() if record.get("status") in MONITOR_STATUSES]
    max_competing = projects.principles.max_recent_competing_prs if projects else None
    targets = []
    for record in records:
        match = PR_URL.match(str(record.get("pr_url") or ""))
        if not match:
            targets.append({"id": record.get("id"), "state": "skipped", "reason": "缺少合法 PR URL"})
            continue
        targets.append({"id": record.get("id"), "repo": match.group(1), "number": int(match.group(2)),
                        "status": record.get("status")})

    if dry_run:
        return JobResult("monitor", "dry_run", f"将只读检查 {len(records)} 个 PR", {
            "commands": [
                f"gh pr view {item['number']} --repo {item['repo']} --json state,mergeable,reviewDecision,statusCheckRollup,comments"
                for item in targets if item.get("repo")
            ] + [
                f"gh pr list --repo {repo} --state open --json number,title,url,body --limit 100（同一仓库只读一次，用于竞态 PR）"
                for repo in sorted({item["repo"] for item in targets if item.get("repo")})
            ],
            "skipped": [item for item in targets if item.get("state") == "skipped"],
            "max_recent_competing_prs": max_competing,
        })

    observations = []
    cache: dict = {}
    for item in targets:
        if item.get("state") == "skipped":
            observations.append(item)
            continue
        try:
            view = ghcli.pr_view(item["repo"], item["number"])
        except (PrAgentError, BlockedError) as exc:
            observations.append({**item, "state": "UNKNOWN", "error": str(exc)[:800]})
            continue
        checks = view.get("statusCheckRollup") or []
        failing = [c.get("name") or c.get("context") for c in checks
                   if str(c.get("conclusion") or c.get("state") or "").upper() in {"FAILURE", "ERROR", "TIMED_OUT"}]

        # 竞态 PR（§7）：查询失败记 UNKNOWN，绝不用空列表冒充"没有竞态"
        issue_number = _issue_number(str(item.get("id")))
        competing: list[dict] | None | str
        if issue_number is None:
            competing = "UNKNOWN"
        else:
            competing = ghcli.competing_prs(item["repo"], issue_number,
                                            own_numbers={int(item["number"])}, cache=cache)
            if competing is None:
                competing = "UNKNOWN"
        exceeded = None
        if isinstance(competing, list) and max_competing is not None:
            exceeded = len(competing) > max_competing

        observation = {
            **item,
            "state": view.get("state"),
            "mergeable": view.get("mergeable"),
            "review_decision": view.get("reviewDecision"),
            "ci_total": len(checks),
            "ci_failing": failing,
            "updated_at": view.get("updatedAt"),
            "comments": len(view.get("comments") or []),
            "competing_prs": competing,
            "competition_exceeded": exceeded,
        }
        observations.append(observation)
        pool_summary_event(
            paths, issue_id=str(item["id"]), kind="PR_MONITOR",
            evidence={key: observation[key] for key in
                      ("state", "mergeable", "review_decision", "ci_total", "ci_failing",
                       "comments", "competing_prs", "competition_exceeded")},
        )
        try:
            if view.get("state") == "MERGED":
                pool.transition(str(item["id"]), "MERGED", actor="pr-agent",
                                evidence={"pr": view.get("url"), "observed_at": now_iso()})
            elif view.get("state") == "CLOSED":
                pool.transition(str(item["id"]), "CLOSED", actor="pr-agent",
                                evidence={"pr": view.get("url"), "observed_at": now_iso()})
        except PoolError as exc:
            observation["transition_error"] = str(exc)

    return JobResult("monitor", "ok", f"只读检查 {len(records)} 个 PR；未做任何写操作",
                     {"observations": observations})


def run_job(name: str, paths: Paths, config: AgentsConfig, projects: ProjectsConfig,
            *, dry_run: bool = False) -> JobResult:
    if name == "pool-maint":
        return pool_maint(paths, config, projects, dry_run=dry_run)
    if name == "run-day":
        return run_day(paths, config, projects, dry_run=dry_run)
    if name == "monitor":
        return monitor(paths, config, dry_run=dry_run, projects=projects)
    raise ValueError(f"未知作业：{name}")


def _issue_number(issue_id: str) -> int | None:
    tail = issue_id.rsplit("#", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _freshness_hours(paths: Paths) -> int:
    try:
        workflow = json.loads((paths.open_coding / "workflow" / "config.yaml").read_text(encoding="utf-8"))
        return int(workflow.get("freshness_hours", 12))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 12


def _near_expiry(records: list[dict], freshness_hours: int, *, limit: int = 3) -> list[dict]:
    """挑出即将过期的 VERIFIED 记录（已用掉 60% 以上新鲜期），按剩余时间升序。"""
    now = datetime.now(timezone.utc)
    threshold = freshness_hours * 0.6
    candidates = []
    for record in records:
        if record.get("status") != "VERIFIED" or not record.get("issue_number"):
            continue
        stamp = record.get("last_verified_at") or record.get("updated_at")
        if not isinstance(stamp, str):
            continue
        try:
            verified_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=timezone.utc)
        age_hours = (now - verified_at).total_seconds() / 3600
        if age_hours >= threshold:
            candidates.append((age_hours, record))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _, record in candidates[:limit]]
