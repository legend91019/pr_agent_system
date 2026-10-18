"""交叉 review（DEVELOPMENT_PLAN §12，P3）。

第二意见由**另一家厂商**的模型执行：
- 选择顺序：`agents.yaml` 的 `cross_review.reviewer` → fallback_chain 中第一家厂商不同且可用的 agent。
- 只做 review，不允许写入：以 assisted 权限（acceptEdits）运行，prompt 明确要求只输出意见。
- Blocker/Major → 任务打回 CODING（记录证据），Minor/Nit 写入 dossier 与 handoff/review_findings.md。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .adapters import build_adapter
from .config import AgentsConfig
from .errors import PrAgentError
from .events import now_iso
from .models import Task
from .paths import Paths
from .quota import UsageLedger

VENDOR = {
    "claude_code": "anthropic",
    "gemini": "google",
    "codex": "openai",
    "opencode": "multi",
    "workbuddy": "tencent",
}

PROMPT_TEMPLATE = """你是独立代码审查者，请对下面的改动做一次严格 review。你**不得修改任何文件**，不得进行任何 GitHub 操作，只输出审查意见。

审查标准：
- Blocker：根因未解决、数据/权限/并发错误、API/兼容性破坏、测试或 CI 失败；
- Major：验收条件缺失、错误处理不完整、生成文件/changeset 漏交、测试不能捕获回归；
- Minor/Nit：风格、命名、可维护性、文档问题。

输出格式（严格）：
## Blocker
- <没有则写 无>
## Major
- <没有则写 无>
## Minor/Nit
- <没有则写 无>
## 结论
APPROVE 或 REQUEST_CHANGES

任务：{issue_id}
base SHA：{base_sha}
head SHA：{head_sha}
改动统计：{stats}

```diff
{diff}
```
"""


@dataclass
class ReviewResult:
    reviewer: str | None
    blocking: list[str]
    findings: dict[str, list[str]]
    conclusion: str
    raw: str
    path: str | None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "blocking": self.blocking,
            "findings": self.findings,
            "conclusion": self.conclusion,
            "path": self.path,
            "error": self.error,
        }


def choose_reviewer(config: AgentsConfig, author: str, usage: UsageLedger) -> str | None:
    author_vendor = VENDOR.get(config.agent(author).adapter, author)
    preferred = config.cross_review.reviewer
    ordered = ([preferred] if preferred else []) + [name for name in config.fallback_chain if name != preferred]
    for name in ordered:
        if name == author:
            continue
        agent = config.agents.get(name)
        if agent is None or not agent.enabled:
            continue
        if VENDOR.get(agent.adapter, name) == author_vendor:
            continue
        if usage.quota_left(agent) == 0:
            continue
        ok, _ = build_adapter(agent).probe()
        if not ok:
            continue
        return name
    return None


def parse_findings(text: str) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {"Blocker": [], "Major": [], "Minor/Nit": []}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("## "):
            if "blocker" in lowered:
                current = "Blocker"
            elif "major" in lowered:
                current = "Major"
            elif "minor" in lowered or "nit" in lowered:
                current = "Minor/Nit"
            else:
                current = None
            continue
        if current and stripped.startswith(("-", "*")):
            item = stripped.lstrip("-* ").strip()
            if item and item not in {"无", "None", "none", "N/A"}:
                findings[current].append(item)
    return findings


def run(
    *,
    paths: Paths,
    config: AgentsConfig,
    usage: UsageLedger,
    task: Task,
    task_dir: Path,
    diff_text: str,
    stats: str,
    timeout_seconds: float | None = None,
) -> ReviewResult:
    reviewer = choose_reviewer(config, task.agent, usage)
    if reviewer is None:
        return ReviewResult(None, [], {"Blocker": [], "Major": [], "Minor/Nit": []},
                            "SKIPPED", None, None, "没有可用的跨厂商 reviewer")
    agent = config.agent(reviewer)
    adapter = build_adapter(agent)
    prompt = PROMPT_TEMPLATE.format(
        issue_id=task.issue_id,
        base_sha=task.base_sha,
        head_sha=task.extra.get("head_sha", "UNKNOWN"),
        stats=stats,
        diff=diff_text[: config.cross_review.max_diff_lines * 120],
    )
    prompt_path = task_dir / "cross-review-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    chunks: list[str] = []
    try:
        for event in adapter.spawn(
            prompt=prompt,
            prompt_path=prompt_path,
            cwd=Path(task.worktree),
            model=agent.model_for(large=False),
            full_autonomy=False,
            max_turns=min(agent.max_turns, 60),
            timeout_seconds=timeout_seconds or min(agent.timeout_minutes, 30) * 60,
            task_id=f"{task.task_id}-review",
        ):
            if event.type == "assistant_message":
                chunks.append(str(event.payload.get("text", "")))
            elif event.type == "result":
                chunks.append(str(event.payload.get("result", "")))
    except PrAgentError as exc:
        return ReviewResult(reviewer, [], {"Blocker": [], "Major": [], "Minor/Nit": []},
                            "ERROR", None, None, f"{exc.message} {exc.detail}")

    raw = "\n".join(part for part in chunks if part).strip()
    findings = parse_findings(raw)
    conclusion = "REQUEST_CHANGES" if "REQUEST_CHANGES" in raw else ("APPROVE" if "APPROVE" in raw else "UNKNOWN")
    blocking = findings["Blocker"] + findings["Major"]

    directory = task_dir / "handoff"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "review_findings.md"
    output.write_text(
        "\n".join([
            f"# 交叉 review 发现（reviewer: {reviewer}）",
            "",
            f"- 时间：`{now_iso()}`",
            f"- 结论：`{conclusion}`",
            f"- 阻断项：{len(blocking)}",
            "",
            "## Blocker",
            *[f"- {item}" for item in findings["Blocker"] or ["无"]],
            "",
            "## Major",
            *[f"- {item}" for item in findings["Major"] or ["无"]],
            "",
            "## Minor/Nit",
            *[f"- {item}" for item in findings["Minor/Nit"] or ["无"]],
            "",
            "## 原始输出",
            "",
            "```text",
            raw[:20_000],
            "```",
            "",
        ]),
        encoding="utf-8",
    )
    (task_dir / "cross-review.json").write_text(
        json.dumps({"reviewer": reviewer, "conclusion": conclusion, "findings": findings},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return ReviewResult(reviewer, blocking, findings, conclusion, raw, str(output), None)


def summary_line(result: ReviewResult) -> str:
    if result.reviewer is None:
        return f"交叉 review 跳过：{result.error}"
    return (
        f"交叉 review（{result.reviewer}）结论 {result.conclusion}："
        f"Blocker {len(result.findings['Blocker'])} / Major {len(result.findings['Major'])} / "
        f"Minor {len(result.findings['Minor/Nit'])}"
    )


def looks_like_blocker(text: str) -> bool:  # pragma: no cover - 兼容旧调用
    return bool(re.search(r"\b(Blocker|Major)\b", text, re.IGNORECASE))
