"""计划任务注册（只生成命令，确认后才执行）。"""

from __future__ import annotations

from pr_agent import schedule

from tests.conftest import build_workspace


def test_build_plans_covers_three_jobs(tmp_path):
    ws = build_workspace(tmp_path)
    plans = schedule.build_plans(ws.paths, python="C:/py/python.exe")
    names = [plan.name for plan in plans]
    assert names == ["pr_agent-pool-maint", "pr_agent-monitor", "pr_agent-run-day"]
    pooled = plans[0]
    assert "schtasks" in pooled.display() and "job-pool-maint.cmd" in pooled.display()
    # 真正执行的动作写在包装脚本里（避免 /TR 里嵌套引号被截断）
    assert "job pool-maint" in pooled.script
    assert "C:/py/python.exe" in pooled.script
    assert "09:05" in plans[2].display()
    assert "MON,TUE,WED,THU,FRI" in plans[2].display()


def test_install_requires_confirm(tmp_path):
    ws = build_workspace(tmp_path)
    result = schedule.install(ws.paths, confirm=False, dry_run=True)
    assert result["status"] == "dry_run"
    assert len(result["commands"]) == 3
    assert not (ws.paths.workspace / "scripts").exists()  # dry-run 不落盘


def test_install_writes_wrappers_before_registering(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path)
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return Done()

    monkeypatch.setattr(schedule.subprocess, "run", fake_run)
    result = schedule.install(ws.paths, confirm=True, dry_run=False, python="C:/py/python.exe")
    assert result["status"] == "installed"
    wrapper = ws.paths.workspace / "scripts" / "job-run-day.cmd"
    assert wrapper.is_file() and "job run-day" in wrapper.read_text(encoding="utf-8")
    assert len(calls) == 3 and calls[0][:4] == ["schtasks", "/Create", "/TN", "pr_agent-pool-maint"]


def test_uninstall_dry_run_lists_all():
    result = schedule.uninstall(confirm=False, dry_run=True)
    assert result["status"] == "dry_run"
    assert len(result["commands"]) == 3
