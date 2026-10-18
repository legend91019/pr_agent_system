"""dossier 校验与调度器补充证据的幂等追加。"""

from __future__ import annotations

from pr_agent import dossier


def _diff():
    return {
        "head_sha": "head123",
        "base_sha": "base123",
        "committed_files": [{"status": "M", "path": "pkg/parser.py"}],
        "committed_changed_lines": 12,
        "committed_added_lines": 9,
        "committed_deleted_lines": 3,
        "working_tree_dirty": False,
        "untracked_files": [],
    }


def test_missing_dossier(tmp_path):
    result = dossier.validate(tmp_path / "nope.md")
    assert result["exists"] is False
    assert result["problems"]


def test_append_section_is_idempotent(tmp_path):
    path = tmp_path / "d.md"
    path.write_text("## 1. A\n## 5. B\n## 6. C\n## 8. D\n", encoding="utf-8")
    for _ in range(2):
        dossier.append_orchestrator_section(
            path, task_id="T1", mode="assisted", agent="claude-code", model="m",
            branch="fix/issue-7", base_sha="base123", head_sha="head123", diff=_diff(),
            exit_code=0, timed_out=False, handoff_check={"ok": True, "problems": [], "warnings": []},
            allocation={"agent": "claude-code"},
        )
    text = path.read_text(encoding="utf-8")
    assert text.count(dossier.BEGIN) == 1
    assert text.count(dossier.END) == 1
    assert "调度器补充证据" in text
    assert "pkg/parser.py" in text


def test_validate_warns_on_skeleton(tmp_path):
    path = tmp_path / "d.md"
    path.write_text(
        "## 1.\n" + ("NOT RECORDED\n" * 30) + "## 5.\n" + ("NOT RUN\n" * 10) + "## 6.\n## 8.\n",
        encoding="utf-8",
    )
    result = dossier.validate(path)
    assert result["problems"] == []
    assert result["warnings"]
