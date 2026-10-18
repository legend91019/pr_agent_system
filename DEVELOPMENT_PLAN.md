# pr_agent 开发计划（Development Plan）

> 版本：v1.0（2026-10-18）
> 状态：待执行
> 读者：负责实现的 coding agent（Claude Code / Codex / OpenCode / Gemini CLI 等）
> 约束：本文档是唯一需求来源；实现中的一切歧义以本文档与 `D:\Desktop\open_coding` 中权威工作流文件为准；两者冲突时先停下来在任务中记录问题，不要自行降级。

---

## 0. 一句话目标

在 `D:\Desktop\personal_agent` 实现一个**面向个人开发者的多 coding agent PR 调度系统（pr_agent）**：前端 CLI（可配置 LLM）负责对话与任务下发；调度器按额度、白/黑名单与权限模式，把"一个 Issue → 一个 PR"的任务派发给多家 coding agent（Claude Code、Codex、OpenCode、Gemini CLI、WorkBuddy）以 **headless 子进程**方式在**独立 git worktree** 中执行既定工作流；产出"一 Issue 一文档"的审查档案，最终由**人工终审**。

**已存在的资产不做重造**：候选池、状态机、准入审计、dossier 渲染全部复用 `D:\Desktop\open_coding`（见 §2.3 资产清单）。pr_agent 是编排层，不是新工作流。

## 1. 背景与核心设计决策

以下决策已经评审确定，实现时**不要**重新发明：

1. **不用 GUI/Computer Use 驱动各家 agent**。主流 coding CLI 均支持 headless 非交互执行与结构化输出（§8）。GUI 自动化只允许出现在对纯 GUI agent（如 Antigravity）的特殊适配中，且不在本计划范围内。
2. **不用 OpenClaw 做编排**。OpenClaw（本地 `D:\Desktop\open_coding\openclaw-fix` 有源码 checkout）定位为**未来的消息入口**（Telegram/WhatsApp 等渠道接入前端），不是执行层，也不是调度器。P4 之前不需要它。
3. **fail-closed / 证据优先 / 人工终审**三大原则全部从 open_coding 工作流继承：查询失败只能得到 `UNKNOWN`，不得解释为"没有冲突"；agent 不得自动合并、自批准、隐藏失败证据；合并永远由人工决定。
4. **权限分两档**（本次新增功能 1，见 §6）：
   - `full_autonomy`（完全权限模式）：GitHub token 与权限在配置层预先配好；agent 直接在 GitHub 留言、认领、推分支、创建 PR；创建后仍停在 `PENDING_HUMAN`。
   - `assisted`（默认权限模式）：agent **不做任何 GitHub 写操作**；中间层产出简短报告 + 草稿 PR + 草稿留言，交给前端呈现，由人工去 GitHub 执行。
   - 两种模式**都必须**生成一 Issue 一文档的审查档案供人工审查。
5. **定时任务落两处**（本次新增功能 2，见 §7）：维护 issue 池子（补货/保鲜）；正式做 PR（生产）。另有 PR 后监控。三个作业均为幂等命令，调度用 Windows 任务计划程序或内置守护进程。

## 2. 总体架构

### 2.1 分层

```text
你（人工终审）
  ↕ 对话 / 下发 / 审批白名单提案 / 执行 assisted 草稿
前端 CLI（可配置 LLM）：pr_agent chat · 任务下发 · 提案审批
  ↓
调度器 pr_agent core
  · 权限模式（full_autonomy / assisted）→ 决定 prompt 增量与 GitHub 门禁
  · 额度感知分配（quota 记账 + fallback 链）
  · 白/黑名单（config/projects.yaml）+ 硬门槛
  · 状态机（复用 open_coding/scripts/pool.py）
  · 定时作业（pool-maint / run-day / monitor）
  ↓ headless 适配层（统一 spawn · 事件流 · 结果回收）
Claude Code · Codex · OpenCode · Gemini CLI · WorkBuddy
  ↓ 每任务：独立 git worktree + canonical prompt + dossier
GitHub（full_autonomy 经 gh CLI；assisted 由人工执行草稿）
  ↓
PENDING_HUMAN：pr_reviews 一 Issue 一文档 → 人工批准合并
```

### 2.2 本仓库目录规划（实现产物落位）

```text
D:\Desktop\personal_agent\
  DEVELOPMENT_PLAN.md        # 本文档
  pr_agent\                  # Python 包（实现，见 §9）
  config\
    agents.yaml              # 模式、前端 LLM、各 agent 额度/模型/开关
    projects.yaml            # 白名单/黑名单/原则权重（允许人工与前端 agent 双写，见 §10）
  tasks\<task_id>\           # 每任务一个目录（见 §6.3）
  logs\                      # 调度器自身日志（与 open_coding 事件日志分工见 §11）
  tests\                     # pytest
  .workbuddy\                # 会话记忆（已存在，勿动）
```

**写入边界（必须遵守）**：pr_agent 只写 `personal_agent\` 上述目录，以及 open_coding 中明确属于"池与产物"的目录（`pool\*.jsonl`、`events.jsonl`、`deliverables\`、`pr_reviews\`、`repos\` 下的 worktree、`worktrees\`）。open_coding 的 `workflow\`、`docs\`、`ISSUE_SELECTION_RULES.md`、`GITHUB_TOKEN_SETUP.md`、`scripts\` 源码为**只读权威**（`pool.py` 作为子进程被调用，由它自己写它自己的文件）。

### 2.3 复用资产清单（open_coding，只读引用）

| 资产 | 路径 | 用法 |
|---|---|---|
| Canonical prompt | `open_coding\workflow\prompt.md` | 任务 prompt 基座；按模式打增量（§6.4） |
| 状态机与迁移 | `open_coding\scripts\pool.py`（`refresh/transition/plan-week/show/health`） | 以子进程调用；池与事件真相在 `pool\issues.jsonl`、`pool\events.jsonl` |
| 准入审计 | `ISSUE_SELECTION_RULES.md`、`scripts\issue_audit.py` | 规则只读；审计经 refresh 落池 |
| dossier 渲染 | `scripts\render_pr_review.py` | 生成 `pr_reviews\<owner>__<repo>__<issue>.md` |
| token 方案 | `GITHUB_TOKEN_SETUP.md`、`scripts\store_github_token.ps1`、`install_stored_github_token.ps1` | full_autonomy 前置条件；DPAPI 加密 + gh 凭据库 |
| 周度运维契约 | `workflow\operator-guide.md`、`workflow\automation.md` | 定时作业对齐此契约（§7） |
| 工作流设计 spec | `docs\superpowers\specs\2026-10-02-agent-pr-workflow-design.md` | 状态机/门禁语义权威 |

## 3. 状态机（复用 + 双模式差异）

基础状态机（open_coding 已实现，勿改）：

```text
DISCOVERED → VERIFIED | REJECTED | UNKNOWN
VERIFIED  → RESERVED | STALE
RESERVED  → CODING | BLOCKED
CODING    → REVIEW | BLOCKED
REVIEW    → CODING | PR_READY
PR_READY  → PR_OPEN | CODING
PR_OPEN   → PENDING_HUMAN → MERGED | CHANGES_REQUESTED | CLOSED | STALE
```

双模式差异（pr_agent 新增语义，落在本仓库 `tasks\` 与前端，不改 pool.py 语义）：

| 阶段 | full_autonomy | assisted |
|---|---|---|
| 认领（RESERVED） | agent 经 `gh` 留言/认领（带 marker 防重） | 产出 `handoff\comment.md` 草稿留言；人工代发后回填 URL |
| 交付（PR_READY→PR_OPEN） | agent 推分支并用 `gh pr create` | 产出 `handoff\`（报告+PR 草稿+命令清单）；人工执行并回填 PR URL |
| 终审（PENDING_HUMAN） | 相同：dossier + PR URL + CI + 未验证项，人工决定合并 | 相同 |

assisted 下任务目录持有 `handoff_state: pending_handoff | executed | canceled`；人工执行动作经前端命令（§9.2 `task handoff-done`）回填证据后，才由调度器把池记录转 `PR_OPEN`/`PENDING_HUMAN`。

## 4. 配置层（config\agents.yaml）

示例（实现时按此 schema 做加载 + 严格校验 + 报错不猜）：

```yaml
mode: assisted              # assisted | full_autonomy；可被任务级覆盖
max_parallel: 2             # 全局同时执行任务上限（P1 固定小值）
fallback_chain: [claude-code, gemini, codex, opencode]

frontend:                   # 前端对话 LLM（可配置；不参与关键路径）
  provider: openai-compatible
  base_url: ${PR_AGENT_FRONTEND_BASE_URL}
  model: ${PR_AGENT_FRONTEND_MODEL}
  api_key_env: PR_AGENT_FRONTEND_KEY   # 只从环境变量读，绝不入库

agents:
  claude-code:
    enabled: true
    adapter: claude_code
    quota: {budget_type: window, window_hours: 5, max_tasks_per_window: 10, cooldown_minutes: 30}
    models: {default: sonnet, large: opus}   # 大改动任务用 large
  gemini:
    enabled: true
    adapter: gemini
    quota: {budget_type: daily, max_tasks_per_day: 20}
    models: {default: gemini-2.5-pro}
  codex:
    enabled: false          # 未安装；安装后置 true
    adapter: codex
    quota: {budget_type: weekly, max_tasks_per_week: 30}
  opencode:
    enabled: false
    adapter: opencode
  workbuddy:
    enabled: false          # P4
    adapter: workbuddy      # 接口待定（§8.6）
```

要点：

- 额度记账以**任务数**为主（各订阅窗口规则差异大，任务数是唯一可本地可靠计数的量）；P2 起可加可选的 CLI 用量采样校准。
- `enabled: false` 的 agent 不参与分配；`fallback_chain` 中出现的 agent 必须 `enabled: true`（启动时校验）。
- 所有键引用环境变量（`${VAR}`）在加载期展开；密钥/令牌一律不入库、不入日志、不入事件。

## 5. 项目层（config\projects.yaml）

```yaml
whitelist:                  # 允许主动深耕
  - repo: Tencent/BrowserSkill
    added_by: human         # human | agent（agent 提案需 approved: true 才生效）
    approved: true
blacklist:                  # 显式永不投入
  - repo: someone/noisy-repo
    reason: 维护者多次无理由关 PR
principles:                 # 硬门槛（不做加权评分）
  min_stars: 150
  max_recent_competing_prs: 3      # 同 Issue 语义竞态 PR 数上限（超限拒绝，硬门禁）
```

> **用户裁决（2026-10-18）**：原计划的三维加权评分（`weights` / `require_backing` / `max_days_to_first_review`）**下线**——选题优先级交给 agent/人工判断（run-day 按 plan-week 顺序取第一个合规候选，人工可经 `pick` 显式指定）。这三个键已从 schema 删除；旧配置里若仍存在，加载时忽略并在 `config_warnings` 告警。

治理规则（§10）：白名单三来源（人工手写 / 前端 agent 提案 / 后端 agent 发现上报），agent 写入的条目**默认不生效**，经前端呈现、人工批准（`approved: true` 由人工动作触发）后才参与分配。

## 6. 权限模式（功能 1，详细设计）

### 6.1 full_autonomy（完全权限模式）

- **前置**：按 `open_coding\GITHUB_TOKEN_SETUP.md` 完成 token 安装（fine-grained PAT，最小仓库集，`Contents/PR/Issues: Read and write`；DPAPI 加密 + `gh` 凭据库；不落明文）。配置层增加自检：任务启动前跑 `gh auth status`，失败 → 任务 `BLOCKED`，提示人工执行安装脚本，**绝不**把失败当作无权限静默跳过。
- **中间层行为**：与 canonical prompt 的授权范围一致——允许留言/认领/推分支/建 PR；创建后转 `PENDING_HUMAN` 并停止一切写动作；不得合并、自批准、关 Issue、删他人分支、隐藏失败。
- **prompt 增量**：`mode_delta(full_autonomy)` = §6.4-A。

### 6.2 assisted（默认权限模式）

- **中间层行为**：**零 GitHub 写操作**（不留言、不认领、不 push、不建 PR；`gh` 只读查询仍允许）。到达交付点后产出 `handoff` 包并停：
- **prompt 增量**：`mode_delta(assisted)` = §6.4-B，**替换** canonical prompt 的第 2 节与第 6 节。

### 6.3 handoff 包（assisted 交付物）

`tasks\<task_id>\handoff\`：

| 文件 | 内容 |
|---|---|
| `report.md` | 简短中文报告：选了什么、为什么、改动摘要、验证结果、风险 |
| `comment.md` | 草稿留言（含防重 marker `<!-- agent-pr-workflow: OWNER/REPO#N -->`），供人工复制到 Issue 下 |
| `pr_body.md` | 草稿 PR 描述（链接 `Fixes #N`、修改文件、验证命令、dossier 链接、`Generated-by` 披露） |
| `commands.md` | 人工一键执行清单：按序给出可复制粘贴的 `git push` / `gh pr create` 命令（含分支、base、head、body 文件路径） |
| `review_findings.md` | 交叉 review 发现（若已执行 §12） |

前端呈现 handoff 后置 `pending_handoff`；人工执行完用 `pr_agent task handoff-done <task_id>` 回填留言/PR URL，调度器据此转池状态。人工放弃 → `canceled`（记录原因，池记录回 `STALE`）。

### 6.4 prompt 组装（唯一拼装方式，不得自创）

```text
final_prompt = workflow\prompt.md
             + "\n\n## 权限模式契约\n" + mode_delta(mode)
```

- **A（full_autonomy）**：`本任务运行于 full_autonomy：允许你在 GitHub 上留言、申请/接受认领、推送分支、创建 PR（使用已登录的 gh CLI，不得读写任何 token 文件）。创建 PR 后必须停在 PENDING_HUMAN，由人工完成最终审查；不得自动合并、批准自己的 PR、关闭 Issue 或隐藏失败证据。`
- **B（assisted）**：`本任务运行于 assisted：你不得进行任何 GitHub 写操作（不留言、不认领、不 push、不创建 PR；gh 仅限只读查询）。完成实现与验证后，产出交付包并停止：简短中文报告 report.md、草稿留言 comment.md、草稿 PR 描述 pr_body.md、人工命令清单 commands.md，写入指定 handoff 目录。GitHub 上的动作全部由人工执行。`

### 6.5 共同点

两模式都必须：生成 `pr_reviews\<owner>__<repo>__<issue>.md`（经 `render_pr_review.py` + 补全）；保存全部验证原始输出；事件写 `events.jsonl`；停在人工终审。

## 7. 定时任务（功能 2，详细设计）

三个**幂等作业命令**（实现为 CLI 子命令，可被任何调度器调用；调度器只负责按时拉起）：

| 作业 | 命令 | 频率 | 语义 |
|---|---|---|---|
| 池子维护 | `pr_agent job pool-maint` | 每 6 小时 | refresh `DISCOVERED/STALE/UNKNOWN` 直到 ≥6 个新鲜 `VERIFIED`、覆盖 ≥3 仓、单仓 ≤2 新 VERIFIED；数量不足如实报告，**不降门禁凑数** |
| PR 生产 | `pr_agent job run-day` | 周一至五 09:05（Asia/Shanghai） | `pool.py plan-week --target 3 --reserve 6` → 取今日至多 1 个任务执行（受 `max_parallel`、周 cap 3、断路器约束）；计划不完整 → 记录并跳过 |
| PR 监控 | `pr_agent job monitor` | 每 6 小时 | 对 `PR_OPEN/PENDING_HUMAN/CHANGES_REQUESTED` 记录 CI、维护者评论、竞态 PR 为事件；**绝不**合并/批准/回复 |

实现要求：

1. 每个作业：开始先刷新再决策；12 小时 freshness 窗口；两候选连续验证失败 → 停止当日生产（断路器，继承 operator-guide）。
2. `pr_agent job run-day` 内部完成"打包 → 分配（§9.3）→ spawn → 事件流 → dossier/handoff"全链。
3. 调度承载二选一（都实现，P2 交付守护、P3 交付注册）：
   - `pr_agent daemon`：单进程 sleep 循环，内部按时拉起三作业（日志落 `logs\daemon.log`）；
   - `pr_agent install-schedule`：用 `schtasks` 注册 Windows 任务计划（每作业一条，`--uninstall` 移除）。文档写明：当前 open_coding `execution_mode: manual_only`，启用定时前须人工确认放开。
4. 所有作业可 `--dry-run`：打印将做之事，不执行。

## 8. headless 适配层规范

统一协议（`pr_agent\adapters\base.py`）：

```python
class Adapter(Protocol):
    name: str
    def available(self) -> bool: ...            # 探测 CLI 在 PATH 且可鉴权
    def spawn(self, prompt: str, cwd: str, model: str | None,
             full_autonomy: bool, max_turns: int = 200) -> Iterator[Event]: ...
# Event = {ts, task_id, adapter, type, payload}
# type ∈ start|tool_use|file_edit|command|assistant_message|usage|result|exit
```

各 adapter（P1 仅 claude_code；P2 其余）：

| adapter | 命令（cwd=worktree） | 备注 |
|---|---|---|
| `claude_code` | `claude -p "<prompt>" --output-format stream-json --verbose --model <model> [--resume <sid>]` | 权限：full → `--permission-mode bypassPermissions`；assisted → `--permission-mode acceptEdits`（反正无 gh 写）。必带 `--max-turns` |
| `gemini` | `gemini -m <model> -p "<prompt>"`（JSON 输出开关按本机版本探测） | P2 |
| `codex` | `codex exec --json [--full-auto|--sandbox workspace-write] "<prompt>"` | 安装后 P2 |
| `opencode` | `opencode run -m <model> "<prompt>"`；可选 `opencode serve` HTTP | P2 |
| `workbuddy` | 接口待定（本产品 CLI/会话拉起）；先留桩 | P4 |

硬规则：

- 事件流统一写 `tasks\<task_id>\events.jsonl`；**摘要级**事件（start/transition/result）同步写 `open_coding\pool\events.jsonl`。
- exit code ≠ 0 或超时 → 任务 `BLOCKED`，原文落盘，**禁止**总结成"失败但有价值"。
- adapter **永不**读取、传递、记录任何 token；GitHub 操作只经 `gh`。
- 每 adapter 每 worktree 一把锁；全局并发 ≤ `max_parallel`。
- prompt 经 **stdin 或临时文件**传入（Windows 命令行长度限制），临时文件用后即删。

## 9. 调度器与前端 CLI

### 9.1 命令面（Typer，`pr_agent`）

```text
pr_agent status                     # 池子/额度/任务/模式 一屏
pr_agent config wizard|show         # 交互式配置面板 / 查看生效配置与缺口
pr_agent pick [--repo R --issue N]  # 人工选题 → 打包执行
pr_agent run <task_id> / cancel <task_id>
pr_agent task handoff-done <task_id> [--comment-url U] [--pr-url U]
pr_agent job pool-maint|run-day|monitor [--dry-run]
pr_agent daemon | install-schedule | uninstall-schedule
pr_agent whitelist propose <repo>   # 前端 agent 也可写提案（人工批准生效）
pr_agent chat                       # 可选：前端 LLM 对话（读配置 §4 frontend）
```

### 9.2 任务打包（runner）

`task_id = <owner>__<repo>__<issue>`；`tasks\<task_id>\`：`task.json`（id、mode、agent、worktree 路径、base SHA、池记录指针）、`events.jsonl`、`prompt.md`（拼装结果，存档）、`handoff\`（assisted）。worktree 建于 `open_coding\repos\<repo>`（`git worktree add ..\..\worktrees\<task_id> -b fix/issue-N-<slug> <base>`，以本机现状为准，若已有 worktrees 布局则沿用）。

### 9.3 分配算法（P1 规则版，勿上 ML）

```
score(agent, task) = enabled && available && quota_left && model_fit(规模→default/large)
选择 = fallback_chain 中首个 score>0；全满 → 入队等待（pool-maint 补货不占生产名额）
```

- 额度记账：`usage`（任务窗口计数 + 冷却）；P2 可选采样 CLI 用量校准。
- 任务规模：读池记录 `scope_metrics`（行数）；>400 行用 `large` 模型（若配置）。
- 分配决策连同理由写事件（可审计）。

## 10. 白/黑名单治理（功能联动）

- 分配前过滤：repo ∈ blacklist → 拒；repo ∈ approved whitelist → 通过；其余走 `principles` 硬门槛（`min_stars`、竞态 PR 上限）。不做加权评分（用户裁决 2026-10-18）；多个合规候选时按 plan-week 顺序取第一个，选题优先级交给 agent/人工判断。
- 前端 agent 调 `whitelist propose`：写入 `added_by: agent, approved: false`；前端呈现给人工；人工批准（前端命令或直接改 YAML）→ 生效。
- 后端 agent 在执行中发现潜力项目（如 review 中见到的仓库）：写入 `tasks\...\discoveries\`，由池子维护作业汇总上报前端，同一审批链。
- 所有提案/批准动作写事件日志。

## 11. 审计与日志分工

| 位置 | 内容 |
|---|---|
| `open_coding\pool\events.jsonl` | 池状态转换与外部动作（经 pool.py / 调度器摘要事件）——**唯一状态真相** |
| `tasks\<id>\events.jsonl` | 该任务完整 agent 事件流（审计用） |
| `logs\` | 调度器/守护自身运行日志 |

原则：状态只读池子；任务目录可重建；两处都不含密钥。

## 12. 交叉 review（P3）

- `REVIEW` 阶段第二意见由**另一家厂商**模型执行（如 Claude 做的 → Gemini review；规则表配置于 `agents.yaml` 的 `cross_review`，缺省按 fallback 链取下一家）。
- 发现 Blocker/Major → 回 `CODING`（同厂商或换回原厂商，配置定）；Minor/Nit 写入 dossier。
- 发现并入 `pr_reviews\<...>.md` 与 handoff `review_findings.md`。

## 13. 安全与合规（硬约束）

1. token：只走 `GITHUB_TOKEN_SETUP.md` 流程（DPAPI + gh 凭据库）；禁明文、禁聊天、禁日志、禁 env dump。
2. 最小权限：fine-grained PAT、最小仓库集；禁 account/admin/secrets 权限。
3. 分支策略：fork + upstream PR；不动 main/master。
4. 外部动作四许可：留言/认领/推分支/建 PR；三禁止：合并/自批准/关 Issue；隐藏失败 = 最高违规。
5. AI 贡献按目标仓库规则披露（`Generated-by` trailer 等）。
6. 本仓库不做 Key 管理 UI；密钥只经人工执行脚本。

## 14. 里程碑（顺序执行，每期有验收）

**P1 单 agent 全链闭环（先 claude-code）**
- 范围：`config`（agents/projects）、runner（打包/worktree/prompt 拼装 §6.4）、adapter `claude_code`、双模式、dossier 复用、handoff、`status/pick/task handoff-done`、`--dry-run`。
- 验收（在自有测试仓库演示，两模式各 ≥1 次）：
  - assisted：agent 全程零 GitHub 写；handoff 四件套齐全；人工执行后回填转 `PR_OPEN`。
  - full_autonomy：`gh auth status` 自检；agent 建分支+PR；自动停 `PENDING_HUMAN`。
  - 两模式 dossier 均生成且含真实 diff 与原始输出；查询失败处均 `UNKNOWN`/`BLOCKED`。
  - pytest 全绿；`--dry-run` 不产生外部动作。

**P2 多 agent + 额度 + 定时**
- 范围：gemini/codex/opencode adapter、`usage` 记账、fallback 链、锁与并发、三作业命令、`daemon`、`install-schedule`。
- 验收：两家并行两 worktree 互不干扰；额度满正确降级到 fallback；三作业幂等（重跑不重复留言/认领）；断路器（reserve 不足→停产）生效。

**P3 治理 + 交叉 review**
- 范围：whitelist 提案链、`discoveries` 上报、cross_review。
- 验收：未批准提案绝不参与分配；交叉 review 发现的 Blocker 能打回；dossier 含两厂商意见。

**P4 入口与扩展（可选）**
- OpenClaw 消息入口接前端；WorkBuddy adapter；TUI 面板；Computer-Use 特殊适配（纯 GUI agent 专用，独立开关）。

每期 Definition of Done：验收项全过 + pytest 全绿 + 本文档 `docs\changelog` 段落更新 + 更新 `tasks\` 无遗留垃圾。

## 15. 非目标（明确不做）

- 不做 GUI 自动化驱动 CLI agent（§1.1）；不做浏览器操控（现有 dogfood 例外不迁移）。
- 不做 Key/密码管理 UI；不做多用户/SaaS；不做自动合并。
- 不重写工作流规则/审计器/渲染器（只调用）。
- 不做 ML 调度（规则打分足够）。

## 16. 风险与对策

| 风险 | 对策 |
|---|---|
| CLI 参数随版本漂移 | adapter 启动自检（`--version`/help 探测），失败→`enabled:false` 并报告，不猜 |
| Windows 命令行长度/编码 | prompt 走 stdin/临时文件；UTF-8 显式 |
| headless 卡死 | 每 spawn 硬超时 + 心跳；超时→`BLOCKED` 落盘现场 |
| 额度估算失真 | 以任务数为主；采样仅校准；额度宁可少给不多给 |
| 两个 agent 撞同仓 | 每 worktree 锁 + 任务独占 repo 分支命名（task_id 唯一） |
| open_coding 布局变化 | 只依赖 §2.3 表列路径；启动自检缺失即报 `BLOCKED` |

## 17. 实现约定

- Python ≥3.11，包 `pr_agent`（Typer + Pydantic + stdlib 为主；依赖最小化）。
- 所有输出中文（代码/命令/路径/专有名词保留原文）；所有命令幂等；所有失败带原始输出。
- 测试：pytest 单测（配置校验/分配/额度/handoff 拼装）+ `--dry-run` 冒烟。
- 提交信息 Conventional Commits；不提交任何密钥/池子数据（`.gitignore`：`tasks\`、`logs\`、`config\agents.yaml` 含环境变量除外——见 .gitignore 注记）。

---

## 附：执行顺序建议（给实现 agent）

1. 读 §2.3 全部资产 → 2. 搭 P1 骨架（config + runner + claude_code adapter + 双模式）→ 3. 自有仓库两模式演示 → 4. P1 验收 → 5. P2 → 6. P3。每步先读本文档对应节，不明确处停下发问，不自行降级。
