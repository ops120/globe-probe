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


def test_failover_driven_by_run_loop(tmp_path, monkeypatch):
    """真驱动 run()：坏首选连续失败 → 自动切备用（治「手工重演计数」的假绿）。

    上一版在测试内自己 `a._sync_fails += 1` 再调 `_rotate_server`，从未调用 run()——
    把 run() 里阈值 2 改成 3、或删掉 `next_sync_try = 0`，测试照样全绿（复核抓到的假绿）。
    """
    a = make_agent(tmp_path, ["http://bad", "http://good"])
    seen = []

    async def fake_sync(self):
        seen.append(self.server)
        if self.server == "http://bad":
            raise ConnectionError("首选不可达")

    async def no_report(self):
        return

    monkeypatch.setattr(Agent, "sync_once", fake_sync)
    monkeypatch.setattr(Agent, "report_once", no_report)

    async def scenario():
        task = asyncio.create_task(a.run())
        for _ in range(30):                 # run 每轮 sleep(1s)，最多等约 3s
            await asyncio.sleep(0.1)
            if a.server == "http://good":
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert "http://bad" in seen, "首选应先被尝试"
    assert a.server == "http://good", f"连续失败后应切到备用（实际 {a.server}，轨迹 {seen}）"


def test_rotate_only_after_two_consecutive_failures(tmp_path, monkeypatch):
    """把「连续失败 ≥2 次才切换」这条阈值钉住：阈值被改成 1 或 3 此用例即红。"""
    a = make_agent(tmp_path, ["http://bad", "http://good"])
    state = {"rotated": 0}
    real_rotate = a._rotate_server

    async def fake_sync(self):
        if self.server == "http://bad":
            raise ConnectionError("首选不可达")

    def spy_rotate(self, reason):        # 同步：_rotate_server 是普通方法，不是协程
        state["rotated"] += 1
        real_rotate(reason)

    async def no_report(self):
        return

    monkeypatch.setattr(Agent, "sync_once", fake_sync)
    monkeypatch.setattr(Agent, "_rotate_server", spy_rotate)
    monkeypatch.setattr(Agent, "report_once", no_report)

    async def scenario():
        task = asyncio.create_task(a.run())
        # sync 失败后退避 2s→4s，第二次失败在 ~2s 后才到；给足 6s 窗口
        for _ in range(60):
            await asyncio.sleep(0.1)
            if state["rotated"]:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert state["rotated"] == 1, f"应恰好切换一次（实际 {state['rotated']}）"
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
