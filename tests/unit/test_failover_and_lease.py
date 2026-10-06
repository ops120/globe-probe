"""批次 2/3 单元与集成测试：agent 多 server failover + 后台任务 DB 租约。"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpm.agent.agent import Agent  # noqa: E402
from gpm.config import Config  # noqa: E402
from gpm.server.lease import DbLease  # noqa: E402
from gpm.server.storage import Storage  # noqa: E402


def make_agent(tmp_path, servers, server_url=None):
    """servers 原样进 config（测试语义：servers 就是完整备用列表）。"""
    cfg = Config({"agent": {
        "server_url": server_url or (servers[0] if servers else "http://127.0.0.1:8620"),
        "servers": list(servers or []),
        "name": "failover-test", "data_dir": str(tmp_path / "agent"),
    }, "probe": {}})
    return Agent(cfg)


def test_servers_parsing(tmp_path):
    """servers 解析：列表、逗号串、server_url 兜底、去重保序。"""
    a = make_agent(tmp_path, ["http://a", "http://b"])
    assert a.servers == ["http://a", "http://b"] and a.server == "http://a"
    # server_url 逗号列表（CLI --server "a,b" 形态）
    b = make_agent(tmp_path, ["http://a", "http://b"], server_url="http://a,http://b")
    assert b.servers == ["http://a", "http://b"]
    # 中文逗号与空白容忍：server_url 拆出 a,c；servers 提供 b → 汇总去重 [a,b,c]
    c = make_agent(tmp_path, [" http://b "], server_url="http://a，http://c")
    # 顺序约定：server_url（主/粘滞地址）在前 → [a, c, b]；去重保序
    assert c.servers == ["http://a", "http://c", "http://b"] and len(c.servers) == 3
    # 单值退化为单元素
    d = make_agent(tmp_path, [], server_url="http://only")
    assert d.servers == ["http://only"] and len(d.servers) == 1


def test_rotate_server_cycles(tmp_path):
    a = make_agent(tmp_path, ["http://a", "http://b", "http://c"])
    a._rotate_server("测试")
    assert a.server == "http://b" and a._active == 1
    a._rotate_server("测试")
    assert a.server == "http://c"
    a._rotate_server("测试")
    assert a.server == "http://a" and a._active == 0


def test_failover_after_consecutive_failures(tmp_path, monkeypatch):
    """连续 2 次同步失败 → 自动切到下一个 server（run 循环内联逻辑的等价驱动）。"""
    import gpm.agent.agent as mod
    a = make_agent(tmp_path, ["http://bad", "http://good"])

    calls = []

    def fake_post(url, payload, timeout=10):
        calls.append(url)
        if "bad" in url:
            return 0, {}
        if url.endswith("/api/agent/sync"):
            return 200, {"config_version": 1, "server_time": int(time.time()), "tasks": []}
        return 200, {"node_id": "n-failover", "created": False, "server_time": int(time.time())}

    monkeypatch.setattr(mod, "_post_json", fake_post)

    async def scenario():
        # 模拟 run() 的失败计数与切换（与 run() 内联逻辑同口径驱动两次失败）
        a._sync_fails += 1
        a._sync_fails += 1
        if a._sync_fails >= 2 and len(a.servers) > 1:
            a._rotate_server("连续同步失败 %d 次" % a._sync_fails)
            a._sync_fails = 0
        await a.sync_once()
        assert "good" in calls[-1]

    asyncio.run(scenario())
    assert a.server == "http://good"


def test_try_preferred_switches_back(tmp_path, monkeypatch):
    """粘滞回探：备用期间首选恢复 → 切回；首选仍坏 → 留在备用。"""
    import gpm.agent.agent as mod
    a = make_agent(tmp_path, ["http://pref", "http://backup"])
    a._active = 1
    a.server = "http://backup"
    state = {"ok": False}

    async def fake_sync(self):
        if not state["ok"]:
            raise ConnectionError("首选仍不可达")
    monkeypatch.setattr(Agent, "sync_once", fake_sync)

    asyncio.run(a._try_preferred())
    assert a.server == "http://backup", "首选仍坏应留在备用"
    state["ok"] = True
    asyncio.run(a._try_preferred())
    assert a.server == "http://pref" and a._active == 0, "首选恢复应切回"


# ---------------- 批次 3：DB 租约 ----------------

def test_lease_single_runner_two_processes(tmp_path):
    """双「进程」（两个 Storage 实例共享同一 SQLite 文件）单执行者语义。"""
    db = str(tmp_path / "lease.db")
    sa = Storage(db)
    sb = Storage(db)
    la = DbLease(sa, "alert", "holder-A")
    lb = DbLease(sb, "alert", "holder-B")
    assert la.hold() is True
    assert lb.hold() is False, "他人持有未过期 → 不可获取"
    assert la.hold() is True, "持有者续约必须成功"
    assert lb.hold() is False
    assert la.release() is True
    assert lb.hold() is True, "让出后可被接管"
    assert la.hold() is False


def test_lease_expiry_takeover(tmp_path):
    """TTL 过期后可被争夺（把 expires_at 拨到过去，模拟持有者死亡）。"""
    db = str(tmp_path / "lease2.db")
    sa = Storage(db)
    sb = Storage(db)
    la = DbLease(sa, "retention", "A", ttl=60)
    lb = DbLease(sb, "retention", "B")
    assert la.hold() is True
    with sa.lock:
        sa.db.execute("UPDATE lease SET expires_at=strftime('%s','now')-1 WHERE name='retention'")
        sa.db.commit()
    assert lb.hold() is True, "过期租约必须可被接管"
    assert la.hold() is False, "接管后旧持有者续约失败"


def test_lease_distinct_names_independent(tmp_path):
    """不同租约名互不影响（alert 与 retention 是两把锁）。"""
    db = str(tmp_path / "lease3.db")
    s = Storage(db)
    l1 = DbLease(s, "alert", "X")
    l2 = DbLease(s, "retention", "Y")
    assert l1.hold() and l2.hold()


def test_app_loops_have_lease_guard():
    """接线回归钉：8 个后台循环必须走租约守卫；selfcheck 刻意豁免（每实例自诊断）。"""
    from gpm.server import app
    src = Path(app.__file__).read_text(encoding="utf-8")
    for name in ("sweep", "agg", "retention", "alert", "retry", "digest", "channel_probe", "pull"):
        assert f'state["leases"]["{name}"]' in src, f"循环 {name} 缺租约守卫"
    # selfcheck 不接租约（注释即声明）
    assert "不接单执行者租约" in src
