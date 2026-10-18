# pr_agent

**面向个人开发者的多 coding agent PR 调度系统。** pr_agent 把你闲置的 coding agent 额度（Claude Code、Gemini CLI、Codex、OpenCode）变成一条开源贡献生产线：选出一个已验证的 Issue → 派给可用 agent 以 **headless 子进程**在**独立 git worktree** 中执行 → 产出**一 Issue 一档案**的审查文档 → 停在**人工终审**门禁——系统绝不自行合并或批准。

[English](README.md) | [简体中文](README_zh-CN.md)

---

## 为什么做

大多数人同时握有几家 AI coding agent 的额度，却任其过期。手工操作意味着开一堆终端、丢失状态跟踪、上下文互相污染。pr_agent 把整个闭环自动化，但坚持一条不可协商的底线：**最终决定永远由人做出**。

三条原则贯穿所有代码路径：

| 原则 | 含义 |
|---|---|
| **fail-closed** | 读不到、验不了的东西就是 `UNKNOWN` / `BLOCKED`，绝不解释成"没问题" |
| **证据优先** | 原始输出落盘（事件流、diff、验证日志），禁止总结成"失败但有价值" |
| **人工终审** | 合并、批准、关闭 Issue 永远是人的决定 |

## 架构

```text
┌────────────────────────────────────────────────────────────────┐
│  你（对话 / CLI）                                               │
│      pr_agent chat · pick · status · config wizard             │
└──────────────┬─────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────┐
│  调度器（pr_agent core）                                        │
│  · 权限模式：assisted / full_autonomy                           │
│  · 额度记账（窗口/日/周）+ fallback 链                           │
│  · 白/黑名单 + 硬门槛（min_stars、竞态 PR 上限）                 │
│  · 任务状态机（复用池引擎）                                      │
│  · 定时作业：pool-maint · run-day · monitor                     │
└──────────────┬─────────────────────────────────────────────────┘
               ▼  统一 adapter 协议：spawn · 事件流 · 结果回收
┌────────────────────────────────────────────────────────────────┐
│  headless CLI agent（每个任务独占一个 git worktree）             │
│  claude-code · gemini · codex · opencode ·（workbuddy，P4）     │
└──────────────┬─────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────┐
│  跨厂商交叉 review（可选） →  审查档案  →  PENDING_HUMAN 人工门 │
└────────────────────────────────────────────────────────────────┘
```

关键设计决策：

- **headless CLI，绝不 GUI 自动化。** 所有支持的 agent 都以非交互方式运行（`claude -p`、`gemini -p`、`codex exec`、`opencode run`）并输出 JSON 事件流。GUI 脚本脆弱、慢、不可审计——只保留给纯 GUI 工具（暂不在范围内）。
- **一任务一 worktree。** agent 之间绝不共享工作区，上下文不会串台，两家 agent 可安全并行。
- **adapter 永不触碰 token。** 凭据存在操作系统凭据库 / `gh` 里，由 `gh` 自己消费；调度器在 full-autonomy 开工前只做 `gh auth status` 自检。
- **本仓库是编排层，不是新工作流。** 候选池状态机、准入审计、dossier 渲染全部复用配套的 `open_coding` 工作流（只读权威资产），pr_agent 只调用、不重写。

## Workflow（一个任务的一生）

1. **池子维护**（`job pool-maint`）：刷新候选 Issue 记录，凑够 ≥6 个新鲜 `VERIFIED`、跨 ≥3 仓。不足就如实报告——绝不降门禁凑数。
2. **选题**（人工 `pick`，或定时 `job run-day`）：按 plan-week 顺序取第一个合规候选。**不做加权评分**——优先级交给 agent/人工判断，只保留硬门槛（见下）。
3. **打包**：canonical 工作流 prompt + 权限模式契约 + 任务上下文 + fork/upstream 分支策略拼装成完整任务包。
4. **派发**：调度器分配 agent（enabled + 探测可用 + 额度未耗尽 + 冷却已过），在全新 worktree 里 headless 启动，全部事件写入 `tasks/<id>/events.jsonl`。
5. **交付**：
   - `full_autonomy`——agent 经 `gh` 留言、推分支、建 PR，然后停在 `PENDING_HUMAN`。
   - `assisted`（默认）——**对 GitHub 零写操作**；agent 产出交付包：`report.md`、`comment.md`（草稿留言）、`pr_body.md`（草稿 PR）、`commands.md`（一键复制命令清单）。你人工执行后调 `task handoff-done` 回填 URL。
6. **跨厂商交叉 review**（可选）：由**另一家厂商**的 agent 对 diff 出第二意见；Blocker/Major 打回重做，Minor 写进档案。
7. **档案 + 人工门禁**：每个任务必产出一份 Issue 专属审查文档（`pr_reviews/<owner>__<repo>__<issue>.md`），含 diff 摘要、原始验证输出、未验证项。池记录停在 `PENDING_HUMAN`——合并是你的事。

## 治理：白名单/黑名单机制（我们独到的部分）

这是 pr_agent 与"随手让 agent 做个 Issue"的本质区别：

**白名单条目有三个来源，但只有一道审批闸门。**

| 来源 | 路径 | 是否生效 |
|---|---|---|
| 人工直接编辑 `config/projects.yaml` | `added_by: human, approved: true` | 立即生效 |
| 前端 agent 提案 | `pr_agent whitelist propose OWNER/REPO` 写入 `added_by: agent, approved: false` | **批准前绝不生效** |
| 后端 agent 发现 | agent 把潜力仓库写进 `tasks/<id>/discoveries/`，由 `pool-maint` 汇总上报 | 走同一条审批链 |

所有提案/批准动作写入 `logs/governance.jsonl` 留痕。未批准的提案绝不影响分配。

**黑名单 + 硬门槛，而非评分。** 显式黑名单（`repo: someone/noisy-repo, reason: ...`）一票否决。其余仓库只过两道硬门槛——**刻意不做加权评分**（2026-10 的设计裁决：评分公式假装了证据并不支撑的精确度，所以优先级判断交还给人/agent）：

- `min_stars`（默认 150）——没星标的仓库不值得你的额度；
- `max_recent_competing_prs`（默认 3）——同 Issue 语义竞态 PR 太多直接 veto，调度器按顺序取下一个合规候选。

**六道开工门禁（fail-closed）。** 派发前逐条校验：池记录必须是 `VERIFIED`、`scope_gate` 必须 `PASS`、记录必须新鲜、仓库必须过治理、agent 必须 enabled + 探测可用 + 额度充足 + 冷却已过、并发槽（含 adapter 锁与 worktree 锁）必须空闲。任何一条不满足都是拒绝，不是跳过。

## 定时作业

| 作业 | 频率 | 语义 |
|---|---|---|
| `job pool-maint` | 每 6 小时 | 刷新候选，凑够 ≥6 个新鲜 `VERIFIED`（≥3 仓、单仓 ≤2）；不足如实报告，不降门禁凑数 |
| `job run-day` | 周一至五 09:05 | `plan-week --target 3 --reserve 6`；计划不完整即跳过；每日至多 1 个生产任务；连续失败触发断路器当日停产 |
| `job monitor` | 每 6 小时 | 只读记录 CI、维护者评论、竞态 PR——绝不合并、批准、回复 |

承载双选：内置 `pr_agent daemon`（单进程循环），或 `pr_agent install-schedule`（生成 `scripts/job-*.cmd` 包装脚本后用 Windows `schtasks` 注册；`--confirm` 才真正落盘）。

## 快速开始

前置条件：Python ≥ 3.11、git、[GitHub CLI](https://cli.github.com/)（已登录）、至少一个受支持的 coding agent CLI、以及本地可用的配套 `open_coding` 工作流资产（清单见 `DEVELOPMENT_PLAN.md` §2.3——调度器会自检，缺任何一项直接拒绝开工）。

```bash
git clone https://github.com/legend91019/pr_agent_system.git
cd pr_agent_system

python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e ".[dev]"
```

**1. 用交互式配置面板一次配齐**（推荐）：

```bash
pr_agent config wizard
```

wizard 自动探测本机已装 CLI，然后带你过完 `config/agents.yaml`（模式、逐家 agent 的启用/模型/额度、fallback 链）和 `config/projects.yaml`（白/黑名单、`min_stars`、竞态上限、权限模式、每日上限、reserve 预留额）。已有值作为默认回显，写前留 `.bak` 备份，落盘前 dry-run 校验。`pr_agent config show` 脱敏显示当前配置。

**2. 自检，然后先看后跑：**

```bash
pr_agent doctor                # 环境 / 资产 / 配置 / adapter / gh 一次看清（只读）
pr_agent status                # 池子分布、额度、任务、并发槽、断路器

pr_agent pick OWNER/REPO 123 --dry-run    # 只看计划，不执行
pr_agent pick OWNER/REPO 123 --no-run     # 只打包；检查 tasks/<id>/prompt.md
pr_agent run <task_id>                    # 真正派发
```

**3. 日常驾驶：**

```bash
pr_agent job run-day --dry-run   # 晨间作业会做什么
pr_agent job pool-maint          # 给候选池补货
pr_agent task handoff-done <task_id> --comment-url URL --pr-url URL   # assisted 人工执行后回填
pr_agent whitelist propose / approve / list                           # 治理链
```

密钥：`config/agents.yaml` 只引用环境变量（`${PR_AGENT_FRONTEND_KEY}` 等），token 绝不入库。GitHub 凭据走 `gh` 和操作系统凭据库。

## 测试

```bash
pytest -q    # 112 项，全离线：仿真池 / 仿真 adapter / 临时 git 仓库
```

覆盖：配置严格校验、额度窗口与冷却、分配与降级、prompt 拼装（canonical + 模式增量 + fork 策略）、handoff 交付包校验与回填幂等、dossier 渲染、交叉 review 并入幂等、竞态 PR 监控（查询失败记 `UNKNOWN`）、开工前刷新、锁（重入/死进程回收/并发槽）、worktree 与 diff、三作业 dry-run、断路器、计划任务包装脚本、前端对话注入实时状态事实。

## 命令面

```text
pr_agent status | doctor [--json] | assets
pr_agent config wizard | show
pr_agent pick OWNER/REPO ISSUE [--mode assisted|full_autonomy] [--agent NAME]
                            [--allow-unlisted] [--dry-run] [--no-run] [--json]
pr_agent run <task_id> [--dry-run]
pr_agent cancel <task_id> --reason "..."
pr_agent task show|handoff-done|cancel ...
pr_agent job pool-maint|run-day|monitor [--dry-run] [--json]
pr_agent daemon [--once] [--max-hours N] [--dry-run]
pr_agent install-schedule [--confirm] | uninstall-schedule [--confirm]
pr_agent whitelist propose|approve|list
pr_agent repo add OWNER/REPO
pr_agent chat [MESSAGE]
```

退出码：`0` 正常/干跑 · `2` 排队/跳过 · `3` BLOCKED · `1` 参数/配置/环境错误。`--json` 下失败也会输出 `{"status":"blocked","error":...,"evidence":...}`。

## 已知限制（如实）

- 真实 coding agent 端到端与真实 GitHub 写操作尚未执行——测试套件用仿真 adapter 验证编排逻辑；真实 CLI 参数会随版本漂移。
- adapter 探测有超时保护且带缓存；`--version` 挂起的 CLI 会被记为不可用（fail-closed），不假设它能用。
- 定时任务尚未在真实机器注册（`schtasks --confirm` 是刻意保留的人工步骤）。
- 跨厂商交叉 review 已实现并有单测，但默认关闭。

## 相关文档

- `DEVELOPMENT_PLAN.md` —— 唯一需求来源（§1–§17 完整设计）
- `docs/plan-coverage.md` —— 逐节对照计划的覆盖矩阵（✅/🟡/⚠️/❌ 与证据）
- `docs/changelog.md` —— 分期交付记录、计划歧义裁决、未验证项清单
