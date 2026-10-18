# 计划覆盖核查（对 DEVELOPMENT_PLAN v1.0 逐节）

> 核查时间：2026-10-18（第二轮补缺后）
> 状态口径：
> - ✅ **已实现并离线验证**：代码在，pytest 覆盖或命令可复现（不代表已在真实仓库跑过）。
> - 🟡 **已实现，未真实验证**：逻辑与单测齐备，但依赖真实 agent / GitHub 写操作 / 计划任务，本轮未执行。
> - ⚠️ **偏差（有理由）**：做法与文字不同，理由写在 `docs/changelog.md` 的"计划歧义裁决"。
> - ❌ **未实现**。

| 计划节 | 内容 | 状态 | 证据 / 说明 |
|---|---|---|---|
| §2.2 | 目录落位（`pr_agent/`、`config/`、`tasks/`、`logs/`、`tests/`） | ✅ | `Paths` 统一定义；`status` 可显示实际路径 |
| §2.3 | 复用 canonical prompt / pool.py / 审计 / 渲染器 / token 方案 / 运维契约 | ✅ | `paths.REQUIRED_ASSETS` 11 项自检；`doctor` 报 `missing_assets: []` |
| §3 | 状态机经 `pool.py` 迁移，不重写 | ✅ | `poolctl.Pool.transition/plan_week/refresh`；`test_poolctl.py` |
| §3 | 双模式差异（认领/交付/终审） | ✅ | `runner` + `handoff`；`test_runner_integration.py` 两模式各 1 例 |
| §4 | `agents.yaml` 全字段 + 严格校验 + `${VAR}` 展开 | ✅ | `config.py` `extra="forbid"`；`test_config.py`（12 例） |
| §4 | `enabled:false` 不参与分配；fallback 成员必须 enabled | ✅ | `allocate.py` + 配置校验；`test_allocate.py` |
| §5 | 项目层 whitelist/blacklist/principles 全字段 | ✅ | `ProjectsConfig`；空列表（只有注释）也能解析 |
| §5 | `weights` 三维权衡评分 | ✅ | `governance.score_record`；`test_governance.py::test_score_record_*`（公式为解释性实现，见下） |
| §5 | `require_backing` / `max_days_to_first_review` 参与评分 | ❌ | 仅解析与校验；池记录没有"背书类型/首次 review 天数"字段，无证据可用，未编造数据 |
| §6.1 | full_autonomy：gh 自检失败即 BLOCKED | ✅ | `pick` 前置；`test_full_autonomy_requires_gh_auth` |
| §6.2 | assisted：零 GitHub 写；gh 只读 | ✅ | prompt 契约 + `ghcli.READONLY_PREFIXES` 白名单 |
| §6.3 | handoff 四件套 + 回填 + 放弃 | ✅ | `handoff.validate/finish/cancel`；8 例测试 |
| §6.4 | prompt = canonical + `## 权限模式契约` + mode_delta 原文 | ✅ | `prompt.assemble_prompt`；`test_prompt.py` 校验 A/B 原文用词 |
| §6.5 | 两模式都产出 dossier | ✅ | `runner` 统一路径；两模式集成测试均断言 |
| §7 | 三作业幂等 + 12h freshness + 断路器 + 周 cap | ✅ | `jobs.py`；`test_jobs.py`（13 例） |
| §7.1 | "每个作业开始先刷新再决策" | ✅ | run-day 开工前对临近过期（>60% 新鲜期）的 VERIFIED 有界刷新（≤3），失败即熔断；`test_run_day_refreshes_near_expiry_before_planning` |
| §7 | pool-maint 达成 "≥3 仓、单仓 ≤2" | ⚠️ | 该约束由 `pool.py plan-week` 判定并输出 `repo_cap_excluded` / diversity warnings，作业如实转述，不自行重算 |
| §7 | monitor 记录 CI / 维护者评论 / 竞态 PR | ✅ | `ghcli.competing_prs` + `monitor` observation；查询失败记 `UNKNOWN`，不用空列表冒充"无竞态" |
| §7.3 | daemon | ✅ | `daemon --once --dry-run` 实跑，日志落 `logs/daemon.log` |
| §7.3 | install-schedule / 卸载 | ✅ | 生成 `scripts/job-*.cmd` 再 `schtasks`；`test_schedule.py`（4 例）。真实注册 🟡 |
| §7.4 | 所有作业 `--dry-run` | ✅ | 三作业 dry-run 均不产生外部动作（monitor 干跑不调 gh，有测试） |
| §8 | 统一 Event 协议 + 事件双写（任务流 + 池摘要） | ✅ | `adapters/base.py`、`events.py`；`test_events.py` |
| §8 | claude_code adapter（模式权限、max-turns、超时、原文落盘） | ✅ | `adapters/claude_code.py`；`doctor` 探测到 2.1.153 |
| §8 | gemini / codex / opencode | 🟡 | 按 §8 命令形态实现 + 版本探测；未真实跑过任务；codex/opencode 本机未安装 |
| §8 | workbuddy 留桩 | ✅ | 明确返回"接口待定"，不假装可用（计划即 P4） |
| §8 | prompt 经 stdin（Windows 命令行长度） | ✅ | `prompt_transport`；claude 用 stdin |
| §8 | exit≠0/超时 → BLOCKED 且原文落盘 | ✅ | `test_nonzero_exit_blocks` / `test_timeout_blocks` |
| §8 | adapter/worktree 锁 + 并发 ≤ max_parallel | ✅ | `locks.py`；含重入与死进程回收测试 |
| §9.1 | 命令面（status/pick/run/cancel/task/job/daemon/schedule/whitelist/chat） | ✅ | `pr_agent --help`；另有 `doctor/assets/repo` |
| §9.2 | task_id 命名、worktree、branch、base 来源记录 | ✅ | `runner._build_plan`；`test_gitsvc.py` |
| §9.3 | 规则版分配 + >400 行用 large + 决策落事件 | ✅ | `allocate.py`；`test_allocate.py` |
| §10 | 黑白名单过滤 + min_stars 硬门槛 | ✅ | `governance.check_repo` |
| §10 | 提案链（agent 写 → 人工批准才生效） | ✅ | `whitelist propose/approve/list`，真实工作区实测；`test_governance.py` |
| §10 | discoveries 汇总上报 | ✅ | `pool-maint` 汇总 `tasks/*/discoveries/*.md` |
| §10 | 所有提案/批准写事件 | ✅ | `logs/governance.jsonl` |
| §11 | 三处日志分工 | ✅ | `pool/events.jsonl`（池真相）、`tasks/<id>/events.jsonl`、`logs/` |
| §12 | 跨厂商第二意见 + Blocker 打回 + 并入 dossier 与 handoff | ✅ | `cross_review.py` + `dossier.append_cross_review_section`（本轮补齐 dossier 侧）；默认 `enabled:false` |
| §12 | 接真实第二厂商模型跑通 | 🟡 | 逻辑与打回路径有单测；未接真实模型 |
| §13.1 | token 只走 DPAPI + gh 凭据库，不入库/日志/事件 | ✅ | 代码不读 token 文件；`gh` 调用不打印凭证 |
| §13.2 | 最小权限 PAT | ✅ | 只依赖 `gh auth status`；不做任何权限提升 |
| §13.3 | fork + upstream PR；不动 main/master | ⚠️ | 分支策略已写进 prompt 契约（先 fork、`gh pr create --repo <upstream> --head <账号>:<分支>`），fork 的**创建动作**由 agent(full_autonomy)/人工(assisted) 执行，调度器不代做 |
| §13.4 | 四许可 / 三禁止 | ✅ | prompt 契约 + monitor 只读 + `ghcli` 只读白名单（越界即拒绝） |
| §13.5 | AI 贡献披露 | ✅ | `handoff.validate` 要求 `pr_body.md` 含 `Generated-by` |
| §14 P1 | 两模式各 ≥1 次（自有测试仓库演示） | 🟡 | 仿真 adapter 全链验证；真实 agent + 真实仓库未跑 |
| §14 P2 | 两家并行两 worktree 互不干扰 | 🟡 | 锁与并发槽有单测；未真实并行两个 agent |
| §14 P2 | 额度满降级 / 断路器 | ✅ | `test_allocate.py`、`test_jobs.py` |
| §14 P3 | 未批准提案不参与分配 / Blocker 打回 / dossier 含两厂商意见 | ✅ | 三组单测；同上 🟡 真实模型未跑 |
| §14 DoD | pytest 全绿 + changelog + 无遗留垃圾 | ✅ | 110 项通过；`tasks/` 空；`docs/changelog.md` |
| §15 | 非目标（不 GUI 驱动、不做 Key UI、不自动合并、不 MLA 调度） | ✅ | 无相关代码路径 |
| §16 | 风险对策（版本漂移探测、stdin、硬超时、额度、撞仓、布局自检） | ✅ | 逐条对应实现；`doctor` 汇总 |
| §17 | Python ≥3.11、中文输出、幂等、Conventional Commits、.gitignore | ✅ | `pyproject.toml`、`.gitignore`、两次 Conventional 提交 |

## 结论

- **计划范围内"写代码"的部分已全部落地**，110 项 pytest 全绿。
- **未完成的两类**：
  1. **未真实跑**（不是没实现）：真实 coding agent 端到端、GitHub 写操作、`schtasks` 注册、交叉 review 接真实模型、两家并行。需要环境/授权与人工放行。
  2. **一处明确未实现**：`require_backing` 与 `max_days_to_first_review` 未参与评分——池记录没有对应字段，硬凑就是编数据，因此保持"解析 + 校验但不打分"，等数据源。
- **一处解释性实现需确认**：§5 的 `weights` 三维公式。当前取 `potential = 审计分/10`、`feasibility = scope_gate PASS 且规模数据齐全`、`speed = 1 - 竞态数/上限`，缺证据一律 0 分并列 `missing`。若你要的口径不同（例如 speed 按 Issue 存在时长、feasibility 按改动行数分档），改 `governance.score_record` 一处即可。
- **两处偏差**（状态机不允许的连边、`schtasks` 引号问题）已记录在 `docs/changelog.md`，未做降级。
