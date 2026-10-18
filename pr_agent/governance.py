"""白/黑名单治理（DEVELOPMENT_PLAN §10）。

P1/P2 采用"显式名单 + min_stars 硬门槛"：
- blacklist → 直接拒绝；
- approved whitelist → 通过；
- 其余仓库：有 star 证据且满足 min_stars 才通过，证据缺失 = UNKNOWN = 拒绝（fail-closed）；
- `--allow-unlisted` 属于人工显式覆盖，必须落事件（不是门禁降级，而是人工决策留痕）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ProjectEntry, ProjectsConfig, dump_projects_config, load_projects_config
from .events import append_jsonl, now_iso
from .paths import Paths


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    kind: str  # whitelist | blacklist | principles | unlisted | override
    stars: int | None = None

    def as_event(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "kind": self.kind, "stars": self.stars}


def star_count(record: dict) -> int | None:
    candidates = [
        record.get("repo_stars"),
        record.get("stars"),
        (record.get("evidence") or {}).get("stars") if isinstance(record.get("evidence"), dict) else None,
        (record.get("evidence") or {}).get("repo_stars") if isinstance(record.get("evidence"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def check_repo(
    config: ProjectsConfig,
    record: dict,
    *,
    allow_unlisted: bool = False,
) -> GateDecision:
    repo = str(record.get("repo") or "")
    key = repo.lower()
    if not repo:
        return GateDecision(False, "池记录缺少 repo 字段", "unlisted")
    if key in config.blacklisted_repos():
        reason = next(
            (entry.reason for entry in config.blacklist if entry.repo.lower() == key), ""
        )
        return GateDecision(False, f"仓库在黑名单：{repo}（{reason or '未注原因'}）", "blacklist")
    if key in config.approved_repos():
        return GateDecision(True, f"仓库在已批准白名单：{repo}", "whitelist")

    principles = config.principles
    stars = star_count(record)
    if allow_unlisted:
        return GateDecision(True, f"人工显式覆盖未列名仓库：{repo}", "override", stars)
    if principles.min_stars is None:
        return GateDecision(
            False,
            f"仓库不在白名单且未配置 min_stars 门槛：{repo}（需人工批准或 --allow-unlisted）",
            "unlisted",
            stars,
        )
    if not principles.enforce_min_stars:
        return GateDecision(True, f"min_stars 门槛已关闭，原则放行：{repo}", "principles", stars)
    if stars is None:
        return GateDecision(
            False,
            f"仓库不在白名单，且缺少 star 证据（UNKNOWN ≠ 通过）：{repo}",
            "unlisted",
            stars,
        )
    if stars < principles.min_stars:
        return GateDecision(
            False,
            f"star 数 {stars} 低于门槛 {principles.min_stars}：{repo}",
            "principles",
            stars,
        )
    return GateDecision(
        True,
        f"star 数 {stars} ≥ 门槛 {principles.min_stars}：{repo}（原则放行）",
        "principles",
        stars,
    )


def propose(paths: Paths, repo: str, *, reason: str = "", added_by: str = "agent") -> ProjectEntry:
    """写入待批准提案（approved=false），并写事件。"""
    config = load_projects_config(paths)
    if any(entry.repo.lower() == repo.lower() for entry in config.whitelist):
        raise ValueError(f"白名单已存在该仓库：{repo}")
    entry = ProjectEntry(repo=repo, added_by=added_by, approved=False, reason=reason)
    config.whitelist.append(entry)
    dump_projects_config(paths, config)
    append_jsonl(paths.logs_dir / "governance.jsonl", {
        "at": now_iso(),
        "action": "propose",
        "repo": repo,
        "added_by": added_by,
        "approved": False,
        "reason": reason,
    })
    return entry


def approve(paths: Paths, repo: str, *, approved_by: str = "human") -> ProjectEntry:
    config = load_projects_config(paths)
    for entry in config.whitelist:
        if entry.repo.lower() == repo.lower():
            entry.approved = True
            dump_projects_config(paths, config)
            append_jsonl(paths.logs_dir / "governance.jsonl", {
                "at": now_iso(),
                "action": "approve",
                "repo": repo,
                "approved_by": approved_by,
            })
            return entry
    raise ValueError(f"白名单中不存在该仓库：{repo}")


def pending_proposals(paths: Paths) -> list[ProjectEntry]:
    return load_projects_config(paths).pending_proposals()


def collect_discoveries(tasks_dir: Path) -> list[dict]:
    """汇总后端 agent 在执行中上报的潜力项目（tasks/<id>/discoveries/*.md）。"""
    found: list[dict] = []
    if not tasks_dir.is_dir():
        return found
    for directory in sorted(tasks_dir.glob("*/discoveries")):
        for file in sorted(directory.glob("*.md")):
            try:
                snippet = file.read_text(encoding="utf-8")[:400]
            except OSError as exc:
                snippet = f"<读取失败：{exc}>"
            found.append({
                "task_id": directory.parent.name,
                "path": str(file),
                "snippet": snippet,
                "reported_at": now_iso(),
            })
    return found


@dataclass
class PrincipleScore:
    """原则评分（§5 weights 的落地形式，P3）。

    三个维度都只用池记录里**已有证据**计算，缺证据给 0 并列出缺失项（fail-closed）：
    - potential   = 审计分 score / 10（0-10 → 0-1）
    - feasibility = scope_gate 通过且 scope_metrics 给出改动规模 → 1，否则 0
    - speed       = 1 - 竞态 PR 数 / max_recent_competing_prs（竞态越少越快）
    """

    total: float
    breakdown: dict[str, float]
    missing: list[str]
    veto: str | None = None

    @property
    def eligible(self) -> bool:
        return self.veto is None and self.total > 0

    def as_event(self) -> dict:
        return {"total": round(self.total, 4), "breakdown": self.breakdown,
                "missing": self.missing, "veto": self.veto}


def score_record(config: ProjectsConfig, record: dict) -> PrincipleScore:
    principles = config.principles
    weights = principles.weights
    missing: list[str] = []
    veto: str | None = None

    raw_score = record.get("score")
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        potential = max(0.0, min(1.0, float(raw_score) / 10.0))
    else:
        potential = 0.0
        missing.append("potential: 池记录缺 score")

    gate = record.get("scope_gate") if isinstance(record.get("scope_gate"), dict) else {}
    metrics = record.get("scope_metrics") if isinstance(record.get("scope_metrics"), dict) else {}
    if gate.get("result") == "PASS" and metrics.get("expected_changed_lines") and metrics.get("expected_changed_files"):
        feasibility = 1.0
    else:
        feasibility = 0.0
        missing.append("feasibility: scope_gate 未通过或缺少 scope_metrics")

    competing = record.get("collision_prs")
    if isinstance(competing, list):
        limit = principles.max_recent_competing_prs
        if limit is not None and len(competing) > limit:
            veto = f"竞态 PR {len(competing)} 个 > 上限 {limit}"
            speed = 0.0
        else:
            speed = 1.0 - (len(competing) / limit if limit else 0.0)
            speed = max(0.0, min(1.0, speed))
    else:
        speed = 0.0
        missing.append("speed: 池记录缺 collision_prs（竞态未查询 = UNKNOWN）")

    total = (weights.get("potential", 0.0) * potential
             + weights.get("feasibility", 0.0) * feasibility
             + weights.get("speed", 0.0) * speed)
    return PrincipleScore(round(total, 4),
                          {"potential": round(potential, 3), "feasibility": round(feasibility, 3),
                           "speed": round(speed, 3)},
                          missing, veto)
