"""前端 LLM 对话：未配置即失败关闭；一次性提问注入真实状态事实。"""

from __future__ import annotations

import io
import json

from pr_agent import frontend

from tests.conftest import build_workspace


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_chat_without_config_fails_closed(tmp_path, capsys):
    ws = build_workspace(tmp_path)
    code = frontend.chat_loop(ws.paths, ws.config, "今天池子如何")
    out = capsys.readouterr().out
    assert code == 1
    assert "未配置完整" in out


def test_chat_one_shot_injects_status_facts(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path)
    ws.config.frontend.base_url = "http://localhost:9999/v1"
    ws.config.frontend.model = "fake-model"
    monkeypatch.setenv("PR_AGENT_FRONTEND_KEY", "test-key")
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        body = {"choices": [{"message": {"content": "池子有 1 条 VERIFIED"}}]}
        return _Response(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(frontend.urllib.request, "urlopen", fake_urlopen)
    code = frontend.chat_loop(ws.paths, ws.config, "今天池子如何")
    assert code == 0
    assert captured["url"] == "http://localhost:9999/v1/chat/completions"
    system = captured["payload"]["messages"][0]["content"]
    assert "当前事实" in system and "VERIFIED" in system
    assert captured["payload"]["messages"][-1]["content"] == "今天池子如何"


def test_chat_reports_llm_failure(tmp_path, monkeypatch, capsys):
    ws = build_workspace(tmp_path)
    ws.config.frontend.base_url = "http://localhost:9999/v1"
    ws.config.frontend.model = "fake-model"
    monkeypatch.setenv("PR_AGENT_FRONTEND_KEY", "test-key")

    def boom(request, timeout=0):
        raise frontend.urllib.error.URLError("connection refused")

    monkeypatch.setattr(frontend.urllib.request, "urlopen", boom)
    code = frontend.chat_loop(ws.paths, ws.config, "hi")
    assert code == 1
    assert "调用前端 LLM 失败" in capsys.readouterr().out
