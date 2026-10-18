"""pytest 公共夹具：临时工作区 + 仿真 open_coding + 仿真 adapter。

说明：这里的 open_coding 是**仿真件**，只用于验证 pr_agent 的编排逻辑；
真实工作流（pool.py / 渲染器 / 审计器）的测试在 open_coding 仓库内，不在此重复。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

FAKE_POOL = r'''"""测试用 pool.py 仿真件：保持与真实 pool.py 相同的 CLI 与状态机语义。"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSUES = ROOT / "pool" / "issues.jsonl"
EVENTS = ROOT / "pool" / "events.jsonl"

ALLOWED = {
    "DISCOVERED": {"VERIFIED", "REJECTED", "UNKNOWN", "STALE", "RESERVED"},
    "VERIFIED": {"RESERVED", "STALE", "REJECTED", "UNKNOWN", "BLOCKED"},
    "RESERVED": {"CODING", "STALE", "BLOCKED", "UNKNOWN"},
    "CODING": {"REVIEW", "BLOCKED", "UNKNOWN", "STALE"},
    "REVIEW": {"CODING", "PR_READY", "BLOCKED", "UNKNOWN"},
    "PR_READY": {"PR_OPEN", "CODING", "BLOCKED"},
    "PR_OPEN": {"PENDING_HUMAN", "MERGED", "CHANGES_REQUESTED", "CLOSED", "STALE"},
    "PENDING_HUMAN": {"CHANGES_REQUESTED", "MERGED", "CLOSED", "STALE"},
    "CHANGES_REQUESTED": {"CODING", "STALE", "BLOCKED"},
    "UNKNOWN": {"DISCOVERED", "VERIFIED", "REJECTED", "STALE"},
    "STALE": {"DISCOVERED", "VERIFIED", "REJECTED", "UNKNOWN"},
    "BLOCKED": {"CODING", "UNKNOWN", "STALE", "REJECTED"},
    "REJECTED": {"DISCOVERED", "UNKNOWN"},
    "MERGED": set(),
    "CLOSED": {"DISCOVERED", "STALE"},
}

def load():
    out = {}
    if ISSUES.exists():
        for line in ISSUES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["id"]] = rec
    return out

def save(records):
    ISSUES.write_text("".join(json.dumps(records[k], ensure_ascii=False) + "\n" for k in sorted(records)), encoding="utf-8")

def event(kind, issue_id, evidence=None):
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "issue_id": issue_id, "evidence": evidence or {}}, ensure_ascii=False) + "\n")

args = sys.argv[1:]
cmd = args[0]
records = load()
if cmd == "health":
    counts = {}
    for rec in records.values():
        counts[rec.get("status")] = counts.get(rec.get("status"), 0) + 1
    print(json.dumps({"total": len(records), "by_status": counts}))
elif cmd == "show":
    status = args[args.index("--status") + 1] if "--status" in args else None
    print(json.dumps([r for r in records.values() if not status or r.get("status") == status], ensure_ascii=False))
elif cmd == "transition":
    issue_id = args[args.index("--id") + 1]
    to = args[args.index("--to") + 1]
    rec = records[issue_id]
    old = rec["status"]
    if old == to:
        print(json.dumps(rec, ensure_ascii=False))
        raise SystemExit(0)
    if to not in ALLOWED.get(old, set()):
        print(f"forbidden transition {old} -> {to}", file=sys.stderr)
        raise SystemExit(1)
    rec["status"] = to
    save(records)
    event("TRANSITION", issue_id, {"from": old, "to": to})
    print(json.dumps(rec, ensure_ascii=False))
elif cmd == "plan-week":
    selected = [r for r in records.values()
                if r.get("status") == "VERIFIED" and (r.get("scope_gate") or {}).get("result") == "PASS"]
    warnings = [] if len(selected) >= 3 else ["weekly plan incomplete"]
    print(json.dumps({"selected": selected[:3], "target": 3, "eligible_count": len(selected), "warnings": warnings}, ensure_ascii=False))
    raise SystemExit(0 if not warnings else 2)
elif cmd == "refresh":
    issue_id = f"{args[args.index('--repo') + 1]}#{args[args.index('--issue') + 1]}"
    rec = records.get(issue_id)
    if rec is None:
        print("unknown", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(rec, ensure_ascii=False))
else:
    print(f"unsupported: {cmd}", file=sys.stderr)
    raise SystemExit(2)
'''

FAKE_RENDERER = r'''import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("record")
parser.add_argument("--output")
args = parser.parse_args()
record = json.loads(Path(args.record).read_text(encoding="utf-8"))
text = "\n".join([
    f"# PR Review Dossier — {record.get('repo')} #{record.get('issue_number')}",
    "", "## 1. Project and change overview", "", "## 2. Issue and PR timeline", "",
    "## 3. Root cause and expected behavior", "", "## 4. Code changes", "",
    "## 5. Verification evidence", "", "## 6. Independent review", "",
    "## 7. Risks and unresolved items", "", "## 8. PR and human decision", "",
    "diff: NOT RECORDED", "",
])
Path(args.output).write_text(text, encoding="utf-8")
'''


@dataclass
class Workspace:
    paths: object
    config: object
    projects: object
    repo_dir: Path
    base_sha: str
    issue_id: str


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout.strip()


def build_workspace(tmp_path: Path, *, status: str = "VERIFIED", scope: str = "PASS",
                    repo: str = "Super/Repo", issue_number: int = 7) -> Workspace:
    open_coding = tmp_path / "oc"
    workspace = tmp_path / "ws"
    for sub in ("scripts", "workflow", "pool", "pr_reviews", "repos", "worktrees", "deliverables"):
        (open_coding / sub).mkdir(parents=True, exist_ok=True)
    (workspace / "config").mkdir(parents=True)
    (workspace / "logs").mkdir(parents=True)
    (workspace / "tasks").mkdir(parents=True)

    (open_coding / "scripts" / "pool.py").write_text(FAKE_POOL, encoding="utf-8")
    (open_coding / "scripts" / "issue_audit.py").write_text("", encoding="utf-8")
    (open_coding / "scripts" / "render_pr_review.py").write_text(FAKE_RENDERER, encoding="utf-8")
    (open_coding / "workflow" / "prompt.md").write_text(
        "# Canonical\n\n```markdown\n你是测试用贡献工程师。\n\n## 1. 刷新候选池\n只做测试。\n```\n",
        encoding="utf-8",
    )
    (open_coding / "workflow" / "config.yaml").write_text(
        json.dumps({"freshness_hours": 12, "execution_mode": "manual_only"}), encoding="utf-8"
    )
    (open_coding / "workflow" / "states.md").write_text("states", encoding="utf-8")
    (open_coding / "workflow" / "schema.json").write_text("{}", encoding="utf-8")
    (open_coding / "workflow" / "operator-guide.md").write_text("guide", encoding="utf-8")
    (open_coding / "workflow" / "automation.md").write_text("auto", encoding="utf-8")
    (open_coding / "workflow" / "prompt.md").exists()
    (open_coding / "GITHUB_TOKEN_SETUP.md").write_text("token", encoding="utf-8")
    (open_coding / "ISSUE_SELECTION_RULES.md").write_text("rules", encoding="utf-8")

    repo_dir = open_coding / "repos" / repo.split("/")[-1]
    repo_dir.mkdir()
    _git(["init", "-q"], repo_dir)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], repo_dir)
    _git(["commit", "-q", "-m", "init"], repo_dir)
    (repo_dir / "package.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(["add", "-A"], repo_dir)
    _git(["commit", "-q", "-m", "base"], repo_dir)
    base_sha = _git(["rev-parse", "HEAD"], repo_dir)

    record = {
        "id": f"{repo}#{issue_number}",
        "repo": repo,
        "issue_number": issue_number,
        "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
        "title": "Fix flaky behaviour in parser",
        "status": status,
        "priority": "P1",
        "score": 9.0,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "last_verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "base_sha": base_sha,
        "scope_metrics": {"expected_changed_lines": 220, "expected_changed_files": 4,
                          "production_files": 2, "test_files": 1},
        "scope_gate": {"result": scope, "reason": "test", "checks": [], "warnings": []},
        "evidence": {"source_status": "CHECKED"},
        "collision_prs": [],
        "vetoes": [],
        "report_path": f"pr_reviews/{repo.replace('/', '__')}__{issue_number}.md",
    }
    (open_coding / "pool" / "issues.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (open_coding / "pool" / "events.jsonl").write_text("", encoding="utf-8")

    agents_yaml = {
        "mode": "assisted",
        "max_parallel": 2,
        "weekly_pr_cap": 3,
        "fallback_chain": ["claude-code"],
        "frontend": {"base_url": "", "model": ""},
        "agents": {
            "claude-code": {
                "enabled": True,
                "adapter": "claude_code",
                "quota": {"budget_type": "none"},
                "models": {"default": "fake-model", "large": "fake-large"},
                "timeout_minutes": 1,
                "max_turns": 10,
            }
        },
        "cross_review": {"enabled": False},
    }
    (workspace / "config" / "agents.yaml").write_text(
        yaml.safe_dump(agents_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    projects_yaml = {
        "whitelist": [{"repo": repo, "added_by": "human", "approved": True}],
        "blacklist": [],
        "principles": {"min_stars": 10, "enforce_min_stars": True},
    }
    (workspace / "config" / "projects.yaml").write_text(
        yaml.safe_dump(projects_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    from pr_agent.config import load_agents_config, load_projects_config
    from pr_agent.paths import load_paths

    paths = load_paths(workspace, open_coding)
    return Workspace(paths, load_agents_config(paths), load_projects_config(paths),
                     repo_dir, base_sha, f"{repo}#{issue_number}")


class FakeAdapter:
    """仿真 adapter：按 claude_code 事件形状产出事件，并可写入交付物。"""

    name = "claude_code"
    prompt_transport = "stdin"

    def __init__(self, *, write_handoff: bool = True, write_dossier: bool = True,
                 commit: bool = True, exit_code: int = 0, timed_out: bool = False):
        self.write_handoff = write_handoff
        self.write_dossier = write_dossier
        self.commit = commit
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.prompts: list[str] = []

    def probe(self, *, refresh: bool = False):
        return True, "fake 1.0"

    def available(self) -> bool:
        return True

    def spawn(self, *, prompt, prompt_path, cwd, model, full_autonomy, max_turns,
              timeout_seconds, task_id):
        from pr_agent.adapters.base import Event

        self.prompts.append(prompt)
        task_dir = Path(prompt_path).parent
        yield Event("start", {"argv": ["fake"], "pid": 999999, "model": model}, self.name)
        if self.exit_code == 0 and not self.timed_out:
            if self.write_dossier:
                dossier_path = _extract(prompt, "一 Issue 一文档审查档案（必须生成并补全）：`")
                if dossier_path:
                    Path(dossier_path).write_text(
                        "\n".join([
                            "# PR Review Dossier", "", "## 1. Project and change overview", "ok",
                            "## 2. Issue and PR timeline", "ok", "## 3. Root cause and expected behavior", "ok",
                            "## 4. Code changes", "ok", "## 5. Verification evidence",
                            "focused test PASSED", "## 6. Independent review", "no blockers",
                            "## 7. Risks and unresolved items", "none", "## 8. PR and human decision", "PENDING",
                            "diff: real diff below", "```diff", "-return 1", "+return 2", "```", "",
                        ]),
                        encoding="utf-8",
                    )
            if self.write_handoff:
                marker = _extract(prompt, "Issue：`")
                issue_id = marker or "Super/Repo#7"
                directory = task_dir / "handoff"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "report.md").write_text(
                    "已修复解析器的边界条件；验证 focused test 通过；风险：未跑全量集成测试，已在 dossier 标注。\n"
                    * 3, encoding="utf-8")
                (directory / "comment.md").write_text(
                    f"准备处理此问题。\n\n<!-- agent-pr-workflow: {issue_id} -->\n", encoding="utf-8")
                (directory / "pr_body.md").write_text(
                    f"Fixes #{issue_id.split('#')[-1]}\n\nGenerated-by: pr_agent\n", encoding="utf-8")
                (directory / "commands.md").write_text(
                    "1. git push -u origin fix/issue-7\n"
                    "2. gh pr create --body-file handoff/pr_body.md\n", encoding="utf-8")
            if self.commit:
                (Path(cwd) / "agent_change.py").write_text(
                    "def value():\n    return 2\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-c", "user.email=agent@example.com", "-c", "user.name=Agent",
                     "add", "-A"], cwd=str(cwd), capture_output=True, text=True,
                )
                subprocess.run(
                    ["git", "-c", "user.email=agent@example.com", "-c", "user.name=Agent",
                     "commit", "-q", "-m", "fix(parser): handle edge case"],
                    cwd=str(cwd), capture_output=True, text=True,
                )
        yield Event("assistant_message", {"role": "assistant", "text": "完成实现与验证"}, self.name)
        yield Event("result", {"subtype": "success", "is_error": False, "num_turns": 8,
                               "total_cost_usd": 0.0, "result": "done"}, self.name)
        yield Event("exit", {"returncode": self.exit_code, "timed_out": self.timed_out,
                             "stderr_tail": "" if self.exit_code == 0 else "boom"}, self.name)


def _extract(prompt: str, prefix: str) -> str | None:
    for line in prompt.splitlines():
        if prefix in line:
            tail = line.split(prefix, 1)[1]
            return tail.split("`")[0].strip()
    return None


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return build_workspace(tmp_path)


@pytest.fixture
def fake_build(monkeypatch):
    """把 runner 的 adapter 工厂替换为仿真 adapter。"""
    from pr_agent import runner as runner_mod

    holder = {}

    def install(**kwargs):
        adapter = FakeAdapter(**kwargs)
        holder["adapter"] = adapter
        monkeypatch.setattr(runner_mod, "build_adapter", lambda agent: adapter)
        return adapter

    return install
