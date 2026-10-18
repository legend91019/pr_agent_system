"""一 Issue 一文档审查档案（DEVELOPMENT_PLAN §6.5 / §12）。

复用 open_coding/scripts/render_pr_review.py 生成骨架，绝不重写渲染器；
调度器只做两件事：
1. 档案缺失时用渲染器建骨架（记录为 UNKNOWN，不算通过）；
2. 追加"调度器补充证据"段（真实 diff/base/head/事件/退出码），追加段可重复执行而不重复堆叠。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .errors import BlockedError
from .events import now_iso
from .paths import Paths
from .poolctl import Pool

BEGIN = "<!-- pr-agent:orchestrator:begin -->"
END = "<!-- pr-agent:orchestrator:end -->"
CR_BEGIN = "<!-- pr-agent:cross-review:begin -->"
CR_END = "<!-- pr-agent:cross-review:end -->"


def path_for(paths: Paths, repo: str, issue_number: int) -> Path:
    return paths.dossier_path(repo, issue_number)


def ensure_skeleton(paths: Paths, pool: Pool, record: dict) -> tuple[Path, str | None]:
    """档案缺失时用官方渲染器生成骨架；返回 (路径, 错误说明)。"""
    repo = str(record.get("repo", ""))
    number = int(record.get("issue_number") or 0)
    if not repo or not number:
        raise BlockedError("池记录缺少 repo/issue_number，无法生成 dossier")
    destination = path_for(paths, repo, number)
    if destination.is_file():
        return destination, None
    scratch = paths.logs_dir / "dossier-records"
    scratch.mkdir(parents=True, exist_ok=True)
    record_file = scratch / f"{repo.replace('/', '__')}__{number}.json"
    record_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(paths.renderer), str(record_file), "--output", str(destination)],
        cwd=str(paths.open_coding),
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if completed.returncode != 0 or not destination.is_file():
        return destination, (
            f"render_pr_review.py 执行失败（{completed.returncode}）："
            f"{(completed.stderr or completed.stdout)[-2000:]}"
        )
    return destination, None


def validate(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "problems": ["档案不存在"], "not_recorded": None, "not_run": None}
    text = path.read_text(encoding="utf-8")
    not_recorded = text.count("NOT RECORDED")
    not_run = text.count("NOT RUN")
    problems = []
    warnings = []
    for heading in ("## 1.", "## 5.", "## 6.", "## 8."):
        if heading not in text:
            problems.append(f"缺少必需章节 {heading}")
    if not_recorded > 25:
        warnings.append(f"骨架未补全迹象：出现 {not_recorded} 处 NOT RECORDED")
    if not_run > 8:
        warnings.append(f"验证证据不足：出现 {not_run} 处 NOT RUN")
    if "diff" not in text.lower():
        warnings.append("文档未包含 diff 对照内容")
    return {
        "exists": True,
        "chars": len(text),
        "not_recorded": not_recorded,
        "not_run": not_run,
        "problems": problems,
        "warnings": warnings,
        "path": str(path),
    }


def append_orchestrator_section(
    path: Path,
    *,
    task_id: str,
    mode: str,
    agent: str,
    model: str | None,
    branch: str,
    base_sha: str,
    head_sha: str,
    diff: dict,
    exit_code: int | None,
    timed_out: bool,
    handoff_check: dict | None,
    allocation: dict | None,
    notes: list[str] | None = None,
) -> Path:
    if not path.is_file():
        raise BlockedError(f"dossier 不存在，无法追加调度器证据：{path}")
    files_table = "\n".join(
        f"| `{item.get('path')}` | {item.get('status')} |" for item in diff.get("committed_files", [])
    ) or "| `（无已提交改动）` | - |"
    untracked = "\n".join(f"- `{item}`" for item in diff.get("untracked_files", [])) or "- 无"
    handoff_lines = "未启用（full_autonomy 模式）"
    if handoff_check is not None:
        handoff_lines = (
            f"校验结果：`{'PASS' if handoff_check.get('ok') else 'BLOCKED'}`\n\n"
            + ("\n".join(f"- 问题：{item}" for item in handoff_check.get("problems", [])) or "- 无阻断问题")
            + ("\n" + "\n".join(f"- 提醒：{item}" for item in handoff_check.get("warnings", [])) or "")
        )
    section = "\n".join([
        BEGIN,
        "",
        "## 9. 调度器补充证据（由 pr_agent 自动追加）",
        "",
        f"- 追加时间：`{now_iso()}`",
        f"- 任务：`{task_id}`，模式：`{mode}`，agent：`{agent}`，模型：`{model or '默认'}`",
        f"- 分支：`{branch}`",
        f"- base SHA：`{base_sha}`；head SHA：`{head_sha}`",
        f"- adapter 退出码：`{exit_code}`；超时：`{timed_out}`",
        f"- 已提交改动：{diff.get('committed_changed_lines', 0)} 行"
        f"（+{diff.get('committed_added_lines', 0)} / -{diff.get('committed_deleted_lines', 0)}）",
        f"- 工作区是否仍有未提交改动：`{diff.get('working_tree_dirty')}`",
        "",
        "### 9.1 已提交文件清单（base..HEAD）",
        "",
        "| 文件 | 状态 |",
        "|---|---|",
        files_table,
        "",
        "### 9.2 未跟踪文件",
        "",
        untracked,
        "",
        "### 9.3 handoff 校验",
        "",
        handoff_lines,
        "",
        "### 9.4 分配决策",
        "",
        "```json",
        json.dumps(allocation or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "### 9.5 调度器备注",
        "",
        "\n".join(f"- {item}" for item in (notes or [])) or "- 无",
        "",
        END,
    ])
    text = path.read_text(encoding="utf-8")
    if BEGIN in text and END in text:
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        text = head.rstrip("\n") + "\n\n" + section + tail
    else:
        text = text.rstrip("\n") + "\n\n" + section + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def append_cross_review_section(path: Path, result: dict) -> Path:
    """把交叉 review 的第二意见并入 dossier（DEVELOPMENT_PLAN §12）。

    与调度器补充证据段同样用标记包裹，重复执行只替换不堆叠。
    """
    if not path.is_file():
        raise BlockedError(f"dossier 不存在，无法并入交叉 review：{path}")
    findings = result.get("findings") or {}
    blocking = result.get("blocking") or []

    def bullets(name: str) -> list[str]:
        items = findings.get(name) or []
        return [f"- {item}" for item in items] or ["- 无"]

    section = "\n".join([
        CR_BEGIN,
        "",
        "## 10. 交叉 review（第二意见，由 pr_agent 追加）",
        "",
        f"- 追加时间：`{now_iso()}`",
        f"- reviewer：`{result.get('reviewer') or 'UNKNOWN'}`",
        f"- 结论：`{result.get('conclusion') or 'UNKNOWN'}`",
        f"- 阻断项：{len(blocking)}",
        f"- 原始输出：`{result.get('path') or 'NOT RECORDED'}`",
        f"- 执行错误：{result.get('error') or '无'}",
        "",
        "### 10.1 Blocker",
        "",
        *bullets("Blocker"),
        "",
        "### 10.2 Major",
        "",
        *bullets("Major"),
        "",
        "### 10.3 Minor/Nit",
        "",
        *bullets("Minor/Nit"),
        "",
        CR_END,
    ])
    text = path.read_text(encoding="utf-8")
    if CR_BEGIN in text and CR_END in text:
        head, _, rest = text.partition(CR_BEGIN)
        _, _, tail = rest.partition(CR_END)
        text = head.rstrip("\n") + "\n\n" + section + tail
    else:
        text = text.rstrip("\n") + "\n\n" + section + "\n"
    path.write_text(text, encoding="utf-8")
    return path
