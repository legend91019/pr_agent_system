"""prompt 拼装（§6.4）：逐字契约、顺序、任务上下文。"""

from __future__ import annotations

from pr_agent.prompt import (MODE_DELTA, assemble_prompt, extract_canonical,
                             load_canonical, task_context)


def test_extract_fenced_markdown():
    raw = "# Canonical\n\n```markdown\n你是工程师。\n## 1. 刷新\n```\n\ntail"
    text, kind = extract_canonical(raw)
    assert kind == "fence"
    assert text.startswith("你是工程师。")
    assert "tail" not in text


def test_extract_without_fence_falls_back_to_whole_file():
    text, kind = extract_canonical("纯文本 prompt")
    assert kind == "whole_file"
    assert text == "纯文本 prompt"


def test_load_canonical_records_sha(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("```markdown\nABC\n```\n", encoding="utf-8")
    canonical = load_canonical(path)
    assert canonical.sha256 and canonical.source_kind == "fence"
    assert canonical.text == "ABC"


def test_assemble_order_matches_contract(workspace):
    canonical = load_canonical(workspace.paths.canonical_prompt)
    context = task_context(
        issue_id="Super/Repo#7", issue_url="https://github.com/Super/Repo/issues/7",
        repo="Super/Repo", issue_number=7, task_id="Super__Repo__7",
        worktree="W", base_branch="main", base_sha="abc", branch="fix/issue-7-x",
        handoff_dir="H", dossier_path="D", verification_dir="V",
        mode="assisted", open_coding_root="O",
    )
    prompt = assemble_prompt(canonical, "assisted", context)
    assert prompt.startswith(canonical.text)
    assert prompt.index("## 权限模式契约") < prompt.index("## 任务上下文")
    assert MODE_DELTA["assisted"] in prompt
    assert "禁止任何 GitHub 写操作" in prompt


def test_full_autonomy_delta_is_exact():
    delta = MODE_DELTA["full_autonomy"]
    assert "允许你在 GitHub 上留言、申请/接受认领、推送分支、创建 PR" in delta
    assert "不得自动合并、批准自己的 PR、关闭 Issue 或隐藏失败证据" in delta
    assert "PENDING_HUMAN" in delta


def test_assisted_delta_is_exact():
    delta = MODE_DELTA["assisted"]
    for needle in ("不留言、不认领、不 push、不创建 PR", "report.md", "comment.md",
                   "pr_body.md", "commands.md"):
        assert needle in delta
