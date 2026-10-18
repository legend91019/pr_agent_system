"""pool.py 子进程封装：失败不吞、输出不可解析即报错。"""

from __future__ import annotations

import pytest

from pr_agent.errors import PoolError
from pr_agent.paths import load_paths
from pr_agent.poolctl import Pool

from tests.conftest import build_workspace


def test_health_and_show(tmp_path):
    ws = build_workspace(tmp_path)
    pool = Pool(ws.paths)
    health = pool.health()
    assert health["total"] == 1
    records = pool.show("VERIFIED")
    assert records[0]["id"] == "Super/Repo#7"


def test_transition_forbidden_is_reported(tmp_path):
    ws = build_workspace(tmp_path)
    pool = Pool(ws.paths)
    with pytest.raises(PoolError) as excinfo:
        pool.transition("Super/Repo#7", "MERGED")
    assert "MERGED" in str(excinfo.value.message)
    assert excinfo.value.evidence.get("returncode") == 1


def test_missing_pool_cli(tmp_path):
    paths = load_paths(tmp_path / "ws", tmp_path / "missing")
    with pytest.raises(PoolError):
        Pool(paths).health()


def test_unparsable_output(tmp_path):
    ws = build_workspace(tmp_path)
    ws.paths.pool_cli.write_text("print('not json')\n", encoding="utf-8")
    with pytest.raises(PoolError) as excinfo:
        Pool(ws.paths).health()
    assert "JSON" in str(excinfo.value.message)
