"""文件锁：每 worktree 一把、每 adapter 一把、全局并发槽（§8 硬规则）。

实现用 O_CREAT|O_EXCL 原子创建锁文件；释放即删除。
持锁进程崩溃遗留的锁按 mtime 超过 stale_after 视为陈旧，可由下一位持锁者回收。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .errors import LockBusyError
from .events import now_iso
from .paths import Paths


class FileLock(AbstractContextManager["FileLock"]):
    def __init__(
        self,
        path: Path,
        *,
        owner: str,
        timeout: float = 10.0,
        stale_after: float = 3600.0,
        poll: float = 0.1,
    ) -> None:
        self.path = path
        self.owner = owner
        self.timeout = timeout
        self.stale_after = stale_after
        self.poll = poll
        self.acquired = False

    def _holder(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _try_once(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"owner": self.owner, "pid": os.getpid(), "at": now_iso()}, ensure_ascii=False)
            )
        self.acquired = True
        return True

    def _reclaim_if_stale(self) -> bool:
        """回收陈旧锁：持锁进程已不存在，或锁文件超过 stale_after。

        进程存活判定优先于超时判定——否则一次 kill -9 会让该槽位在 stale_after（默认 1h）
        内不可用；反之长任务超时也不该被误抢，所以两者只要满足「进程已死」即回收。
        """
        try:
            stat = self.path.stat()
        except OSError:
            return False
        holder = self._holder()
        pid = holder.get("pid")
        dead = isinstance(pid, int) and pid > 0 and pid != os.getpid() and not _pid_alive(pid)
        if not dead and (time.time() - stat.st_mtime) < self.stale_after:
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    def __enter__(self) -> "FileLock":
        if self.acquired:  # 已持锁：允许重入，避免 acquire_slot 返回后 with 二次加锁
            return self
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_once():
                return self
            if self._reclaim_if_stale():
                continue
            if time.monotonic() >= deadline:
                holder = self._holder()
                raise LockBusyError(
                    f"锁被占用：{self.path.name}",
                    detail=json.dumps(holder, ensure_ascii=False),
                    evidence={"lock": str(self.path), "holder": holder, "owner": self.owner},
                )
            time.sleep(self.poll)

    def __exit__(self, *exc_info: object) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Windows 上句柄未及时释放时删除会失败；显式标记，让下一位持锁者按 pid 回收
                pass
            self.acquired = False


def worktree_lock(paths: Paths, task_id: str, *, timeout: float = 5.0) -> FileLock:
    return FileLock(paths.locks_dir / f"worktree-{_safe(task_id)}.lock", owner=f"worktree:{task_id}", timeout=timeout)


def adapter_lock(paths: Paths, adapter: str, *, timeout: float = 5.0) -> FileLock:
    return FileLock(paths.locks_dir / f"adapter-{_safe(adapter)}.lock", owner=f"adapter:{adapter}", timeout=timeout)


def acquire_slot(paths: Paths, max_parallel: int, task_id: str) -> FileLock:
    """全局并发槽：最多 max_parallel 个任务同时执行。"""
    paths.locks_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, max_parallel + 1):
        lock = FileLock(
            paths.locks_dir / f"slot-{index}.lock",
            owner=f"slot{index}:{task_id}",
            timeout=0.0,
            stale_after=6 * 3600,
        )
        try:
            return lock.__enter__()
        except LockBusyError:
            continue
    holders = []
    for index in range(1, max_parallel + 1):
        path = paths.locks_dir / f"slot-{index}.lock"
        if path.exists():
            try:
                holders.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                holders.append({"owner": "unknown"})
    raise LockBusyError(
        f"并发已满（max_parallel={max_parallel}），任务入队等待",
        detail=json.dumps(holders, ensure_ascii=False),
        evidence={"holders": holders},
    )


def active_slots(paths: Paths) -> list[str]:
    slots = []
    if not paths.locks_dir.is_dir():
        return slots
    for path in sorted(paths.locks_dir.glob("slot-*.lock")):
        try:
            slots.append(str(json.loads(path.read_text(encoding="utf-8")).get("owner", "unknown")))
        except (OSError, json.JSONDecodeError):
            slots.append(f"{path.name}:unknown")
    return slots


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


def _pid_alive(pid: int) -> bool:
    """判定 pid 是否仍然存活（不发送任何信号，避免 Windows 上 os.kill 的副作用）。"""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(process_query_limited_information, False, wintypes.DWORD(pid))
            if not handle:
                # 无权限访问也可能是「进程存在」；用 ERROR_INVALID_PARAMETER 区分不存在
                return ctypes.get_last_error() != 87
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 判定失败时保守视为存活，避免误抢锁
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True
