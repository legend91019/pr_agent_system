"""headless 适配层统一协议（DEVELOPMENT_PLAN §8）。

统一约定：
- 事件类型 ∈ start|tool_use|file_edit|command|assistant_message|usage|result|exit|unknown_line
- exit code ≠ 0 / 超时 / 无法解析 → 调用方必须 BLOCKED，原文落盘。
- adapter 永不读取、传递、记录任何 token；GitHub 操作只经 gh。
- prompt 经 stdin（stdin 型 adapter）或 argv（长度受限型 adapter）传入，绝不出现在日志里。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..config import AgentConfig
from ..errors import AdapterError
from ..events import now_iso

EVENT_TYPES = {
    "start", "tool_use", "file_edit", "command", "assistant_message",
    "usage", "result", "exit", "unknown_line",
}
ARGV_PROMPT_LIMIT = 24_000  # Windows 命令行上限 32767，留出参数余量


@dataclass
class Event:
    type: str
    payload: dict
    adapter: str
    ts: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{self.type}")


class Adapter:
    """adapter 基类：探测可用性 + 生成命令 + 解析输出。"""

    name = "base"
    command_name = ""
    prompt_transport = "stdin"  # "stdin" | "argv"

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._probe: tuple[bool, str] | None = None
        self._resolved: str | None = None

    # --- 可用性 ---
    @property
    def executable(self) -> str:
        """解析真实可执行文件：Windows 下 npm 安装的 CLI 多为 .cmd 包装脚本。"""
        if self.config.command:
            return self.config.command
        if self._resolved:
            return self._resolved
        suffixes = ("", ".cmd", ".exe", ".bat") if os.name == "nt" else ("",)
        for suffix in suffixes:
            found = shutil.which(f"{self.command_name}{suffix}")
            if found:
                self._resolved = found
                return found
        return self.command_name

    def probe(self, *, refresh: bool = False) -> tuple[bool, str]:
        if self._probe is not None and not refresh:
            return self._probe
        path = shutil.which(self.executable)
        if not path:
            self._probe = (False, f"未在 PATH 中找到 {self.executable}")
            return self._probe
        try:
            result = subprocess.run(
                [self.executable, "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._probe = (False, f"版本探测失败：{exc}")
            return self._probe
        if result.returncode != 0:
            self._probe = (
                False,
                f"{self.executable} --version 返回 {result.returncode}：{(result.stderr or result.stdout)[:500]}",
            )
            return self._probe
        version = (result.stdout or result.stderr).strip().splitlines()
        self._probe = (True, version[0] if version else "version unknown")
        return self._probe

    def available(self) -> bool:
        return self.probe()[0]

    # --- 命令 ---
    def build_command(
        self,
        *,
        prompt: str,
        model: str | None,
        full_autonomy: bool,
        max_turns: int,
        prompt_path: Path | None,
    ) -> list[str]:
        raise NotImplementedError

    def parse_line(self, line: str) -> list[tuple[str, dict]]:
        text = line.rstrip("\r\n")
        if not text.strip():
            return []
        return [("assistant_message", {"text": text[:4000]})]

    # --- 执行 ---
    def spawn(
        self,
        *,
        prompt: str,
        prompt_path: Path,
        cwd: Path,
        model: str | None,
        full_autonomy: bool,
        max_turns: int,
        timeout_seconds: float,
        task_id: str,
    ) -> Iterator[Event]:
        ok, detail = self.probe()
        if not ok:
            raise AdapterError(f"adapter 不可用：{self.name}", detail=detail, evidence={"probe": detail})
        if self.prompt_transport == "argv" and len(prompt) > ARGV_PROMPT_LIMIT:
            raise AdapterError(
                f"prompt 过长（{len(prompt)} 字符），超过 argv 安全上限 {ARGV_PROMPT_LIMIT}",
                detail="该 adapter 以 argv 传 prompt；请改用 stdin 型 adapter，或拆分任务。",
            )
        argv = self.build_command(
            prompt=prompt, model=model, full_autonomy=full_autonomy,
            max_turns=max_turns, prompt_path=prompt_path,
        )

        stdin_handle = None
        try:
            if self.prompt_transport == "stdin":
                stdin_handle = open(prompt_path, "rb")
                stdin_arg = stdin_handle
            else:
                stdin_arg = subprocess.DEVNULL
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdin=stdin_arg,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=_child_env(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                raise AdapterError(f"无法启动 {self.name}：{exc}", evidence={"argv": _redact(argv)}) from exc

            yield Event("start", {
                "argv": _redact(argv),
                "cwd": str(cwd),
                "model": model,
                "transport": self.prompt_transport,
                "prompt_sha256_prefix": _prompt_fingerprint(prompt),
                "timeout_seconds": timeout_seconds,
                "task_id": task_id,
                "pid": process.pid,
            }, self.name)

            lines: queue.Queue[str | None] = queue.Queue()
            stderr_chunks: list[str] = []

            def pump_stdout() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            def pump_stderr() -> None:
                assert process.stderr is not None
                for line in process.stderr:
                    stderr_chunks.append(line)
                    if len(stderr_chunks) > 4000:
                        del stderr_chunks[:2000]

            threading.Thread(target=pump_stdout, daemon=True).start()
            threading.Thread(target=pump_stderr, daemon=True).start()

            deadline = time.monotonic() + timeout_seconds
            started = time.monotonic()
            timed_out = False
            finished = False
            while not finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate(process)
                    break
                try:
                    line = lines.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    if process.poll() is not None and lines.empty():
                        break
                    continue
                if line is None:
                    finished = True
                    break
                for event_type, payload in self.parse_line(line):
                    if event_type == "unknown_line":
                        payload.setdefault("raw_line", line.rstrip("\r\n")[:8000])
                    yield Event(event_type, payload, self.name)

            try:
                returncode = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _terminate(process)
                returncode = process.wait(timeout=30)

            stderr_text = "".join(stderr_chunks)
            yield Event("usage", {
                "duration_seconds": round(time.monotonic() - started, 3),
                "timed_out": timed_out,
                "stderr_tail": stderr_text[-6000:],
            }, self.name)
            yield Event("exit", {
                "returncode": returncode,
                "timed_out": timed_out,
                "stderr_tail": stderr_text[-6000:],
            }, self.name)
        finally:
            if stdin_handle is not None:
                stdin_handle.close()


def _terminate(process: subprocess.Popen) -> None:
    """Windows 下按进程树终止，避免 headless 子进程残留。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True, text=True, errors="replace",
        )
    else:  # pragma: no cover - 本机为 Windows
        process.kill()


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["CI"] = env.get("CI", "") or "1"
    return env


def _redact(argv: list[str]) -> list[str]:
    """prompt 不进入事件日志（防止上下文与潜在敏感信息外泄）。"""
    redacted = []
    for item in argv:
        if len(item) > 200:
            redacted.append(f"<redacted {len(item)} chars>")
        else:
            redacted.append(item)
    return redacted


def _prompt_fingerprint(prompt: str) -> str:
    import hashlib

    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
