"""open_coding pool.py 的唯一调用入口（只读 + 状态迁移）。

原则：
- 状态真相只在 pool/issues.jsonl；本模块不直接改写池文件。
- 子进程失败、超时、输出不可解析 → PoolError（调用方据此 BLOCKED），绝不猜测结果。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PoolError
from .paths import Paths

DEFAULT_TIMEOUT = 180.0
REFRESH_TIMEOUT = 900.0


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    def as_evidence(self) -> dict:
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 3),
            "stdout_tail": self.stdout[-4000:],
            "stderr_tail": self.stderr[-4000:],
        }


@dataclass
class Pool:
    paths: Paths
    python: str = field(default_factory=lambda: sys.executable)

    def _run(self, args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        if not self.paths.pool_cli.is_file():
            raise PoolError(
                f"找不到 pool.py：{self.paths.pool_cli}",
                detail="open_coding 布局变化，只允许依赖 DEVELOPMENT_PLAN §2.3 表列路径。",
            )
        argv = [self.python, str(self.paths.pool_cli), *args]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.paths.open_coding),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PoolError(
                f"pool.py 超时（{timeout:.0f}s）：{' '.join(args)}",
                detail=(exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
                evidence={"argv": argv, "timeout": timeout},
            ) from exc
        except OSError as exc:
            raise PoolError(f"无法执行 pool.py：{exc}", evidence={"argv": argv}) from exc
        return CommandResult(
            argv=argv,
            cwd=str(self.paths.open_coding),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            seconds=time.monotonic() - started,
        )

    # --- 查询 ---
    def health(self) -> dict:
        result = self._run(["health"])
        if result.returncode != 0:
            raise PoolError("pool.py health 失败", detail=result.as_evidence().__str__(),
                            evidence=result.as_evidence())
        return _parse_json(result, "health")

    def show(self, status: str | None = None) -> list[dict]:
        args = ["show"] + (["--status", status] if status else [])
        result = self._run(args)
        if result.returncode != 0:
            raise PoolError("pool.py show 失败", evidence=result.as_evidence())
        payload = _parse_json(result, "show")
        if not isinstance(payload, list):
            raise PoolError("pool.py show 输出不是数组", evidence=result.as_evidence())
        return [item for item in payload if isinstance(item, dict)]

    def record_for(self, issue_id: str) -> dict | None:
        for record in self.show():
            if str(record.get("id")) == issue_id:
                return record
        return None

    def plan_week(self, target: int = 3, reserve: int = 6) -> tuple[int, dict | None, CommandResult]:
        result = self._run(["plan-week", "--target", str(target), "--reserve", str(reserve)])
        payload = None
        if result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = None
        return result.returncode, payload, result

    # --- 变更 ---
    def transition(self, issue_id: str, to: str, *, actor: str = "pr-agent", evidence: dict | None = None) -> dict:
        result = self._run(
            ["transition", "--id", issue_id, "--to", to, "--actor", actor,
             "--evidence", json.dumps(evidence or {}, ensure_ascii=False)]
        )
        if result.returncode != 0:
            raise PoolError(
                f"池状态迁移失败：{issue_id} → {to}",
                detail=(result.stderr or result.stdout)[-4000:],
                evidence=result.as_evidence(),
            )
        return _parse_json(result, "transition")

    def refresh(self, repo: str, issue_number: int, local_repo: Path | None = None) -> dict:
        args = ["refresh", "--repo", repo, "--issue", str(issue_number)]
        if local_repo is not None:
            args.extend(["--local-repo", str(local_repo)])
        result = self._run(args, timeout=REFRESH_TIMEOUT)
        if result.returncode != 0:
            raise PoolError(
                f"候选刷新失败：{repo}#{issue_number}",
                detail=(result.stderr or result.stdout)[-4000:],
                evidence=result.as_evidence(),
            )
        return _parse_json(result, "refresh")


def _parse_json(result: CommandResult, command: str) -> object:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PoolError(
            f"pool.py {command} 输出不是合法 JSON",
            detail=f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}",
            evidence=result.as_evidence(),
        ) from exc
