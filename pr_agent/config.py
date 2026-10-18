"""配置层：加载 + 严格校验 + 环境变量展开（DEVELOPMENT_PLAN §4/§5）。

硬规则：
- 配置非法 = 启动失败（不在运行时猜默认值）。
- `${VAR}` 在加载期展开；缺失变量在关键路径上直接报错。
- 密钥只从环境变量读，不写入任何文件/日志/事件。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError
from .paths import Paths

ADAPTER_NAMES = {"claude_code", "gemini", "codex", "opencode", "workbuddy"}
MODE_NAMES = {"assisted", "full_autonomy"}
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, *, strict: bool, missing: list[str]) -> Any:
    """递归展开 `${VAR}`；strict=False 时缺失变量留空并记录。"""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                missing.append(name)
                return "" if not strict else match.group(0)
            return resolved

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand(item, strict=strict, missing=missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, strict=strict, missing=missing) for item in value]
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(
            f"配置文件不存在：{path}",
            detail="请在 config/ 下按 DEVELOPMENT_PLAN §4/§5 的 schema 创建配置。",
            evidence={"path": str(path)},
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败：{path}", detail=str(exc), evidence={"path": str(path)}) from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置根节点必须是对象：{path}", evidence={"path": str(path)})
    return loaded


class QuotaConfig(BaseModel):
    """额度记账以任务数为主（P1/P2），采样校准留待后续。"""

    model_config = ConfigDict(extra="forbid")

    budget_type: Literal["none", "window", "daily", "weekly"] = "none"
    window_hours: int | None = Field(default=None, ge=1)
    max_tasks_per_window: int | None = Field(default=None, ge=1)
    max_tasks_per_day: int | None = Field(default=None, ge=1)
    max_tasks_per_week: int | None = Field(default=None, ge=1)
    cooldown_minutes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _require_limits(self) -> "QuotaConfig":
        if self.budget_type == "window" and (
            self.window_hours is None or self.max_tasks_per_window is None
        ):
            raise ValueError("budget_type=window 需要 window_hours 与 max_tasks_per_window")
        if self.budget_type == "daily" and self.max_tasks_per_day is None:
            raise ValueError("budget_type=daily 需要 max_tasks_per_day")
        if self.budget_type == "weekly" and self.max_tasks_per_week is None:
            raise ValueError("budget_type=weekly 需要 max_tasks_per_week")
        return self


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    enabled: bool = False
    adapter: str
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    models: dict[str, str] = Field(default_factory=dict)
    timeout_minutes: int = Field(default=90, ge=1)
    max_turns: int = Field(default=200, ge=1)
    command: str = ""  # 可选：覆盖默认可执行文件名

    @field_validator("adapter")
    @classmethod
    def _known_adapter(cls, value: str) -> str:
        if value not in ADAPTER_NAMES:
            raise ValueError(f"未知 adapter：{value}（允许：{sorted(ADAPTER_NAMES)}）")
        return value

    def model_for(self, *, large: bool) -> str | None:
        key = "large" if large else "default"
        value = self.models.get(key) or self.models.get("default")
        return value or None


class FrontendConfig(BaseModel):
    """前端 LLM 配置；不参与关键路径，缺失不阻塞调度。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai-compatible"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "PR_AGENT_FRONTEND_KEY"

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model and os.environ.get(self.api_key_env))

    def describe_gap(self) -> list[str]:
        gaps = []
        if not self.base_url:
            gaps.append("base_url 为空")
        if not self.model:
            gaps.append("model 为空")
        if not os.environ.get(self.api_key_env):
            gaps.append(f"环境变量 {self.api_key_env} 未设置")
        return gaps


class CrossReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    reviewer: str | None = None
    max_diff_lines: int = Field(default=1500, ge=1)
    blocking_findings: list[str] = Field(default_factory=lambda: ["Blocker", "Major"])


class AgentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["assisted", "full_autonomy"] = "assisted"
    max_parallel: int = Field(default=1, ge=1)
    fallback_chain: list[str] = Field(default_factory=list)
    weekly_pr_cap: int = Field(default=3, ge=1)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    cross_review: CrossReviewConfig = Field(default_factory=CrossReviewConfig)
    config_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_chain(self) -> "AgentsConfig":
        if not self.agents:
            raise ValueError("agents 不能为空")
        if not self.fallback_chain:
            raise ValueError("fallback_chain 不能为空")
        seen: set[str] = set()
        for name in self.fallback_chain:
            if name in seen:
                raise ValueError(f"fallback_chain 存在重复项：{name}")
            seen.add(name)
            agent = self.agents.get(name)
            if agent is None:
                raise ValueError(f"fallback_chain 引用了未定义的 agent：{name}")
            if not agent.enabled:
                raise ValueError(
                    f"fallback_chain 中的 agent 必须 enabled=true：{name}"
                    "（DEVELOPMENT_PLAN §4：禁用 agent 不参与分配）"
                )
        for name, agent in self.agents.items():
            agent.name = name
        if self.cross_review.reviewer and self.cross_review.reviewer not in self.agents:
            raise ValueError(f"cross_review.reviewer 未定义：{self.cross_review.reviewer}")
        return self

    def agent(self, name: str) -> AgentConfig:
        try:
            return self.agents[name]
        except KeyError as exc:
            raise ConfigError(f"未定义的 agent：{name}") from exc

    def enabled_agents(self) -> list[str]:
        return [name for name, agent in self.agents.items() if agent.enabled]


class ProjectEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    repo: str
    added_by: Literal["human", "agent"] = "human"
    approved: bool = False
    reason: str = ""
    note: str = ""

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[^/\s#]+/[^/\s#]+", value):
            raise ValueError(f"repo 必须形如 OWNER/REPO：{value}")
        return value


class Principles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_stars: int | None = Field(default=None, ge=0)
    enforce_min_stars: bool = True
    max_recent_competing_prs: int | None = Field(default=None, ge=0)
    max_days_to_first_review: int | None = Field(default=None, ge=0)
    require_backing: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(
        default_factory=lambda: {"potential": 0.4, "feasibility": 0.4, "speed": 0.2}
    )

    @field_validator("weights")
    @classmethod
    def _weights_sum(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("weights 不能为空")
        total = sum(value.values())
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"weights 之和必须为 1.0，当前为 {total}")
        return value


class ProjectsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whitelist: list[ProjectEntry] = Field(default_factory=list)
    blacklist: list[ProjectEntry] = Field(default_factory=list)
    principles: Principles = Field(default_factory=Principles)

    @model_validator(mode="after")
    def _no_overlap(self) -> "ProjectsConfig":
        white = {entry.repo.lower() for entry in self.whitelist}
        black = {entry.repo.lower() for entry in self.blacklist}
        overlap = white & black
        if overlap:
            raise ValueError(f"仓库同时出现在白名单与黑名单：{sorted(overlap)}")
        for listing, label in ((self.whitelist, "whitelist"), (self.blacklist, "blacklist")):
            seen: set[str] = set()
            for entry in listing:
                key = entry.repo.lower()
                if key in seen:
                    raise ValueError(f"{label} 存在重复仓库：{entry.repo}")
                seen.add(key)
        return self

    def approved_repos(self) -> set[str]:
        return {entry.repo.lower() for entry in self.whitelist if entry.approved}

    def blacklisted_repos(self) -> set[str]:
        return {entry.repo.lower() for entry in self.blacklist}

    def pending_proposals(self) -> list[ProjectEntry]:
        return [entry for entry in self.whitelist if not entry.approved]


def load_agents_config(paths: Paths, *, mode: str | None = None) -> AgentsConfig:
    raw = _read_yaml(paths.agents_config)
    # 关键路径缺失变量 -> 失败；frontend 缺失只记录警告（不参与关键路径）
    critical_missing: list[str] = []
    critical = {key: value for key, value in raw.items() if key != "frontend"}
    expanded_critical = _expand(critical, strict=False, missing=critical_missing)
    if critical_missing:
        names = sorted(set(critical_missing))
        raise ConfigError(
            "配置引用的环境变量未设置：" + ", ".join(names),
            detail=f"来源：{paths.agents_config}",
            evidence={"missing_env": names},
        )
    frontend_missing: list[str] = []
    expanded = dict(expanded_critical)
    expanded["frontend"] = _expand(raw.get("frontend", {}), strict=False, missing=frontend_missing)

    warnings: list[str] = []
    if frontend_missing:
        warnings.append(
            "frontend 引用的环境变量未设置：" + ", ".join(sorted(set(frontend_missing)))
            + "（仅影响 chat，不影响调度）"
        )
    if mode:
        if mode not in MODE_NAMES:
            raise ConfigError(f"非法权限模式：{mode}（允许：{sorted(MODE_NAMES)}）")
        expanded["mode"] = mode
    expanded["config_warnings"] = warnings
    try:
        return AgentsConfig.model_validate(expanded)
    except ValidationError as exc:
        raise ConfigError(
            f"agents 配置校验失败：{paths.agents_config}",
            detail=exc.json(indent=2),
            evidence={"errors": exc.errors(include_url=False)},
        ) from exc


def load_projects_config(paths: Paths) -> ProjectsConfig:
    raw = _expand(_read_yaml(paths.projects_config), strict=False, missing=[])
    # YAML 里只有注释的列表会解析成 None；视为空列表（不是"配置缺失"）
    for key in ("whitelist", "blacklist"):
        if raw.get(key) is None:
            raw[key] = []
    try:
        return ProjectsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"projects 配置校验失败：{paths.projects_config}",
            detail=exc.json(indent=2),
            evidence={"errors": exc.errors(include_url=False)},
        ) from exc


def dump_projects_config(paths: Paths, config: ProjectsConfig) -> None:
    """写回 projects.yaml（人工与前端 agent 双写，见 §10）。"""
    payload = config.model_dump(mode="json")
    paths.projects_config.parent.mkdir(parents=True, exist_ok=True)
    paths.projects_config.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
