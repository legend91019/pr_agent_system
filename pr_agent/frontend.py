"""可选前端对话（OpenAI 兼容，可配置模型；不参与关键路径）。

只做两件事：自然语言问答 + 显式命令（:status / :pick OWNER/REPO#N / :quit）。
真正的调度能力仍在 CLI 子命令，避免 LLM 直接改状态。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .config import AgentsConfig
from .paths import Paths

SYSTEM_PROMPT = """你是 pr_agent 的前端助手。你可以解释调度规则、池子状态与 handoff 用法，
但不得声称已经执行了 GitHub 写操作，也不得编造池子数据。
可用命令由人类输入：:status、:pick OWNER/REPO#N、:quit。
回答用中文，简洁，必要时提示用户看 dossier。
"""


def chat_loop(paths: Paths, config: AgentsConfig, message: str | None = None) -> int:
    frontend = config.frontend
    gaps = frontend.describe_gap()
    if gaps:
        print("前端 LLM 未配置完整，chat 不可用：")
        for gap in gaps:
            print(f"- {gap}")
        print("\n请设置环境变量后重试（见 config/agents.yaml 的 frontend 段）。")
        return 1
    api_key = os.environ[frontend.api_key_env]
    endpoint = _endpoint(frontend.base_url)

    history = [{"role": "system", "content": SYSTEM_PROMPT + _status_context(paths, config)}]
    if message:
        history.append({"role": "user", "content": message})
        answer, error = _ask(endpoint, api_key, frontend.model, history)
        if error:
            print(f"调用前端 LLM 失败：{error}")
            return 1
        print(answer)
        return 0

    print("pr_agent chat（:status / :pick OWNER/REPO#N / :quit）")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {":quit", ":q", "exit"}:
            return 0
        if line == ":status":
            from .cli import render_status  # 延迟导入避免循环

            print(render_status(paths, config))
            continue
        if line.startswith(":pick"):
            print("请改用 `pr_agent pick OWNER/REPO ISSUE --dry-run` 先检查计划，再实际执行。")
            continue
        history.append({"role": "user", "content": line})
        answer, error = _ask(endpoint, api_key, frontend.model, history)
        if error:
            print(f"调用前端 LLM 失败：{error}")
            history.pop()
            continue
        history.append({"role": "assistant", "content": answer})
        print(answer)


def _endpoint(base_url: str) -> str:
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"
    return endpoint


def _ask(endpoint: str, api_key: str, model: str, history: list[dict]) -> tuple[str, str | None]:
    payload = json.dumps({
        "model": model,
        "messages": history,
        "temperature": 0.2,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"], None
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
        return "", f"{type(exc).__name__}: {exc}"


def _status_context(paths: Paths, config: AgentsConfig) -> str:
    """把当前状态作为事实注入，避免模型凭记忆编造池子数据。"""
    lines = ["", "当前事实（只能依据这些数据回答）："]
    try:
        from .cli import render_status

        lines.append(render_status(paths, config))
    except Exception as exc:  # noqa: BLE001 - 前端不因状态读取失败而崩溃
        lines.append(f"（状态读取失败：{type(exc).__name__}: {exc}，请如实说明无法读取）")
    lines.append("尚不存在的池记录一律回答 UNKNOWN，不得推测。")
    return "\n".join(lines)
