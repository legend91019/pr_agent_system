"""任务打包与执行（DEVELOPMENT_PLAN §9.2 / §6 / §8）。

一次 run 的完整链路：
打包（worktree + prompt + task.json）→ 池状态 RESERVED/CODING → 分配 → spawn →
事件流 → 真实 diff / dossier / handoff 校验 → 池状态 REVIEW/PR_READY（/PR_OPEN/PENDING_HUMAN）
→ 额度记账。任何证据缺失都在此停下并 BLOCKED。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import dossier as dossier_mod
from . import cross_review, gitsvc, ghcli, governance, handoff
from .allocate import Decision, allocate, plan_only_reason
from .adapters import build_adapter
from .config import AgentsConfig, ProjectsConfig
from .errors import BlockedError, LockBusyError, PoolError, PrAgentError
from .events import TaskEvents, now_iso, pool_summary_event
from .locks import acquire_slot, adapter_lock, worktree_lock
from .models import Task, make_task_id
from .paths import Paths
from .poolctl import Pool
from .prompt import assemble_prompt, load_canonical, sha256_text, task_context
from .quota import UsageLedger, load_ledger


@dataclass
class PickResult:
    status: str  # dry_run | packed | queued | blocked
    message: str
    task: Task | None = None
    evidence: dict | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "task": self.task.to_dict() if self.task else None,
            "evidence": self.evidence or {},
        }


class Runner:
    def __init__(
        self,
        paths: Paths,
        config: AgentsConfig,
        projects: ProjectsConfig,
        *,
        pool: Pool | None = None,
        usage: UsageLedger | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.projects = projects
        self.pool = pool or Pool(paths)
        self.usage = usage or load_ledger(paths)
        self._freshness: int | None = None

    # ---------------------------------------------------------------- preflight
    def preflight(self, mode: str) -> list[str]:
        self.paths.require_assets()
        notes: list[str] = []
        if mode == "full_autonomy":
            ok, detail = ghcli.auth_status()
            if not ok:
                raise BlockedError(
                    "full_autonomy 前置条件不满足：gh 未登录",
                    detail=f"{detail}\n请按 open_coding/GITHUB_TOKEN_SETUP.md 由人工安装 token。",
                    evidence={"gh_auth": detail},
                )
            notes.append("gh auth status: 已登录")
        else:
            if ghcli.available():
                notes.append("gh 可用（assisted 仅用于只读查询）")
            else:
                notes.append("未检测到 gh；assisted 下的只读查询将失败并记录为 UNKNOWN")
        bound = self.paths.open_coding / "workflow" / "config.yaml"
        try:
            workflow = json.loads(bound.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            workflow = {}
        if workflow.get("execution_mode") == "manual_only":
            notes.append("open_coding execution_mode=manual_only：定时作业启用前须人工确认放开")
        return notes

    # ---------------------------------------------------------------- 打包
    def pick(
        self,
        repo: str,
        issue_number: int,
        *,
        mode: str | None = None,
        agent_name: str | None = None,
        allow_unlisted: bool = False,
        dry_run: bool = False,
        notes: list[str] | None = None,
    ) -> PickResult:
        effective_mode = mode or self.config.mode
        preflight_notes = self.preflight(effective_mode)
        issue_id = f"{repo}#{issue_number}"
        record = self.pool.record_for(issue_id)
        if record is None:
            raise BlockedError(f"池中没有该候选：{issue_id}", detail="请先运行 pool-maint 或刷新候选。")
        status = str(record.get("status"))
        if status != "VERIFIED":
            raise BlockedError(
                f"候选状态不是 VERIFIED，拒绝开工：{issue_id}（当前 {status}）",
                detail="只有新鲜且 scope_gate=PASS 的 VERIFIED 记录可以开工（DEVELOPMENT_PLAN §3）。",
                evidence={"issue_id": issue_id, "status": status},
            )
        self._check_scope_and_freshness(record)

        gate = governance.check_repo(self.projects, record, allow_unlisted=allow_unlisted)
        if not gate.allowed:
            raise BlockedError(
                f"仓库治理门禁拒绝：{gate.reason}",
                detail="可用 `pr_agent whitelist propose/approve` 或人工修改 config/projects.yaml。",
                evidence=gate.as_event(),
            )

        scope = record.get("scope_metrics") if isinstance(record.get("scope_metrics"), dict) else {}
        scope_lines = scope.get("expected_changed_lines") if isinstance(scope.get("expected_changed_lines"), (int, float)) else None

        if agent_name:
            decision = self._forced_decision(agent_name, scope_lines)
        else:
            decision = allocate(self.config, self.usage, scope_lines=scope_lines)
        if decision is None:
            return PickResult(
                status="queued",
                message=plan_only_reason(None),
                evidence={"issue_id": issue_id},
            )

        task_id = make_task_id(repo, issue_number)
        task_dir = self.paths.task_dir(task_id)
        repo_dir = self.paths.repo_dir(repo)
        task = self._build_plan(
            record=record, task_id=task_id, mode=effective_mode, decision=decision,
            repo_dir=repo_dir, task_dir=task_dir,
        )
        plan_evidence = {
            "task": task.to_dict(),
            "decision": decision.as_event(),
            "gate": gate.as_event(),
            "preflight": preflight_notes,
        }
        if dry_run:
            return PickResult("dry_run", "dry-run：未创建 worktree、未写任务目录、未改池状态", task, plan_evidence)

        self.paths.ensure_local_dirs()
        task_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = task_dir / "prompt.md"
        prompt_file.write_text(task.extra.pop("prompt_text"), encoding="utf-8")
        task.save(self.paths.tasks_dir)
        for sub in ("handoff", "verification", "discoveries"):
            (task_dir / sub).mkdir(exist_ok=True)
        Path(task.extra["verification_dir"]).mkdir(parents=True, exist_ok=True)

        with worktree_lock(self.paths, task_id):
            gitsvc.ensure_repo(repo_dir, repo)
            gitsvc.create_worktree(repo_dir, Path(task.worktree), task.branch, task.base_sha)

        self._transition(task, "RESERVED", {
            "mode": task.mode,
            "reservation": "deferred_to_handoff" if task.mode == "assisted" else "agent_will_comment",
            "task_id": task.task_id,
            "agent": task.agent,
        })
        self._transition(task, "CODING", {
            "base_sha": task.base_sha, "branch": task.branch, "worktree": task.worktree,
            "task_id": task.task_id, "mode": task.mode,
        })
        pool_summary_event(
            self.paths, issue_id=task.issue_id, kind="TASK_PACKED", task_id=task.task_id,
            evidence={"agent": task.agent, "model": task.model, "mode": task.mode,
                      "worktree": task.worktree, "base_sha": task.base_sha},
        )
        return PickResult("packed", f"任务已打包并置为 CODING：{task.task_id}", task, plan_evidence)

    def _check_scope_and_freshness(self, record: dict) -> None:
        """开工前置：scope_gate 必须 PASS，且记录未过期（默认 12 小时）。"""
        issue_id = str(record.get("id"))
        gate = record.get("scope_gate") if isinstance(record.get("scope_gate"), dict) else {}
        result = gate.get("result")
        if result != "PASS":
            raise BlockedError(
                f"scope_gate 未通过，拒绝开工：{issue_id}（result={result or 'MISSING'}）",
                detail=(
                    "只有 scope_gate.result=PASS 的 VERIFIED 记录可开工；"
                    "缺少 scope_metrics 时需先用 pool.py annotate 补齐并重新审计。"
                ),
                evidence={"issue_id": issue_id, "scope_gate": gate},
            )
        freshness = self._freshness_hours()
        stamp = record.get("last_verified_at") or record.get("updated_at")
        if not isinstance(stamp, str):
            raise BlockedError(f"候选缺少验证时间，无法判断新鲜度：{issue_id}")
        try:
            verified_at = _parse_time(stamp)
        except ValueError:
            raise BlockedError(f"候选验证时间不可解析：{issue_id}（{stamp}）") from None
        age_hours = (datetime.now(timezone.utc) - verified_at).total_seconds() / 3600
        if age_hours > freshness:
            raise BlockedError(
                f"候选已过期：{issue_id}（{age_hours:.1f}h > {freshness}h）",
                detail="请运行 `pr_agent job pool-maint` 重新审计后再开工；不得用过期记录凑数。",
                evidence={"issue_id": issue_id, "last_verified_at": stamp, "age_hours": round(age_hours, 2)},
            )

    def _freshness_hours(self) -> int:
        if self._freshness is None:
            try:
                workflow = json.loads(
                    (self.paths.open_coding / "workflow" / "config.yaml").read_text(encoding="utf-8")
                )
                self._freshness = int(workflow.get("freshness_hours", 12))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                self._freshness = 12
        return self._freshness

    def _forced_decision(self, agent_name: str, scope_lines: int | None) -> Decision:
        agent = self.config.agents.get(agent_name)
        if agent is None:
            raise BlockedError(f"未定义的 agent：{agent_name}")
        if not agent.enabled:
            raise BlockedError(f"agent 未启用：{agent_name}")
        ok, detail = build_adapter(agent).probe()
        if not ok:
            raise BlockedError(f"agent 不可用：{agent_name}", detail=detail)
        left = self.usage.quota_left(agent)
        if left == 0:
            raise BlockedError(f"agent 额度已用尽：{agent_name}")
        large = bool(scope_lines and scope_lines > 400 and "large" in agent.models)
        return Decision(
            agent=agent_name,
            model=agent.model_for(large=large),
            large_model=large,
            reason=f"人工指定 agent（额度剩余 {left if left is not None else '不限'}）",
        )

    def _build_plan(
        self,
        *,
        record: dict,
        task_id: str,
        mode: str,
        decision: Decision,
        repo_dir: Path,
        task_dir: Path,
    ) -> Task:
        gitsvc.ensure_repo(repo_dir, str(record.get("repo")))
        repo = str(record["repo"])
        issue_number = int(record["issue_number"])
        base_branch = gitsvc.default_branch(repo_dir)
        notes: list[str] = []
        recorded_base = str(record.get("base_sha") or "")
        if recorded_base and gitsvc.commit_exists(repo_dir, recorded_base):
            base_sha = recorded_base
            base_source = "pool_record"
        else:
            ref = f"origin/{base_branch}"
            if not gitsvc.commit_exists(repo_dir, ref):
                ref = base_branch
            base_sha = gitsvc.head_sha(repo_dir, ref)
            base_source = "local_ref"
            notes.append(
                f"池记录 base_sha={recorded_base or '缺失'} 在本地不可解析，改用 {ref} 当前 tip（已记录）"
            )
        slug = _slug(str(record.get("title") or ""), issue_number)
        branch = f"fix/issue-{issue_number}-{slug}"
        worktree = self.paths.worktrees_dir / task_id

        canonical = load_canonical(self.paths.canonical_prompt)
        verification_dir = self.paths.open_coding / "deliverables" / f"{repo.replace('/', '__')}__{issue_number}"
        dossier_path = self.paths.dossier_path(repo, issue_number)
        context = task_context(
            issue_id=f"{repo}#{issue_number}",
            issue_url=str(record.get("issue_url") or f"https://github.com/{repo}/issues/{issue_number}"),
            repo=repo,
            issue_number=issue_number,
            task_id=task_id,
            worktree=str(worktree),
            base_branch=base_branch,
            base_sha=base_sha,
            branch=branch,
            handoff_dir=str(task_dir / "handoff"),
            dossier_path=str(dossier_path),
            verification_dir=str(verification_dir),
            mode=mode,
            open_coding_root=str(self.paths.open_coding),
        )
        prompt_text = assemble_prompt(canonical, mode, context)
        task = Task(
            task_id=task_id,
            issue_id=f"{repo}#{issue_number}",
            repo=repo,
            issue_number=issue_number,
            mode=mode,
            agent=decision.agent,
            adapter=self.config.agent(decision.agent).adapter,
            model=decision.model,
            worktree=str(worktree),
            branch=branch,
            base_sha=base_sha,
            base_branch=base_branch,
            prompt_path=str(task_dir / "prompt.md"),
            prompt_sha256=sha256_text(prompt_text),
            notes=notes,
        )
        task.extra.update({
            "prompt_text": prompt_text,
            "prompt_source": canonical.source_kind,
            "canonical_prompt_path": canonical.path,
            "canonical_sha256": canonical.sha256,
            "verification_dir": str(verification_dir),
            "base_sha_source": base_source,
            "pool_record_snapshot": {
                "status": record.get("status"),
                "score": record.get("score"),
                "scope_metrics": record.get("scope_metrics"),
                "scope_gate": record.get("scope_gate"),
                "last_verified_at": record.get("last_verified_at"),
            },
            "allocation": decision.as_event(),
            "max_turns": self.config.agent(decision.agent).max_turns,
            "timeout_minutes": self.config.agent(decision.agent).timeout_minutes,
        })
        return task

    # ---------------------------------------------------------------- 执行
    def run_task(self, task: Task, *, dry_run: bool = False, timeout_seconds: float | None = None) -> dict:
        task_dir = self.paths.task_dir(task.task_id)
        if dry_run:
            return {
                "status": "dry_run",
                "task_id": task.task_id,
                "worktree": task.worktree,
                "branch": task.branch,
                "agent": task.agent,
                "model": task.model,
                "prompt_sha256": task.prompt_sha256,
                "mode": task.mode,
                "note": "dry-run：不 spawn、不写事件、不改池状态",
            }
        if not (task_dir / "prompt.md").is_file():
            raise BlockedError(f"任务 prompt 缺失：{task_dir / 'prompt.md'}")
        agent = self.config.agent(task.agent)
        adapter = build_adapter(agent)
        events = TaskEvents(task_dir, task.task_id)
        timeout = timeout_seconds or agent.timeout_minutes * 60

        try:
            slot = acquire_slot(self.paths, self.config.max_parallel, task.task_id)
        except LockBusyError as exc:
            task.note(f"并发已满，保持排队：{exc}")
            task.save(self.paths.tasks_dir)
            return {"status": "queued", "task_id": task.task_id, "message": str(exc)}

        exit_payload: dict = {}
        with slot:
            task.status = "running"
            task.started_at = now_iso()
            task.save(self.paths.tasks_dir)
            events.emit("task_start", {
                "agent": task.agent, "model": task.model, "mode": task.mode,
                "worktree": task.worktree, "timeout_seconds": timeout,
            })
            pool_summary_event(
                self.paths, issue_id=task.issue_id, kind="TASK_STARTED", task_id=task.task_id,
                evidence={"agent": task.agent, "model": task.model, "mode": task.mode},
            )
            try:
                with adapter_lock(self.paths, task.adapter):
                    for event in adapter.spawn(
                        prompt=(task_dir / "prompt.md").read_text(encoding="utf-8"),
                        prompt_path=task_dir / "prompt.md",
                        cwd=Path(task.worktree),
                        model=task.model,
                        full_autonomy=task.mode == "full_autonomy",
                        max_turns=int(task.extra.get("max_turns", 200)),
                        timeout_seconds=timeout,
                        task_id=task.task_id,
                    ):
                        events.emit(event.type, event.payload, adapter=event.adapter)
                        if event.type == "start" and event.payload.get("pid"):
                            task.extra["pid"] = event.payload["pid"]
                            task.save(self.paths.tasks_dir)
                        if event.type == "exit":
                            exit_payload = event.payload
            except PrAgentError as exc:
                return self._block(task, events, f"adapter 执行失败：{exc.message}",
                                   {"error": exc.message, "detail": exc.detail, **exc.evidence})
            except Exception as exc:  # 未预期异常也必须落盘为 BLOCKED
                return self._block(task, events, f"adapter 执行异常：{type(exc).__name__}: {exc}",
                                   {"error": str(exc), "type": type(exc).__name__})

        returncode = exit_payload.get("returncode")
        timed_out = bool(exit_payload.get("timed_out"))
        task.exit_code = returncode if isinstance(returncode, int) else None
        task.finished_at = now_iso()

        if timed_out or returncode != 0:
            self.usage.record(task.agent, task.task_id, outcome="failed")
            self.usage.mark_cooldown(task.agent, agent.quota.cooldown_minutes)
            return self._block(
                task, events,
                f"agent 执行失败（exit={returncode}, timed_out={timed_out}）",
                {"exit": exit_payload, "stderr_tail": exit_payload.get("stderr_tail", "")[-4000:]},
            )

        # ---- 证据采集 ----
        try:
            diff = gitsvc.collect_diff(Path(task.worktree), task.base_sha)
        except PrAgentError as exc:
            self.usage.record(task.agent, task.task_id, outcome="failed")
            return self._block(task, events, f"diff 采集失败：{exc.message}", {"error": str(exc)})
        patch_path = task_dir / "diff.patch"
        patch_path.write_text(diff["patch"] or "", encoding="utf-8")
        (task_dir / "diff-summary.json").write_text(
            json.dumps({key: value for key, value in diff.items() if key != "patch"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        events.emit("diff_collected", {
            "head_sha": diff["head_sha"],
            "committed_changed_lines": diff["committed_changed_lines"],
            "working_tree_dirty": diff["working_tree_dirty"],
            "untracked_files": diff["untracked_files"],
            "patch_path": str(patch_path),
        })

        # ---- dossier ----
        dossier_path = Path(task.dossier or self.paths.dossier_path(task.repo, task.issue_number))
        dossier_error = None
        existed_before = dossier_path.is_file()
        if not existed_before:
            record = self.pool.record_for(task.issue_id) or {
                "repo": task.repo, "issue_number": task.issue_number, "id": task.issue_id,
                "title": "", "issue_url": f"https://github.com/{task.repo}/issues/{task.issue_number}",
                "status": "CODING",
            }
            dossier_path, dossier_error = dossier_mod.ensure_skeleton(self.paths, self.pool, record)
        dossier_check = dossier_mod.validate(dossier_path)

        handoff_check = None
        if task.mode == "assisted":
            handoff_check = handoff.validate(task_dir, repo=task.repo, issue_number=task.issue_number)

        notes = list(task.notes)
        notes.append(f"prompt 来源：{task.extra.get('prompt_source')}；canonical sha256={task.extra.get('canonical_sha256', '')[:16]}")
        notes.append(f"diff 证据：{patch_path}")
        if handoff_check is not None:
            notes.append(f"handoff 校验：{'PASS' if handoff_check['ok'] else 'BLOCKED'}")
        task.notes = notes

        dossier_mod.append_orchestrator_section(
            dossier_path,
            task_id=task.task_id,
            mode=task.mode,
            agent=task.agent,
            model=task.model,
            branch=task.branch,
            base_sha=task.base_sha,
            head_sha=diff["head_sha"],
            diff=diff,
            exit_code=task.exit_code,
            timed_out=timed_out,
            handoff_check=handoff_check,
            allocation=task.extra.get("allocation"),
            notes=notes,
        )
        task.dossier = str(dossier_path)

        if not existed_before:
            self.usage.record(task.agent, task.task_id, outcome="failed")
            return self._block(
                task, events,
                "agent 未产出审查档案（dossier 缺失，仅生成骨架）",
                {"dossier": str(dossier_path), "render_error": dossier_error,
                 "dossier_check": dossier_check},
            )
        if dossier_check["problems"]:
            self.usage.record(task.agent, task.task_id, outcome="failed")
            return self._block(
                task, events, "审查档案不完整：" + "；".join(dossier_check["problems"]),
                {"dossier_check": dossier_check},
            )
        if handoff_check is not None and not handoff_check["ok"]:
            self.usage.record(task.agent, task.task_id, outcome="failed")
            return self._block(
                task, events, "handoff 交付包不完整：" + "；".join(handoff_check["problems"]),
                {"handoff_check": handoff_check},
            )

        # ---- 状态推进 ----
        self._transition(task, "REVIEW", {
            "head_sha": diff["head_sha"],
            "committed_changed_lines": diff["committed_changed_lines"],
            "dossier": str(dossier_path),
            "task_id": task.task_id,
        })

        cross_result = None
        if self.config.cross_review.enabled:
            task.extra["head_sha"] = diff["head_sha"]
            cross_result = cross_review.run(
                paths=self.paths,
                config=self.config,
                usage=self.usage,
                task=task,
                task_dir=task_dir,
                diff_text=diff["patch"],
                stats=(
                    f"{diff['committed_changed_lines']} 行 / "
                    f"{len(diff['committed_files'])} 文件（base..HEAD）"
                ),
            )
            task.notes.append(cross_review.summary_line(cross_result))
            events.emit("cross_review", cross_result.as_dict())
            # §12：第二意见必须同时并入 pr_reviews/<owner>__<repo>__<issue>.md
            dossier_mod.append_cross_review_section(dossier_path, cross_result.as_dict())
            if cross_result.blocking:
                self._transition(task, "CODING", {
                    "reason": "cross_review_blocking_findings",
                    "reviewer": cross_result.reviewer,
                    "blocking": cross_result.blocking,
                    "task_id": task.task_id,
                })
                self.usage.record(task.agent, task.task_id, outcome="failed")
                return self._block(
                    task, events,
                    f"交叉 review 发现 {len(cross_result.blocking)} 项 Blocker/Major，已打回 CODING",
                    {"cross_review": cross_result.as_dict()},
                )

        self._transition(task, "PR_READY", {
            "dossier": str(dossier_path),
            "dossier_warnings": dossier_check["warnings"],
            "handoff": handoff_check,
            "cross_review": cross_result.as_dict() if cross_result else None,
            "task_id": task.task_id,
            "mode": task.mode,
        })

        result: dict = {
            "status": "ready",
            "task_id": task.task_id,
            "mode": task.mode,
            "head_sha": diff["head_sha"],
            "dossier": str(dossier_path),
            "dossier_warnings": dossier_check["warnings"],
            "handoff": handoff_check,
            "cross_review": cross_result.as_dict() if cross_result else None,
            "diff": {key: value for key, value in diff.items() if key != "patch"},
        }

        if task.mode == "assisted":
            state = {
                "state": "pending_handoff",
                "validated_at": handoff_check["validated_at"],
                "problems": handoff_check["problems"],
                "warnings": handoff_check["warnings"],
                "files": handoff_check["files"],
                "dossier": str(dossier_path),
                "head_sha": diff["head_sha"],
            }
            handoff.write_state(task_dir, state)
            task.status = "handoff_pending"
            task.handoff_state = "pending_handoff"
            task.save(self.paths.tasks_dir)
            self.usage.record(task.agent, task.task_id, outcome="ok")
            self.usage.mark_cooldown(task.agent, agent.quota.cooldown_minutes)
            pool_summary_event(
                self.paths, issue_id=task.issue_id, kind="HANDOFF_READY", task_id=task.task_id,
                evidence={"dossier": str(dossier_path), "handoff_dir": str(task_dir / "handoff"),
                          "head_sha": diff["head_sha"]},
            )
            result["status"] = "handoff_pending"
            result["next"] = f"人工执行 handoff 后运行：pr_agent task handoff-done {task.task_id}"
            events.emit("task_result", result)
            return result

        # full_autonomy：验证 PR 真实存在（fail-closed）
        pr = ghcli.find_pr(task.repo, task.branch)
        if not pr:
            self.usage.record(task.agent, task.task_id, outcome="failed")
            return self._block(
                task, events,
                "full_autonomy 下未检测到 PR（agent 声称完成但 GitHub 无对应 PR）",
                {"branch": task.branch, "repo": task.repo, "head_sha": diff["head_sha"]},
            )
        self._transition(task, "PR_OPEN", {
            "pr_url": pr.get("url"), "pr_number": pr.get("number"),
            "head_sha": diff["head_sha"], "task_id": task.task_id,
        })
        self._transition(task, "PENDING_HUMAN", {
            "pr_url": pr.get("url"), "dossier": str(dossier_path), "task_id": task.task_id,
        })
        task.status = "ready"
        task.save(self.paths.tasks_dir)
        self.usage.record(task.agent, task.task_id, outcome="ok")
        self.usage.mark_cooldown(task.agent, agent.quota.cooldown_minutes)
        pool_summary_event(
            self.paths, issue_id=task.issue_id, kind="PR_OPENED", task_id=task.task_id,
            evidence={"pr_url": pr.get("url"), "dossier": str(dossier_path)},
        )
        result["pr"] = pr
        result["next"] = "人工终审：查看 dossier 与 PR，自行决定是否合并"
        events.emit("task_result", result)
        return result

    # ---------------------------------------------------------------- 内部
    def _transition(self, task: Task, to: str, evidence: dict) -> None:
        self.pool.transition(task.issue_id, to, actor="pr-agent", evidence=evidence)
        pool_summary_event(
            self.paths, issue_id=task.issue_id, kind=f"POOL_{to}", task_id=task.task_id,
            evidence=evidence, actor="pr-agent",
        )

    def _block(self, task: Task, events: TaskEvents, reason: str, evidence: dict) -> dict:
        task.status = "blocked"
        task.note(f"BLOCKED: {reason}")
        task.save(self.paths.tasks_dir)
        events.emit("task_blocked", {"reason": reason, "evidence": evidence})
        pool_error = None
        try:
            self.pool.transition(
                task.issue_id, "BLOCKED", actor="pr-agent",
                evidence={"reason": reason, "task_id": task.task_id, **evidence},
            )
            pool_summary_event(
                self.paths, issue_id=task.issue_id, kind="POOL_BLOCKED", task_id=task.task_id,
                evidence={"reason": reason},
            )
        except PoolError as exc:
            pool_error = str(exc)
            task.note(f"池状态 BLOCKED 迁移失败：{pool_error}")
            task.save(self.paths.tasks_dir)
        return {
            "status": "blocked",
            "task_id": task.task_id,
            "reason": reason,
            "evidence": evidence,
            "pool_error": pool_error,
        }


def _slug(title: str, issue_number: int) -> str:
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    slug = "-".join(words[:4])[:32].strip("-")
    return slug or f"issue-{issue_number}"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def cancel_task(paths: Paths, pool: Pool, task: Task, *, reason: str) -> dict:
    """人工取消：尽力终止子进程，池记录 CODING/REVIEW → BLOCKED。"""
    if not reason.strip():
        raise BlockedError("取消必须给出原因（--reason）")
    task_dir = paths.task_dir(task.task_id)
    events = TaskEvents(task_dir, task.task_id)
    pid = task.extra.get("pid")
    killed = False
    if isinstance(pid, int) and task.status == "running":
        target = str(pid)
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", target], capture_output=True, text=True,
            errors="replace",
        ) if shutil.which("taskkill") else None
        killed = bool(completed and completed.returncode == 0)
        events.emit("cancel_kill", {"pid": pid, "killed": killed,
                                    "output": (completed.stdout or completed.stderr) if completed else ""})
    task.status = "canceled"
    task.note(f"人工取消：{reason}")
    task.save(paths.tasks_dir)
    events.emit("task_canceled", {"reason": reason, "killed": killed})
    pool_error = None
    try:
        pool.transition(task.issue_id, "BLOCKED", actor="human",
                        evidence={"reason": f"canceled: {reason}", "task_id": task.task_id})
        pool_summary_event(paths, issue_id=task.issue_id, kind="POOL_BLOCKED",
                           task_id=task.task_id, evidence={"reason": f"canceled: {reason}"})
    except PoolError as exc:
        pool_error = str(exc)
    return {"status": "canceled", "task_id": task.task_id, "killed": killed, "pool_error": pool_error}
