"""三个作业的幂等/跳过/断路器/干跑语义。"""

from __future__ import annotations

import json

from pr_agent.events import now_iso, today_local
from pr_agent.jobs import CircuitBreaker, monitor, pool_maint, run_day
from pr_agent.models import Task

from tests.conftest import build_workspace


def _task(task_id: str, status: str = "running") -> Task:
    return Task(
        task_id=task_id, issue_id=f"Super/Repo#{task_id[-1]}", repo="Super/Repo",
        issue_number=int(task_id[-1]), mode="assisted", agent="claude-code",
        adapter="claude_code", model="m", worktree="w", branch="b", base_sha="s",
        base_branch="main", prompt_path="p", prompt_sha256="h", status=status,
    )


def test_pool_maint_dry_run(tmp_path):
    ws = build_workspace(tmp_path, status="DISCOVERED")
    result = pool_maint(ws.paths, ws.config, ws.projects, dry_run=True)
    assert result.status == "dry_run"
    assert any("pool.py refresh" in command for command in result.evidence["commands"])


def test_pool_maint_reports_shortfall_honestly(tmp_path):
    ws = build_workspace(tmp_path, status="DISCOVERED")
    result = pool_maint(ws.paths, ws.config, ws.projects, dry_run=False)
    assert result.status in {"partial", "ok"}
    assert "刷新 1 个候选" in result.message
    assert result.evidence["plan_warnings"]


def test_run_day_skips_when_plan_incomplete(tmp_path):
    ws = build_workspace(tmp_path)
    result = run_day(ws.paths, ws.config, ws.projects, dry_run=False)
    assert result.status == "skipped"
    assert "plan-week 不完整" in result.message


def test_run_day_honours_breaker(tmp_path):
    ws = build_workspace(tmp_path)
    ws.paths.breaker_state.write_text(
        json.dumps({"consecutive_failures": 2, "opened_on": today_local(), "history": []}),
        encoding="utf-8",
    )
    result = run_day(ws.paths, ws.config, ws.projects)
    assert result.status == "skipped" and "熔断" in result.message


def test_run_day_honours_weekly_cap(tmp_path):
    ws = build_workspace(tmp_path)
    for index in (1, 2, 3):
        _task(f"Super__Repo__{index}").save(ws.paths.tasks_dir)
    result = run_day(ws.paths, ws.config, ws.projects)
    assert result.status == "skipped" and "周上限" in result.message


def test_breaker_opens_after_two_failures(tmp_path):
    ws = build_workspace(tmp_path)
    breaker = CircuitBreaker(ws.paths.breaker_state)
    breaker.record_failure("verify", "candidate-1")
    assert breaker.is_open() is False
    breaker.record_failure("verify", "candidate-2")
    assert breaker.is_open() is True
    breaker.reset()
    assert breaker.is_open() is False


def test_monitor_dry_run_never_calls_gh(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path, status="PENDING_HUMAN")
    records = [
        {**_record(ws), "pr_url": "https://github.com/Super/Repo/pull/12"},
    ]

    class FakePool:
        def show(self, status=None):
            return records

        def transition(self, *args, **kwargs):  # pragma: no cover - dry-run 不应调用
            raise AssertionError("dry-run 不得改池状态")

    result = monitor(ws.paths, ws.config, dry_run=True, pool=FakePool())
    assert result.status == "dry_run"
    assert any("gh pr view 12" in command for command in result.evidence["commands"])


def test_monitor_records_unknown_without_pr_url(tmp_path):
    ws = build_workspace(tmp_path, status="PENDING_HUMAN")

    class FakePool:
        def show(self, status=None):
            return [_record(ws)]

        def transition(self, *args, **kwargs):
            raise AssertionError("缺少 PR URL 时不得改池状态")

    result = monitor(ws.paths, ws.config, dry_run=False, pool=FakePool())
    assert result.status == "ok"
    assert result.evidence["observations"][0]["reason"] == "缺少合法 PR URL"


def _record(ws) -> dict:
    return json.loads((ws.paths.pool_issues).read_text(encoding="utf-8").splitlines()[0])


def test_monitor_records_competing_prs(tmp_path, monkeypatch):
    """§7：monitor 必须把同 Issue 竞态 PR 记为事件，并按 principles 阈值标注超标。"""
    from pr_agent import jobs

    ws = build_workspace(tmp_path, status="PENDING_HUMAN")
    record = {**_record(ws), "pr_url": "https://github.com/Super/Repo/pull/12"}

    class FakePool:
        def show(self, status=None):
            return [record]

        def transition(self, *args, **kwargs):
            raise AssertionError("monitor 不得改池状态")

    monkeypatch.setattr(jobs.ghcli, "pr_view", lambda repo, number: {
        "number": number, "state": "OPEN", "mergeable": "MERGEABLE",
        "reviewDecision": "REVIEW_REQUIRED", "statusCheckRollup": [], "updatedAt": "2026-10-18T00:00:00Z",
        "comments": [],
    })
    monkeypatch.setattr(jobs.ghcli, "competing_prs", lambda *a, **kw: [
        {"number": 13, "title": "fix #7 other way", "url": "https://github.com/Super/Repo/pull/13"},
        {"number": 14, "title": "another fix", "url": "https://github.com/Super/Repo/pull/14"},
    ])
    ws.projects.principles.max_recent_competing_prs = 1
    result = monitor(ws.paths, ws.config, dry_run=False, pool=FakePool(), projects=ws.projects)
    observation = result.evidence["observations"][0]
    assert [item["number"] for item in observation["competing_prs"]] == [13, 14]
    assert observation["competition_exceeded"] is True
    events = ws.paths.pool_events.read_text(encoding="utf-8")
    assert "PR_MONITOR" in events and "competing_prs" in events


def test_monitor_marks_competing_query_unknown_on_failure(tmp_path, monkeypatch):
    """查询失败只能记 UNKNOWN，不能用空列表冒充"没有竞态"。"""
    from pr_agent import jobs

    ws = build_workspace(tmp_path, status="PENDING_HUMAN")
    record = {**_record(ws), "pr_url": "https://github.com/Super/Repo/pull/12"}

    class FakePool:
        def show(self, status=None):
            return [record]

        def transition(self, *args, **kwargs):
            raise AssertionError("monitor 不得改池状态")

    monkeypatch.setattr(jobs.ghcli, "pr_view", lambda repo, number: {
        "number": number, "state": "OPEN", "mergeable": "UNKNOWN",
        "reviewDecision": None, "statusCheckRollup": [], "updatedAt": None, "comments": [],
    })
    monkeypatch.setattr(jobs.ghcli, "competing_prs", lambda *a, **kw: None)
    result = monitor(ws.paths, ws.config, dry_run=False, pool=FakePool(), projects=ws.projects)
    observation = result.evidence["observations"][0]
    assert observation["competing_prs"] == "UNKNOWN"
    assert observation["competition_exceeded"] is None


def _age_record(ws, hours: float) -> dict:
    from datetime import datetime, timedelta, timezone

    record = _record(ws)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0)
    record["last_verified_at"] = stamp.isoformat().replace("+00:00", "Z")
    record["updated_at"] = record["last_verified_at"]
    return record


def test_run_day_refreshes_near_expiry_before_planning(tmp_path):
    """§7.1：开工前先刷新临近过期的候选，再跑 plan-week。"""
    ws = build_workspace(tmp_path)
    record = _age_record(ws, 11)  # freshness=12h，已用 11h
    calls: list[tuple] = []

    class FakePool:
        def show(self, status=None):
            return [record]

        def refresh(self, repo, issue, local_repo=None):
            calls.append((repo, issue))
            return {**record, "status": "VERIFIED"}

        def plan_week(self, target=3, reserve=6):
            return 2, {"selected": [], "target": 3, "warnings": ["weekly plan incomplete"]}, None

        def health(self):
            return {"total": 1, "by_status": {"VERIFIED": 1}}

        def transition(self, *args, **kwargs):
            raise AssertionError("不应迁移状态")

    result = run_day(ws.paths, ws.config, ws.projects, pool=FakePool())
    assert calls == [("Super/Repo", 7)]
    assert result.status == "skipped" and "plan-week 不完整" in result.message


def test_run_day_dry_run_lists_pending_refresh(tmp_path):
    ws = build_workspace(tmp_path)
    record = _age_record(ws, 11)

    class FakePool:
        def show(self, status=None):
            return [record]

        def refresh(self, *args, **kwargs):
            raise AssertionError("dry-run 不得刷新")

    result = run_day(ws.paths, ws.config, ws.projects, dry_run=True, pool=FakePool())
    assert result.status == "dry_run"
    assert any("pool.py refresh" in command for command in result.evidence["refreshes"])


def test_run_day_skips_when_prerefresh_fails(tmp_path):
    ws = build_workspace(tmp_path)
    record = _age_record(ws, 11)

    class FakePool:
        def show(self, status=None):
            return [record, {**record, "id": "Super/Repo#8", "issue_number": 8}]

        def refresh(self, *args, **kwargs):
            from pr_agent.errors import PoolError

            raise PoolError("refresh failed")

    result = run_day(ws.paths, ws.config, ws.projects, pool=FakePool())
    assert result.status == "skipped" and "刷新失败" in result.message


def _age_record(ws, hours: float) -> dict:
    from datetime import datetime, timedelta, timezone

    record = _record(ws)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0)
    record["last_verified_at"] = stamp.isoformat().replace("+00:00", "Z")
    record["updated_at"] = record["last_verified_at"]
    return record


def test_run_day_refreshes_near_expiry_before_planning(tmp_path):
    """§7.1：开工前先刷新临近过期的候选，再跑 plan-week。"""
    ws = build_workspace(tmp_path)
    record = _age_record(ws, 11)  # freshness=12h，已用 11h
    calls: list[tuple] = []

    class FakePool:
        def show(self, status=None):
            return [record]

        def refresh(self, repo, issue, local_repo=None):
            calls.append((repo, issue))
            return {**record, "status": "VERIFIED"}

        def plan_week(self, target=3, reserve=6):
            return 2, {"selected": [], "target": 3, "warnings": ["weekly plan incomplete"]}, None

        def health(self):
            return {"total": 1, "by_status": {"VERIFIED": 1}}

        def transition(self, *args, **kwargs):
            raise AssertionError("不应迁移状态")

    result = run_day(ws.paths, ws.config, ws.projects, pool=FakePool())
    assert calls == [("Super/Repo", 7)]
    assert result.status == "skipped" and "plan-week 不完整" in result.message


def test_run_day_dry_run_lists_pending_refresh(tmp_path):
    ws = build_workspace(tmp_path)
    record = _age_record(ws, 11)

    class FakePool:
        def show(self, status=None):
            return [record]

        def refresh(self, *args, **kwargs):
            raise AssertionError("dry-run 不得刷新")

    result = run_day(ws.paths, ws.config, ws.projects, dry_run=True, pool=FakePool())
    assert result.status == "dry_run"
    assert any("pool.py refresh" in command for command in result.evidence["refreshes"])


def test_run_day_skips_when_prerefresh_fails(tmp_path):
    ws = build_workspace(tmp_path)
    record = _age_record(ws, 11)

    class FakePool:
        def show(self, status=None):
            return [record, {**record, "id": "Super/Repo#8", "issue_number": 8}]

        def refresh(self, *args, **kwargs):
            from pr_agent.errors import PoolError

            raise PoolError("refresh failed")

    result = run_day(ws.paths, ws.config, ws.projects, pool=FakePool())
    assert result.status == "skipped" and "刷新失败" in result.message
