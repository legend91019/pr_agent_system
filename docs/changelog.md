# 交付记录（changelog）

对照 `DEVELOPMENT_PLAN.md` 的分期验收，记录每期落地内容、计划歧义裁决与未验证项。
约定：提交信息用 Conventional Commits；`tasks\`、`logs\` 不入库。

---

## P1 单 agent 全链闭环（claude-code 优先）

**落地**

- `pr_agent/paths.py`：open_coding 复用资产清单 + `missing_assets()` 自检；写入边界集中在 `Paths`。
- `pr_agent/config.py`：`agents.yaml` / `projects.yaml` 用 Pydantic `extra="forbid"` 严格校验；`${VAR}` 加载期展开；密钥只从环境变量读，永不入库/入日志。校验项含：fallback 链成员必须 `enabled`、`cross_review.reviewer` 必须已定义、权重和为 1、白/黑名单不得重叠或重复、前端缺变量只告警不影响调度。
- `pr_agent/poolctl.py`：以子进程调用 `pool.py`（`health/show/transition/plan-week/refresh`），失败带 stdout/stderr 原文；池是唯一状态真相。
- `pr_agent/gitsvc.py`：worktree 建于 `open_coding\worktrees\<task_id>`，分支 `fix/issue-<N>-<slug>`，base 优先用池记录 `base_sha`，本地不可解析时退回 `origin/<default>` 并把来源写进任务备注；`collect_diff` 产出 patch、改动文件/行数、committed 状态。
- `pr_agent/prompt.py`：唯一拼装方式 = `workflow\prompt.md` + `## 权限模式契约` + `mode_delta`（§6.4-A/B 原文用词），再追加任务上下文（issue/worktree/base/branch/handoff/dossier/验证目录）。
- `pr_agent/adapters/base.py` + `claude_code.py`：统一 Event 协议（`start/tool_use/file_edit/command/assistant_message/usage/result/exit`）；prompt 走 stdin（规避 Windows 命令行长度）；硬超时后先 terminate 再 kill，超时/非 0 一律 BLOCKED 并保留 stderr 尾部；`--permission-mode bypassPermissions|acceptEdits` 按模式切换，必带 `--max-turns`。
- `pr_agent/runner.py`：打包 → 门禁 → 分配 → spawn → 事件流落 `tasks\<id>\events.jsonl` → 摘要事件落 `open_coding\pool\events.jsonl` → dossier 骨架 + 调度器补证 → handoff 校验 → 池迁移。
- `pr_agent/handoff.py`：四件套缺一即 BLOCKED；`comment.md` 必须含防重 marker；`pr_body.md` 必须 `Fixes #N` 且披露 `Generated-by`；`commands.md` 必须含 push 与 `gh pr create`。`handoff-done` 校验 URL 后 `PR_READY → PR_OPEN → PENDING_HUMAN`，重复回填幂等。
- `pr_agent/dossier.py`：优先调 `render_pr_review.py`；渲染失败时退化为骨架（不静默通过），并把渲染错误写进 dossier。八节齐全才算完整，缺节只警告不假装完整。
- `pr_agent/cli.py`：`status/doctor/assets/pick/run/cancel/task/job/daemon/install-schedule/uninstall-schedule/whitelist/repo/chat`；`--dry-run` 全程零外部动作。
- `pr_agent/locks.py`：worktree / adapter / 并发槽三类锁；`O_CREAT|O_EXCL` 原子创建；持锁进程已死或锁龄超过阈值即回收。

**验收现状**：两模式（assisted / full_autonomy）由 `tests/test_runner_integration.py` 用仿真 adapter 全链跑通（handoff 四件套、dossier 含真实 diff 与补证、URL 回填转 `PENDING_HUMAN`、`gh` 未登录拒绝开工）。真实 GitHub 写操作未执行（见"未验证项"）。

---

## P2 多 agent + 额度 + 定时

**落地**

- `adapters/gemini.py`、`codex.py`、`opencode.py`：按 §8 的命令形态实现，参数差异按各 CLI 版本探测；探测失败如实报不可用，不猜参数。
- `adapters/workbuddy.py`：留桩（P4），探测/启动均返回明确"接口待定"，不假装可用。
- `pr_agent/quota.py`：以**任务数**记账（window/daily/weekly/none 四种），失败记 failed 并设冷却；额度宁可少给不多给。
- `pr_agent/allocate.py`：`enabled && available && quota_left && model_fit` 打分，取 fallback 链首个可用；>400 行用 `large` 模型；全满入队（退出码 2）；决策连同理由落事件。
- `pr_agent/jobs.py`：三作业幂等实现 + 断路器（连续两次验证失败停止当日生产）。
- `pr_agent/daemon.py`：单进程 sleep 循环，按时间表拉起作业，日志 `logs\daemon.log`，支持 `--once/--max-hours/--dry-run`。
- `pr_agent/schedule.py`：为每作业生成 `scripts\job-*.cmd` 包装脚本，再 `schtasks /Create` 注册；默认只打印，`--confirm` 才落盘。**不直接往 `/TR` 塞带嵌套引号的长命令**（Windows 上会被截断）。

**验收现状**：额度满降级、并发槽互斥、三作业 dry-run 幂等、断路器状态可由 `status` 观察，均有单测/冒烟。真实 `schtasks` 注册未执行。

---

## P3 治理 + 交叉 review

**落地**

- `pr_agent/governance.py`：黑名单拒绝 → 已批准白名单通过 → 其余走 `min_stars` 硬门槛（`enforce_min_stars`）；`propose` 写 `approved:false`，`approve` 由人工触发；所有动作落 `logs\governance.jsonl`。
- `discoveries`：后端 agent 在 `tasks\<id>\discoveries\` 上报，`pool-maint` 汇总进作业证据与前端可见输出，走同一审批链。
- `pr_agent/cross_review.py`：优先 `cross_review.reviewer`，否则取 fallback 链中第一家**不同厂商**且可用者；Blocker/Major → 回 `CODING` 并落证据，Minor/Nit 写 dossier 与 `handoff\review_findings.md`；默认 `enabled: false`（避免默认产生额外调用成本）。

**验收现状**：未批准提案不参与分配、Blocker 打回、dossier 可含两厂商意见，均有单测。

---

## 计划歧义裁决（未降级，仅记录）

| 计划原文 | 实际实现 | 理由 |
|---|---|---|
| §9.1 `pr_agent install-schedule` + `--uninstall` 移除 | 拆成 `install-schedule` / `uninstall-schedule` 两条命令 | Typer 子命令下布尔开关语义含混；拆开更符合"幂等 + 显式人工动作" |
| §9.1 `whitelist propose <repo>` | 增加 `whitelist approve` / `whitelist list` | 计划要求"人工批准才生效"，没有批准入口则该链无法闭合 |
| §9.1 `chat`（可选） | 支持 `chat [MESSAGE]` 一次性提问与交互模式 | 便于自动化验收；一次性模式把 `status` 真实数据注入 system，避免模型编造池子 |
| §6.3 handoff 五件套 | `report/comment/pr_body/commands` 四件**必需**，`review_findings.md` 可选 | 与 §6.3 表格一致：交叉 review 默认关闭时该文件不存在属正常，不应 BLOCKED |
| §7 `install-schedule` 直接注册 schtasks | 先生成 `scripts\job-*.cmd` 包装脚本再注册 | `/TR` 内嵌 `&&`、重定向与多层引号在 Windows 被截断，包装脚本是唯一稳定做法 |
| §3 assisted `canceled` → 池 `STALE` | `task cancel`（handoff 放弃）走 `PR_READY → BLOCKED → STALE`；`pr_agent cancel`（运行期取消）只到 `BLOCKED` | 状态机不允许 `PR_READY/CODING → STALE` 直连；两种取消语义不同（放弃交付 vs 中止执行），池记录保留原因供后续运维处理 |

## 未验证项（交付时明确未做）

1. 真实 coding agent 端到端任务（只用仿真 adapter 验证编排；真实 CLI 参数随版本漂移）。
2. `gemini --version` 本机 15s 超时 → 记为不可用；未尝试登录/修复。
3. `schtasks --confirm` 真实注册与 `execution_mode: manual_only` 放开。
4. 任何 GitHub 写操作（留言/认领/推分支/建 PR），以及 `full_autonomy` 的真实 PR 创建。
5. 交叉 review 接真实第二厂商模型跑完整任务。
6. 真实池子当前 5 条 `VERIFIED` 全部被 scope gate 排除（变更规模不达标），故 `run-day` 拒绝生产；需先 `pool.py annotate` 补 `scope_metrics` 或等新候选。
