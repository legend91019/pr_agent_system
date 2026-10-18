# pr_agent

面向个人开发者的**多 coding agent PR 调度系统**：前端 CLI 负责对话与选题，调度器按额度、白/黑名单与权限模式，把一个 Issue → 一个 PR 的任务派给多家 coding agent，以 **headless 子进程**在**独立 git worktree** 中执行既定的 open_coding 工作流，产出**一 Issue 一文档**的审查档案，最终由**人工终审**。

本仓库是**编排层**，不是新工作流：候选池、状态机、准入审计、dossier 渲染全部复用 `D:\Desktop\open_coding`（只读权威资产），pr_agent 只调用、不重写。

---

## 1. 它做什么、不做什么

| 做 | 不做 |
|---|---|
| 调 `pool.py` 迁移状态、调 `render_pr_review.py` 渲染 dossier | 重写状态机 / 审计规则 / 渲染器 |
| 按模式给 canonical prompt 打增量，spawn `claude/gemini/codex/opencode` headless | GUI 自动化、浏览器操控驱动 agent |
| assisted 产出交付包（报告 + 草稿留言 + 草稿 PR + 命令清单） | 代替人工在 GitHub 上合并 / 自批准 / 关 Issue |
| full_autonomy 经 `gh` 留言、推分支、建 PR 后停 `PENDING_HUMAN` | 自动合并、自动批准、隐藏失败证据 |
| 定时维护池子 / 生产 PR / 只读监控 PR | 管理 token（只走 open_coding 的 DPAPI + gh 凭据库） |

三大原则贯穿实现：**fail-closed**（读不到就是 `UNKNOWN`/`BLOCKED`，不解释成"没问题"）、**证据优先**（原始输出落盘，禁止总结成"失败但有价值"）、**人工终审**（合并永远由人决定）。

## 2. 目录

```text
personal_agent\
  pr_agent\            # 调度器包（Typer + Pydantic + stdlib）
    adapters\          # claude_code / gemini / codex / opencode / workbuddy
    runner.py          # 打包 → 分配 → spawn → 事件流 → dossier/handoff → 池迁移
    jobs.py daemon.py  # 三作业 + 内置守护
    governance.py      # 白/黑名单、提案、discoveries 汇总
    cross_review.py    # 跨厂商第二意见（默认关闭）
  config\
    agents.yaml        # 模式、额度、模型、fallback 链、cross_review
    projects.yaml      # 白/黑名单、原则评分
  tasks\<task_id>\     # 每任务：task.json / prompt.md / events.jsonl / diff.patch / handoff\
  logs\                # 调度器与守护日志
  tests\               # pytest（离线、仿真池 + 仿真 adapter）
  scripts\job-*.cmd    # install-schedule 生成的任务计划包装脚本
```

写入边界：pr_agent 只写本仓库 `tasks\`、`logs\`、`config\`、`scripts\`，以及 open_coding 的 `pool\*.jsonl`（经 `pool.py`）、`pr_reviews\`、`worktrees\`、`deliverables\`。open_coding 的 `workflow\`、`scripts\`、`docs\` 与两份说明文档为只读。

## 3. 安装与自检

```bash
cd D:\Desktop\personal_agent
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

pr_agent doctor      # 环境 / 资产 / 配置 / adapter / gh 一次看清
pr_agent status      # 池子分布、额度、任务、并发槽、断路器
pr_agent assets      # 复用资产存在性
```

`doctor` 是只读的：不写池、不 spawn、不改配置。任一关键资产缺失会直接报出并拒绝开工（§16）。

## 4. 命令面

```text
pr_agent status
pr_agent doctor [--json]
pr_agent assets

pr_agent pick OWNER/REPO ISSUE [--mode assisted|full_autonomy] [--agent NAME]
                             [--allow-unlisted] [--dry-run] [--no-run] [--json]
pr_agent run <task_id> [--dry-run]
pr_agent cancel <task_id> --reason "..."          # 尽力终止子进程；池记录 → BLOCKED

pr_agent task handoff-done <task_id> --comment-url URL --pr-url URL
pr_agent task cancel <task_id> --reason "..."
pr_agent task show <task_id>

pr_agent job pool-maint|run-day|monitor [--dry-run] [--json]
pr_agent daemon [--once] [--max-hours N] [--dry-run]
pr_agent install-schedule [--confirm]             # 默认只打印 schtasks 命令
pr_agent uninstall-schedule [--confirm]

pr_agent whitelist propose OWNER/REPO [--reason R]  # 写入 approved:false
pr_agent whitelist approve OWNER/REPO              # 人工批准后才参与分配
pr_agent whitelist list

pr_agent repo add OWNER/REPO                       # gh clone 到 open_coding\repos\
pr_agent chat [MESSAGE]                            # 可选前端 LLM（读 config frontend）
```

退出码：`0` 正常/干跑，`2` 排队或跳过（如 plan-week 不完整），`3` 阻断（BLOCKED），`1` 参数/配置/环境错误。`--json` 下失败也会输出 `{"status":"blocked","error":...,"evidence":...}`。

## 5. 两种权限模式

| 阶段 | `full_autonomy` | `assisted`（默认） |
|---|---|---|
| GitHub 写操作 | 允许留言/认领/推分支/建 PR（经 `gh`，不碰 token 文件） | **零写操作**（`gh` 仅只读） |
| 交付物 | 分支 + PR，然后停 | `handoff\`：`report.md` `comment.md` `pr_body.md` `commands.md`（+ 可选 `review_findings.md`） |
| 收尾 | 自动转 `PR_OPEN → PENDING_HUMAN` | 人工执行后 `task handoff-done` 回填 URL 才转 |

两模式都**必须**生成 `pr_reviews\<owner>__<repo>__<issue>.md`，并由调度器追加"调度器补充证据"（diff 摘要、原始验证输出、未验证项）。`full_autonomy` 开工前先跑 `gh auth status`，未登录直接 `BLOCKED` 并提示执行 open_coding 的 token 安装脚本——绝不把无权限当成"跳过"。

## 6. 开工门禁（fail-closed）

`pick` / `run-day` 在分配前逐条校验，任一不满足即拒绝：

1. 池记录状态 = `VERIFIED`（不是 `DISCOVERED`/`STALE`）；
2. `scope_gate.result == PASS`（缺 `scope_metrics` 就 `MISSING`，需 `pool.py annotate` 后重审）；
3. 记录未过期（`workflow/config.yaml` 的 `freshness_hours`，默认 12h）；
4. 仓库过白名单（或 `--allow-unlisted` 且落事件）/ 不在黑名单 / 满足 `min_stars` 硬门槛；
5. agent `enabled` 且探测可用（`--version` 自检）、额度未耗尽、冷却已过；
6. 并发槽未满（`max_parallel`），adapter 与 worktree 各持一把锁。

## 7. 定时作业

| 作业 | 建议频率 | 语义 |
|---|---|---|
| `job pool-maint` | 每 6 小时 | 刷新 `DISCOVERED/STALE/UNKNOWN`，凑够 6 个新鲜 `VERIFIED`（≥3 仓、单仓 ≤2）；不足如实报告，不降门禁凑数 |
| `job run-day` | 周一至五 09:05 | `plan-week --target 3 --reserve 6`，计划不完整即跳过；正常时至多执行 1 个任务 |
| `job monitor` | 每 6 小时 | 只读记录 CI / 维护者评论 / 竞态 PR，**绝不**合并、批准、回复 |

两个承载方式都实现了：`pr_agent daemon`（单进程 sleep 循环，日志 `logs\daemon.log`）与 `pr_agent install-schedule`（为每作业生成 `scripts\job-*.cmd` 再用 `schtasks` 注册；默认只打印命令，`--confirm` 才落盘）。

> 当前 open_coding `workflow/config.yaml` 是 `execution_mode: manual_only`。启用定时生产前必须人工确认放开，否则定时作业只做只读维护与监控。

断路器：`pool-maint` 连续两次候选验证失败 → 当日停止生产（状态落 `logs\`，`status` 可见）。

## 8. 治理与交叉 review

- 白名单三来源：人工手写 / 前端 agent 提案（`approved:false`）/ 后端 agent 上报（`tasks\...\discoveries\`，由 `pool-maint` 汇总）。
- **未批准提案绝不参与分配**（`whitelist approve` 或人工改 YAML 才生效），所有提案/批准动作写 `logs\governance.jsonl`。
- 交叉 review（`agents.yaml` 的 `cross_review.enabled`，默认 `false`）：由**另一家厂商**模型对 diff 出第二意见；Blocker/Major 打回 `CODING`，Minor/Nit 写进 dossier 与 `handoff\review_findings.md`。

## 9. 实测与验收

```bash
pr_agent doctor                                   # 资产 OK、claude 2.1.x 可用、gemini 不可用（如实报告）
pr_agent status                                   # 68 条池记录：VERIFIED=5，其余分布可见
pr_agent pick Tencent/BrowserSkill 392 --dry-run   # → BLOCKED：scope_gate=MISSING（拒绝开工）
pr_agent job run-day --dry-run                     # → skipped：plan-week 0/3 且 reserve 0/6
pr_agent job pool-maint --dry-run                  # → 打印 8 条待 refresh 命令，不执行
pr_agent job monitor --dry-run                     # → 打印 7 条只读 gh 命令，不执行
pr_agent install-schedule                          # → 打印 3 条 schtasks + 包装脚本内容
pr_agent daemon --once --dry-run                   # → 三作业顺序跑完，日志落 logs\daemon.log
```

当前真实池子的 5 条 `VERIFIED` 全部被 scope gate 排除（变更规模不达标），因此 `run-day` 正确地拒绝生产——这是 fail-closed 的预期行为，不是故障。要开工需先用 open_coding 的 `pool.py annotate` 补齐 `scope_metrics` 并重新审计，或等 `pool-maint` 找到合规候选。

## 10. 测试

```bash
pytest -q     # 101 项，全离线：仿真 pool.py / 仿真 adapter / 临时 git 仓库
```

覆盖：配置严格校验（含环境变量展开、空列表、未批准提案）、额度窗口/冷却、分配与降级、prompt 拼装（canonical + 模式增量）、handoff 四件套校验与回填幂等、dossier 渲染与调度器补证、锁（重入/陈旧回收/并发槽）、git worktree/diff、三作业 dry-run 与断路器、计划任务包装脚本、cross_review、前端一次性问答注入状态事实。

## 11. 已知限制（未验证项）

- **真实 agent 端到端**：pytest 用仿真 adapter 验证编排；真实 `claude` 与 `gemini/codex/opencode` 的 CLI 参数会随版本漂移，本轮只做到 `--version` 探测（claude 可用），未在真实仓库跑完整任务。首次真实运行建议 `pick ... --no-run` 看计划 → `run` → 全程盯 `tasks\<id>\events.jsonl`。
- **`gemini` 探测超时**：本机 `gemini --version` 15s 未返回，按 fail-closed 记为不可用；需人工确认安装/登录后再启用。
- **定时作业未真实注册**：仅验证了命令生成与 daemon 单轮；`schtasks --confirm` 与 `execution_mode=manual_only` 的放开留待人工决定。
- **`gh` 写操作未实测**：`full_autonomy` 的留言/推分支/建 PR 依赖 open_coding 的 token 安装，本轮未执行任何 GitHub 写操作。
- **交叉 review 默认关闭**：逻辑与打回路径已单测，但未接真实第二厂商模型跑过。

## 12. 相关文档

- `DEVELOPMENT_PLAN.md` —— 唯一需求来源（本实现的对照基准）
- `docs\changelog.md` —— 分期交付记录、计划歧义裁决、未验证项清单
- `D:\Desktop\open_coding\workflow\operator-guide.md` / `automation.md` —— 周度运维契约
