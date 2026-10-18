"""prompt 组装（DEVELOPMENT_PLAN §6.4，唯一拼装方式）。

final_prompt = canonical prompt + "\\n\\n## 权限模式契约\\n" + mode_delta(mode)
             + 任务上下文（canonical prompt 第 66 行要求调度器注入 dossier/worktree 等指针）

注意：模式增量为**逐字**复制 §6.4-A/B 的契约文本，不做改写、不自行加门禁。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import AssetMissingError

FENCE = re.compile(r"```markdown\n(.*?)\n```", re.DOTALL)

MODE_DELTA: dict[str, str] = {
    "full_autonomy": (
        "本任务运行于 full_autonomy：允许你在 GitHub 上留言、申请/接受认领、推送分支、创建 PR"
        "（使用已登录的 gh CLI，不得读写任何 token 文件）。创建 PR 后必须停在 PENDING_HUMAN，"
        "由人工完成最终审查；不得自动合并、批准自己的 PR、关闭 Issue 或隐藏失败证据。"
    ),
    "assisted": (
        "本任务运行于 assisted：你不得进行任何 GitHub 写操作（不留言、不认领、不 push、不创建 PR；"
        "gh 仅限只读查询）。完成实现与验证后，产出交付包并停止：简短中文报告 report.md、"
        "草稿留言 comment.md、草稿 PR 描述 pr_body.md、人工命令清单 commands.md，写入指定 handoff 目录。"
        "GitHub 上的动作全部由人工执行。"
    ),
}


@dataclass
class CanonicalPrompt:
    text: str
    source_kind: str  # "fence" | "whole_file"
    path: str
    sha256: str


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_canonical(raw: str) -> tuple[str, str]:
    """优先取 ```markdown 代码块（canonical prompt 本体），否则整文件。"""
    match = FENCE.search(raw)
    if match:
        return match.group(1).strip("\n"), "fence"
    return raw.strip("\n"), "whole_file"


def load_canonical(path: Path) -> CanonicalPrompt:
    if not path.is_file():
        raise AssetMissingError(f"找不到 canonical prompt：{path}")
    raw = path.read_text(encoding="utf-8")
    text, kind = extract_canonical(raw)
    return CanonicalPrompt(text=text, source_kind=kind, path=str(path), sha256=sha256_text(text))


def mode_delta(mode: str) -> str:
    try:
        return MODE_DELTA[mode]
    except KeyError as exc:
        raise ValueError(f"非法权限模式：{mode}（允许：{sorted(MODE_DELTA)}）") from exc


def task_context(
    *,
    issue_id: str,
    issue_url: str,
    repo: str,
    issue_number: int,
    task_id: str,
    worktree: str,
    base_branch: str,
    base_sha: str,
    branch: str,
    handoff_dir: str,
    dossier_path: str,
    verification_dir: str,
    mode: str,
    open_coding_root: str,
) -> str:
    github_rule = (
        "允许留言/认领/推分支/建 PR（gh 已登录）；创建 PR 后停在 PENDING_HUMAN。"
        if mode == "full_autonomy"
        else "禁止任何 GitHub 写操作；gh 仅只读；GitHub 动作由人工执行 handoff/commands.md。"
    )
    return "\n".join(
        [
            "## 任务上下文（调度器注入）",
            "",
            f"- Issue：`{issue_id}`（{issue_url}）",
            f"- 目标仓库：`{repo}`，Issue 编号：`#{issue_number}`",
            f"- 任务 ID：`{task_id}`",
            f"- open_coding 根目录（只读权威）：`{open_coding_root}`",
            f"- 独立 worktree（本任务唯一工作目录）：`{worktree}`",
            f"- base 分支：`{base_branch}`，base SHA：`{base_sha}`",
            f"- 本任务分支：`{branch}`",
            f"- 缓存验证原始输出目录：`{verification_dir}`",
            f"- 一 Issue 一文档审查档案（必须生成并补全）：`{dossier_path}`",
            f"- handoff 目录：`{handoff_dir}`",
            f"- GitHub 权限边界：{github_rule}",
            f"- 分支策略（§13）：禁止直接改动 `{repo}` 的 main/master。若无 upstream 写权限，"
            f"必须先 fork（`gh repo fork {repo} --clone=false`）并把分支推到 fork，"
            f"再用 `gh pr create --repo {repo} --head <你的账号>:<分支>` 向 upstream 提交 PR；"
            "不得把 fork 的默认分支当成 base。",
            "- 禁止修改 open_coding 的 `workflow/`、`docs/`、`scripts/`、`ISSUE_SELECTION_RULES.md`、"
            "`GITHUB_TOKEN_SETUP.md`；禁止读取、复制或输出任何 token 文件。",
            "- 不得在 main/master 上改动；不得把上一个任务的上下文、未提交文件或测试结论带入本任务。",
            "",
        ]
    )


def assemble_prompt(canonical: CanonicalPrompt, mode: str, context: str | None = None) -> str:
    parts = [
        canonical.text.rstrip("\n"),
        "",
        "## 权限模式契约",
        mode_delta(mode),
    ]
    if context:
        parts.extend(["", context.rstrip("\n")])
    return "\n".join(parts) + "\n"
