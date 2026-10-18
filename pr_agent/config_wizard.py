"""配置面板（DEVELOPMENT_PLAN §9.1 补充，2026-10-18 用户要求）。

交互式生成/更新 `config/agents.yaml` 与 `config/projects.yaml`：
- 覆盖前自动备份到 `logs/config_backup/<时间戳>/`；
- 只写非密钥配置；API key 一律经环境变量（面板只打印设置指引，绝不落盘）；
- 写完立刻用正式加载器回读校验（配置非法 = 面板失败，不留坏文件）。

用户裁决（2026-10-18）：选题不做加权评分，面板不询问 weights 等已废弃项。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from . import ghcli
from .adapters import probe_all
from .config import (
    AgentConfig,
    AgentsConfig,
    CrossReviewConfig,
    FrontendConfig,
    ProjectEntry,
    ProjectsConfig,
    Principles,
    QuotaConfig,
    dump_agents_config,
    dump_projects_config,
    load_agents_config,
    load_projects_config,
)
from .errors import PrAgentError
from .events import append_jsonl, now_iso
from .paths import Paths

# 面板顺序与默认值（agent 键名沿用现有 agents.yaml 的连字符风格）
AGENT_SPECS: list[dict] = [
    {"key": "claude-code", "adapter": "claude_code", "label": "Claude Code",
     "model": "sonnet", "large": "opus", "budget": ("window", {"window_hours": 5, "max_tasks_per_window": 10})},
    {"key": "gemini", "adapter": "gemini", "label": "Gemini CLI",
     "model": "gemini-2.5-pro", "large": "", "budget": ("daily", {"max_tasks_per_day": 20})},
    {"key": "codex", "adapter": "codex", "label": "Codex",
     "model": "", "large": "", "budget": ("weekly", {"max_tasks_per_week": 30})},
    {"key": "opencode", "adapter": "opencode", "label": "OpenCode",
     "model": "", "large": "", "budget": ("none", {})},
    {"key": "workbuddy", "adapter": "workbuddy", "label": "WorkBuddy",
     "model": "", "large": "", "budget": ("none", {})},
]
BUDGET_TYPES = ["none", "window", "daily", "weekly"]


# ---------------------------------------------------------------- IO 抽象
class WizardIO:
    def ask(self, prompt: str, default: str = "") -> str:
        raise NotImplementedError

    def confirm(self, prompt: str, default: bool = True) -> bool:
        raise NotImplementedError

    def choose(self, prompt: str, options: list[str], default: str) -> str:
        raise NotImplementedError

    def info(self, text: str) -> None:
        raise NotImplementedError


class ConsoleIO(WizardIO):
    """真人终端交互（回车 = 取默认值）。"""

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f"（回车={default or '空'}）" if default or default == "" else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        return raw if raw else default

    def confirm(self, prompt: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        raw = input(f"{prompt}（{hint}）: ").strip().lower()
        if not raw:
            return default
        return raw in {"y", "yes", "是", "1", "true"}

    def choose(self, prompt: str, options: list[str], default: str) -> str:
        raw = input(f"{prompt}（{' | '.join(options)}，回车={default}）: ").strip()
        while raw and raw not in options:
            raw = input(f"  无效选项，请从 {' | '.join(options)} 中选择（回车={default}）: ").strip()
        return raw or default

    def info(self, text: str) -> None:
        print(text)


class ScriptedIO(WizardIO):
    """脚本化应答（测试/非交互模式）；脚本耗尽后一律取默认值。"""

    def __init__(self, answers: list | None = None):
        self.answers = list(answers or [])
        self.log: list[tuple[str, str]] = []

    def _next(self):
        return self.answers.pop(0) if self.answers else None

    def ask(self, prompt: str, default: str = "") -> str:
        self.log.append(("ask", prompt))
        raw = self._next()
        return str(raw) if raw is not None else str(default)

    def confirm(self, prompt: str, default: bool = True) -> bool:
        self.log.append(("confirm", prompt))
        raw = self._next()
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"y", "yes", "是", "1", "true"}

    def choose(self, prompt: str, options: list[str], default: str) -> str:
        self.log.append(("choose", prompt))
        raw = self._next()
        value = str(raw) if raw is not None else default
        return value if value in options else default

    def info(self, text: str) -> None:
        self.log.append(("info", text))


def _default_probes() -> dict[str, tuple[bool, str]]:
    stubs = {spec["key"]: AgentConfig(enabled=True, adapter=spec["adapter"]) for spec in AGENT_SPECS}
    return {
        item["agent"]: (bool(item["available"]), str(item["detail"]))
        for item in probe_all(stubs)
    }


def _ask_int(io: WizardIO, prompt: str, default: int) -> int:
    raw = io.ask(prompt, str(default))
    try:
        return int(raw)
    except ValueError:
        io.info(f"  输入不是整数，使用默认值 {default}")
        return default


# ---------------------------------------------------------------- 主流程
def run_wizard(
    paths: Paths,
    io: WizardIO,
    *,
    probes: dict[str, tuple[bool, str]] | None = None,
    gh_check: Callable[[], tuple[bool, str]] | None = None,
) -> dict:
    """跑一遍配置面板；返回摘要（written / warnings / next_steps）。"""
    if probes is None:
        probes = _default_probes()
    if gh_check is None:
        gh_check = ghcli.auth_status

    paths.ensure_local_dirs()
    warnings: list[str] = []
    next_steps: list[str] = []

    io.info("== pr_agent 配置面板 ==")
    io.info(f"工作区：{paths.workspace}")
    io.info("回车 = 接受默认值；已存在的配置会先备份到 logs/config_backup/。")

    # --- 1. 权限模式 ---
    io.info("")
    io.info("-- 1/6 权限模式 --")
    io.info("  assisted：agent 零 GitHub 写操作，产出报告/草稿 PR/草稿留言，人工执行")
    io.info("  full_autonomy：agent 直接留言/认领/推分支/建 PR（token 需预先配置好）")
    mode = io.choose("权限模式", ["assisted", "full_autonomy"], "assisted")

    # --- 2. 调度参数 ---
    io.info("")
    io.info("-- 2/6 调度参数 --")
    max_parallel = _ask_int(io, "全局同时执行任务上限", 2)
    weekly_pr_cap = _ask_int(io, "每周最多创建 PR 数", 3)

    # --- 3. agents ---
    io.info("")
    io.info("-- 3/6 coding agents（先探测本机可用性）--")
    agents: dict[str, AgentConfig] = {}
    enabled_order: list[str] = []
    for spec in AGENT_SPECS:
        key = spec["key"]
        available, detail = probes.get(key, (False, "未探测"))
        io.info(f"  {spec['label']}: {'可用' if available else '不可用'}（{detail}）")
        default_on = available and key != "workbuddy"  # workbuddy 接口待定（P4），默认不启用
        if not io.confirm(f"启用 {spec['label']}?", default=default_on):
            continue
        model = io.ask(f"  {spec['label']} 默认模型", spec["model"])
        large = io.ask(f"  {spec['label']} 大改动模型（可空）", spec["large"])
        budget_type = io.choose("  额度类型", BUDGET_TYPES, spec["budget"][0])
        quota_kwargs: dict = {}
        if budget_type == "window":
            quota_kwargs["window_hours"] = _ask_int(io, "    窗口小时数", 5)
            quota_kwargs["max_tasks_per_window"] = _ask_int(io, "    每窗口任务上限", 10)
        elif budget_type == "daily":
            quota_kwargs["max_tasks_per_day"] = _ask_int(io, "    每日任务上限", 20)
        elif budget_type == "weekly":
            quota_kwargs["max_tasks_per_week"] = _ask_int(io, "    每周任务上限", 30)
        models: dict[str, str] = {}
        if model:
            models["default"] = model
        if large:
            models["large"] = large
        agents[key] = AgentConfig(
            enabled=True,
            adapter=spec["adapter"],
            quota=QuotaConfig(budget_type=budget_type, **quota_kwargs),
            models=models,
        )
        enabled_order.append(key)

    if not agents:
        raise PrAgentError(
            "未启用任何 coding agent，配置无法生效",
            detail="至少启用一家 agent（本机探测结果见上方输出）。",
        )

    # --- 4. fallback 链 ---
    io.info("")
    io.info("-- 4/6 fallback 链（额度满时按此顺序降级）--")
    suggested = " → ".join(enabled_order)
    io.info(f"  建议顺序：{suggested}")
    if io.confirm("使用建议顺序?", default=True):
        fallback_chain = enabled_order
    else:
        raw = io.ask("自定义顺序（逗号分隔，必须是已启用的 agent）", suggested)
        fallback_chain = [item.strip() for item in raw.split(",") if item.strip()]
        unknown = [item for item in fallback_chain if item not in agents]
        if unknown:
            raise PrAgentError(
                f"fallback 链包含未启用的 agent：{unknown}",
                detail=f"已启用：{list(agents)}",
            )

    # --- 5. 交叉 review / 前端 LLM ---
    io.info("")
    io.info("-- 5/6 交叉 review 与前端 LLM --")
    cross_enabled = io.confirm("启用跨厂商交叉 review?（会额外消耗另一家 agent 的额度）", default=False)
    reviewer: str | None = None
    if cross_enabled:
        others = [key for key in enabled_order if key != enabled_order[0]]
        if not others:
            warnings.append("只有一家 agent，交叉 review 无第二厂商可用，保持关闭")
            cross_enabled = False
        else:
            reviewer = io.choose("  reviewer（第二意见由谁出）", others, others[0])
    if io.confirm("配置前端对话 LLM?（可选，只影响 chat，不影响调度）", default=True):
        base_url = io.ask("  base_url（OpenAI 兼容端点）", "")
        model = io.ask("  模型名", "")
        api_key_env = io.ask("  API key 的环境变量名（key 本身绝不写入文件）", "PR_AGENT_FRONTEND_KEY")
        frontend = FrontendConfig(base_url=base_url, model=model, api_key_env=api_key_env)
        if base_url and not ghcli_available_env(api_key_env):
            next_steps.append(f"设置前端 LLM 密钥：setx {api_key_env} \"<你的key>\"（新开终端生效）")
    else:
        frontend = FrontendConfig()

    # --- 6. 项目层 ---
    io.info("")
    io.info("-- 6/6 项目层（白名单 / 黑名单 / 硬门槛）--")
    io.info("  选题不做加权评分（2026-10-18 用户裁决）；这里只配硬门禁。")
    whitelist: list[ProjectEntry] = []
    while io.confirm(f"添加白名单仓库?{'（建议至少一个，否则非白名单仓全靠 star 门槛）' if not whitelist else ''}",
                     default=not whitelist):
        repo = io.ask("  OWNER/REPO", "")
        if not repo:
            io.info("  跳过（未输入）")
            break
        reason = io.ask("  理由（可选）", "")
        whitelist.append(ProjectEntry(repo=repo, added_by="human", approved=True, reason=reason))
    blacklist: list[ProjectEntry] = []
    while io.confirm("添加黑名单仓库?", default=False):
        repo = io.ask("  OWNER/REPO", "")
        if not repo:
            break
        reason = io.ask("  理由", "")
        blacklist.append(ProjectEntry(repo=repo, added_by="human", approved=True, reason=reason))
    min_stars = _ask_int(io, "非白名单仓库 star 门槛（0 = 关闭）", 150)
    max_competing = _ask_int(io, "同 Issue 竞态 PR 上限（超限拒绝）", 3)

    # --- 组装 + 校验（构造即校验，非法直接抛错，不写盘）---
    agents_config = AgentsConfig(
        mode=mode,
        max_parallel=max_parallel,
        fallback_chain=fallback_chain,
        weekly_pr_cap=weekly_pr_cap,
        frontend=frontend,
        agents=agents,
        cross_review=CrossReviewConfig(enabled=cross_enabled, reviewer=reviewer),
    )
    projects_config = ProjectsConfig(
        whitelist=whitelist,
        blacklist=blacklist,
        principles=Principles(
            min_stars=min_stars or None,
            enforce_min_stars=min_stars > 0,
            max_recent_competing_prs=max_competing,
        ),
    )

    # --- full_autonomy 前置检查（只提醒，不阻断写配置）---
    if mode == "full_autonomy":
        ok, detail = gh_check()
        if ok:
            io.info("gh 认证：OK")
        else:
            warnings.append("full_autonomy 模式但 gh 未认证；任务会在开工自检时 BLOCKED")
            next_steps.append(
                "配置 GitHub token：powershell -ExecutionPolicy Bypass -File "
                f"{paths.open_coding / 'scripts' / 'store_github_token.ps1'}"
                "（见 open_coding/GITHUB_TOKEN_SETUP.md，密钥不进聊天不入库）"
            )

    # --- 备份 + 写盘 + 回读 ---
    written = _backup_and_write(paths, agents_config, projects_config)
    try:
        load_agents_config(paths)
        load_projects_config(paths)
    except PrAgentError as exc:
        raise PrAgentError(
            "配置面板写盘后回读校验失败（原配置已备份，可恢复）",
            detail=str(exc),
            evidence={"backup_dir": written.get("backup_dir")},
        ) from exc

    append_jsonl(paths.logs_dir / "config_wizard.jsonl", {
        "at": now_iso(),
        "mode": mode,
        "agents": list(agents),
        "fallback_chain": fallback_chain,
        "whitelist": [entry.repo for entry in whitelist],
        "backup_dir": written.get("backup_dir"),
    })

    next_steps.append("运行 pr_agent doctor 查看环境自检；pr_agent status 查看池子与额度")
    summary = {
        "mode": mode,
        "agents": list(agents),
        "fallback_chain": fallback_chain,
        "cross_review": {"enabled": cross_enabled, "reviewer": reviewer},
        "whitelist": [entry.repo for entry in whitelist],
        "blacklist": [entry.repo for entry in blacklist],
        "min_stars": min_stars,
        "max_recent_competing_prs": max_competing,
        "written": written,
        "warnings": warnings,
        "next_steps": next_steps,
    }
    io.info("")
    io.info("== 配置完成 ==")
    for line in summary["next_steps"]:
        io.info(f"  - {line}")
    for line in warnings:
        io.info(f"  [提醒] {line}")
    return summary


def ghcli_available_env(name: str) -> bool:
    import os

    return bool(os.environ.get(name))


def _backup_and_write(paths: Paths, agents_config: AgentsConfig, projects_config: ProjectsConfig) -> dict:
    backup_dir: Path | None = None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if paths.agents_config.is_file() or paths.projects_config.is_file():
        backup_dir = paths.logs_dir / "config_backup" / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        for source in (paths.agents_config, paths.projects_config):
            if source.is_file():
                (backup_dir / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    dump_agents_config(paths, agents_config)
    dump_projects_config(paths, projects_config)
    return {
        "agents_yaml": str(paths.agents_config),
        "projects_yaml": str(paths.projects_config),
        "backup_dir": str(backup_dir) if backup_dir else None,
    }
