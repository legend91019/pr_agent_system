# pr_agent

**A multi-agent PR orchestration system for individual developers.** pr_agent turns your idle coding-agent quotas (Claude Code, Gemini CLI, Codex, OpenCode) into a production line for open-source contributions: it picks a verified issue, dispatches it to an available agent running **headless** in an **isolated git worktree**, assembles a **per-issue review dossier**, and stops at a **human final review** gate — it never merges or approves on its own.

[English](README.md) | [简体中文](README_zh-CN.md)

---

## Why

Most of us hold quotas on several AI coding agents and let them expire. Doing this manually means juggling terminals, losing track of state, and mixing contexts. pr_agent automates the whole loop with one non-negotiable rule: **a human always makes the final call**.

Three principles govern every code path:

| Principle | Meaning |
|---|---|
| **Fail-closed** | If something can't be read or verified, it's `UNKNOWN` / `BLOCKED` — never silently treated as "fine" |
| **Evidence-first** | Raw outputs are persisted (events, diffs, verification logs); nothing is summarized into "failed but valuable" |
| **Human final review** | Merging, approving, and closing are always human decisions |

## Architecture

```text
┌────────────────────────────────────────────────────────────────┐
│  You (chat / CLI)                                              │
│      pr_agent chat · pick · status · config wizard             │
└──────────────┬─────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────┐
│  Scheduler (pr_agent core)                                     │
│  · permission mode: assisted / full_autonomy                   │
│  · quota accounting (window / daily / weekly) + fallback chain │
│  · whitelist / blacklist / hard gates (min_stars, PR races)    │
│  · task state machine (reused from the pool engine)            │
│  · scheduled jobs: pool-maint · run-day · monitor              │
└──────────────┬─────────────────────────────────────────────────┘
               ▼  unified adapter protocol: spawn · event stream · result
┌────────────────────────────────────────────────────────────────┐
│  Headless CLI agents (each task in its own git worktree)       │
│  claude-code · gemini · codex · opencode · (workbuddy, P4)     │
└──────────────┬─────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────┐
│  Cross-vendor review (optional)  →  Dossier  →  PENDING_HUMAN  │
└────────────────────────────────────────────────────────────────┘
```

Key design decisions:

- **Headless CLI, never GUI automation.** Every supported agent runs non-interactively (`claude -p`, `gemini -p`, `codex exec`, `opencode run`) with JSON event streams. GUI scripting is brittle, slow, unauditable — and reserved for genuinely GUI-only tools (out of scope for now).
- **One task, one worktree.** Agents never share a checkout; contexts can't bleed into each other, and two agents can safely run in parallel.
- **Adapters never touch tokens.** Credentials live in the OS credential store / `gh` and are consumed by `gh` itself; the scheduler only checks `gh auth status` before a full-autonomy run.
- **This repo is the orchestration layer, not a new workflow.** Candidate pool state machine, admission audits, and dossier rendering are reused from the companion `open_coding` workflow (read-only authoritative assets); pr_agent calls them, it does not rewrite them.

## Workflow (life of a task)

1. **Pool maintenance** (`job pool-maint`): refreshes candidate issue records toward ≥6 fresh `VERIFIED` entries across ≥3 repos. It reports shortfalls honestly — it never lowers the bar to fill quotas.
2. **Selection** (`pick` manually, or `job run-day` on schedule): the first eligible candidate in plan-week order wins. No weighted scoring — **priorities are judged by the agent or by you**; only hard gates apply (see below).
3. **Packaging**: the canonical workflow prompt is assembled with the permission-mode contract, task context, and fork/upstream branch strategy.
4. **Dispatch**: the scheduler allocates an agent (enabled + probed + quota left + cooldown passed), spawns it headless in a fresh worktree, and records every event into `tasks/<id>/events.jsonl`.
5. **Delivery**:
   - `full_autonomy` — the agent comments, pushes a branch, opens the PR via `gh`, then stops at `PENDING_HUMAN`.
   - `assisted` (default) — **zero GitHub writes**; the agent produces a handoff bundle: `report.md`, `comment.md` (draft comment), `pr_body.md` (draft PR), `commands.md` (copy-paste command list). You execute manually, then call `task handoff-done` to record the URLs.
6. **Cross-vendor review** (optional): a second agent from a *different* vendor reviews the diff; Blocker/Major findings send the task back to `CODING`, Minors land in the dossier.
7. **Dossier + human gate**: every task produces a per-issue review document (`pr_reviews/<owner>__<repo>__<issue>.md`) with the diff summary, raw verification output, and unverified items. The pool record then waits at `PENDING_HUMAN` — merging is yours.

## Governance: the whitelist / blacklist machinery

This is where pr_agent differs from "just run an agent on an issue":

**Three sources of whitelist entries, one approval gate.**

| Source | Path | Effective? |
|---|---|---|
| You, editing `config/projects.yaml` | `added_by: human, approved: true` | immediately |
| Frontend agent proposal | `pr_agent whitelist propose OWNER/REPO` writes `added_by: agent, approved: false` | **never, until you approve** |
| Backend agent discovery | agents report promising repos into `tasks/<id>/discoveries/`, surfaced by `pool-maint` | same approval chain |

Every proposal and approval is journaled to `logs/governance.jsonl`. An unapproved proposal never influences allocation.

**Blacklist + hard gates instead of scoring.** Explicit blacklists (`repo: someone/noisy-repo, reason: ...`) always win. For everything else, two hard gates apply — deliberately *not* a weighted score (a 2026-10 design ruling: scoring formulas pretend precision that the evidence doesn't support, so priorities are left to human/agent judgment):

- `min_stars` (default 150) — unstarred repos are not worth your quota;
- `max_recent_competing_prs` (default 3) — issues with too many semantic rival PRs are vetoed; the scheduler simply takes the next eligible candidate in order.

**Six pre-flight gates (fail-closed).** Before any dispatch: record must be `VERIFIED`, `scope_gate` must be `PASS`, the record must be fresh, the repo must pass governance, the agent must be enabled + probed + within quota + past cooldown, and a concurrency slot (with per-adapter and per-worktree locks) must be free. Any failure is a refusal, not a skip.

## Scheduled jobs

| Job | Cadence | Semantics |
|---|---|---|
| `job pool-maint` | every 6h | refresh candidates toward ≥6 fresh `VERIFIED` (≥3 repos, ≤2 per repo); never lowers the bar to fill numbers |
| `job run-day` | weekdays 09:05 | `plan-week --target 3 --reserve 6`; skips if the plan is incomplete; at most 1 production task per day; circuit-breaker stops the day on repeated failures |
| `job monitor` | every 6h | read-only tracking of CI, maintainer comments, and competing PRs — never merges, approves, or replies |

Carriers: a built-in `pr_agent daemon`, or `pr_agent install-schedule` (generates `scripts/job-*.cmd` wrappers and registers them via Windows `schtasks`; `--confirm` required to actually register).

## Getting started

Prerequisites: Python ≥ 3.11, git, [GitHub CLI](https://cli.github.com/) installed and authenticated, at least one supported coding-agent CLI installed, and the companion `open_coding` workflow assets available locally (see `DEVELOPMENT_PLAN.md` §2.3 for the required asset list — the scheduler self-checks them and refuses to start if any is missing).

```bash
git clone https://github.com/legend91019/pr_agent_system.git
cd pr_agent_system

python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e ".[dev]"
```

**1. Configure everything through the interactive panel** (recommended):

```bash
pr_agent config wizard
```

The wizard auto-probes installed CLIs, then walks you through `config/agents.yaml` (mode, per-agent enable/model/quota, fallback chain) and `config/projects.yaml` (whitelist/blacklist, `min_stars`, competing-PR cap, permission mode, daily cap, reserve). Existing values are shown as defaults, a `.bak` backup is written first, and everything is dry-run validated before saving. `pr_agent config show` prints the sanitized current configuration.

**2. Self-check, then look before you leap:**

```bash
pr_agent doctor                # environment / assets / config / adapters / gh — read-only
pr_agent status                # pool distribution, quotas, tasks, concurrency, circuit breaker

pr_agent pick OWNER/REPO 123 --dry-run    # see the plan without executing
pr_agent pick OWNER/REPO 123 --no-run     # package only; inspect tasks/<id>/prompt.md
pr_agent run <task_id>                    # actually dispatch
```

**3. Daily driving:**

```bash
pr_agent job run-day --dry-run   # what the morning job would do
pr_agent job pool-maint          # top up the candidate pool
pr_agent task handoff-done <task_id> --comment-url URL --pr-url URL   # after manual assisted steps
pr_agent whitelist propose / approve / list                           # governance chain
```

Secrets: `config/agents.yaml` references environment variables (`${PR_AGENT_FRONTEND_KEY}`, etc.); tokens never live in the repo. GitHub credentials go through `gh` and the OS credential store.

## Testing

```bash
pytest -q    # 112 tests, fully offline: simulated pool / adapters / temp git repos
```

Coverage includes strict config validation, quota windows and cooldowns, allocation and fallback, prompt assembly (canonical + mode delta + fork strategy), handoff bundle validation and idempotent back-fill, dossier rendering, cross-review merge idempotency, competing-PR monitoring (failures recorded as `UNKNOWN`), pre-run refresh, locks (re-entrancy / stale-process recovery / concurrency slots), worktrees and diffs, all three jobs in dry-run, the circuit breaker, schedule wrappers, and the frontend chat injection of live status facts.

## Command surface

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

Exit codes: `0` ok/dry-run · `2` queued/skipped · `3` BLOCKED · `1` argument/config/environment error. With `--json`, failures still emit `{"status":"blocked","error":...,"evidence":...}`.

## Known limitations (honestly)

- End-to-end runs with real coding agents and real GitHub writes have not been executed yet — the suite validates orchestration with simulated adapters; real CLI flags drift across versions.
- Adapter probing is timeout-protected and cached; a CLI that hangs on `--version` is reported unavailable (fail-closed), not assumed working.
- Scheduled tasks are not yet registered on a real machine (`schtasks --confirm` is a deliberate human step).
- Cross-vendor review is implemented and unit-tested but off by default.

## Documentation

- `DEVELOPMENT_PLAN.md` — the single source of requirements (design, §1–§17)
- `docs/plan-coverage.md` — section-by-section coverage matrix against the plan (✅/🟡/⚠️/❌ with evidence)
- `docs/changelog.md` — delivery log, ambiguity rulings, unverified-item ledger
