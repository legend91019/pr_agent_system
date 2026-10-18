"""文件锁与并发槽。"""

from __future__ import annotations

import os
import time

import pytest

from pr_agent.errors import LockBusyError
from pr_agent.locks import FileLock, acquire_slot, active_slots

from tests.conftest import build_workspace


def test_exclusive_lock(tmp_path):
    path = tmp_path / "a.lock"
    with FileLock(path, owner="one", timeout=0):
        with pytest.raises(LockBusyError):
            FileLock(path, owner="two", timeout=0).__enter__()
    with FileLock(path, owner="three", timeout=0) as lock:
        assert lock.acquired


def test_stale_lock_is_reclaimed(tmp_path):
    path = tmp_path / "a.lock"
    path.write_text('{"owner":"dead"}', encoding="utf-8")
    old = time.time() - 7200
    os.utime(path, (old, old))
    with FileLock(path, owner="alive", timeout=0, stale_after=60) as lock:
        assert lock.acquired


def test_slots_limit_concurrency(tmp_path):
    ws = build_workspace(tmp_path)
    first = acquire_slot(ws.paths, 1, "task-a")
    try:
        with pytest.raises(LockBusyError):
            acquire_slot(ws.paths, 1, "task-b")
        assert active_slots(ws.paths) == ["slot1:task-a"]
    finally:
        first.__exit__()
    second = acquire_slot(ws.paths, 1, "task-b")
    second.__exit__()


def test_acquire_slot_result_can_be_used_as_context_manager(tmp_path):
    """acquire_slot 返回的锁已持锁；再进 with 必须可重入，不能自锁死。"""
    ws = build_workspace(tmp_path)
    slot = acquire_slot(ws.paths, 2, "task-c")
    with slot:
        assert slot.acquired
        assert active_slots(ws.paths) == ["slot1:task-c"]
    assert active_slots(ws.paths) == []  # 退出 with 后释放，槽位可复用
    acquire_slot(ws.paths, 2, "task-d").__exit__()


def test_dead_pid_lock_is_reclaimed_even_if_fresh(tmp_path):
    """持锁进程已不存在时立即回收，不必等 stale_after。"""
    path = tmp_path / "b.lock"
    path.write_text('{"owner":"ghost","pid":999999,"at":"2026-01-01T00:00:00Z"}', encoding="utf-8")
    with FileLock(path, owner="alive", timeout=0, stale_after=3600) as lock:
        assert lock.acquired


def test_acquire_slot_result_can_be_used_as_context_manager(tmp_path):
    """acquire_slot 返回的锁已持锁；再进 with 必须可重入，不能自锁死。"""
    ws = build_workspace(tmp_path)
    slot = acquire_slot(ws.paths, 2, "task-c")
    with slot:
        assert slot.acquired
        assert active_slots(ws.paths) == ["slot1:task-c"]
    assert active_slots(ws.paths) == []  # 退出 with 后释放，槽位可复用
    acquire_slot(ws.paths, 2, "task-d").__exit__()


def test_dead_pid_lock_is_reclaimed_even_if_fresh(tmp_path):
    """持锁进程已不存在时立即回收，不必等 stale_after。"""
    path = tmp_path / "b.lock"
    path.write_text('{"owner":"ghost","pid":999999,"at":"2026-01-01T00:00:00Z"}', encoding="utf-8")
    with FileLock(path, owner="alive", timeout=0, stale_after=3600) as lock:
        assert lock.acquired
