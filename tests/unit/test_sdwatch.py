"""tests/unit/test_sdwatch.py —— systemd sd_notify 看门狗单测（mock socket/env，不依赖 systemd）。

守护 sdwatch 的对外契约：

1. available()/socket_path() 只认 NOTIFY_SOCKET（'@' 前缀 = Linux 抽象套接字）；
2. watchdog_interval() = WATCHDOG_USEC/2、下限 2s；缺失/非法/<=0 → 0.0（不启用）；
3. send() 对任何失败（无 AF_UNIX/套接字失效/空参数）只返回 False，绝不抛异常；
4. watchdog_loop 周期发 WATCHDOG=1 直到 stop 置位；interval<=0 直接返回；
5. spawn_tasks 仅在 NOTIFY_SOCKET 存在时返回任务，READY=1 总是先发一次。
"""
from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from gpm.server import sdwatch


# ---------------------------------------------------------------- env 语义

def test_available_and_socket_path(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdwatch.available() is False
    assert sdwatch.socket_path() == ""

    monkeypatch.setenv("NOTIFY_SOCKET", "/run/gpm.sock")
    assert sdwatch.available() is True
    assert sdwatch.socket_path() == "/run/gpm.sock"

    # systemd 抽象套接字：'@' 前缀替换为 '\0'
    monkeypatch.setenv("NOTIFY_SOCKET", "@/gpm.sock")
    assert sdwatch.socket_path() == "\0/gpm.sock"


# ---------------------------------------------------------------- 看门狗周期

def test_watchdog_interval_from_usec(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")     # WatchdogSec=30 -> 15s
    assert sdwatch.watchdog_interval() == 15.0
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")      # 2s -> 下限钳到 2s
    assert sdwatch.watchdog_interval() == 2.0
    monkeypatch.setenv("WATCHDOG_USEC", "1000000")      # 0.5s -> 仍然 >= 2s
    assert sdwatch.watchdog_interval() == 2.0

    for bad in ("", "abc", "0", "-5", None):
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        if bad is not None:
            monkeypatch.setenv("WATCHDOG_USEC", bad)
        assert sdwatch.watchdog_interval() == 0.0, bad


def test_watchdog_interval_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    assert sdwatch.watchdog_interval(4_000_000) == 2.0
    assert sdwatch.watchdog_interval(0) == 0.0


# ---------------------------------------------------------------- send / notify

class FakeSocket:
    """替身 socket.socket：记录 sendto 调用，不真发网络包。"""

    sent: list = []
    fail = False

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendto(self, data, addr):
        if FakeSocket.fail:
            raise OSError("dgram socket dead")
        FakeSocket.sent.append((bytes(data), addr))


class FakeSocketModule:
    """替身 socket 模块：只提供 send() 用到的名字。

    注意：绝不能 monkeypatch 全局 socket.socket —— Windows 的 asyncio proactor
    事件循环建 loop 时就要造 socket，会被一起替换掉，导致收尾时诡异崩溃。
    """

    AF_UNIX = 1
    SOCK_DGRAM = 2
    socket = FakeSocket


@pytest.fixture()
def fake_socket(monkeypatch):
    FakeSocket.sent = []
    FakeSocket.fail = False
    monkeypatch.setattr(sdwatch, "socket", FakeSocketModule)
    return FakeSocket


def test_send_encodes_message_and_path(fake_socket):
    assert sdwatch.send("/run/gpm.sock", "READY=1") is True
    assert fake_socket.sent == [(b"READY=1", "/run/gpm.sock")]


def test_send_swallows_errors(fake_socket, tmp_path):
    fake_socket.fail = True
    assert sdwatch.send("/run/gpm.sock", "WATCHDOG=1") is False
    # 空 path / 空 message 连 socket 都不该碰
    fake_socket.fail = False
    assert sdwatch.send("", "READY=1") is False
    assert sdwatch.send("/run/gpm.sock", "") is False
    assert fake_socket.sent == []


def test_notify_requires_env(fake_socket, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdwatch.notify_ready() is False
    assert fake_socket.sent == []

    monkeypatch.setenv("NOTIFY_SOCKET", "@/gpm.sock")
    assert sdwatch.notify_ready() is True
    assert fake_socket.sent == [(b"READY=1", "\0/gpm.sock")]


def test_send_real_unix_socket(tmp_path):
    """真发一次数据报（仅 AF_UNIX 平台；Windows 官方 CPython 会 skip）。"""
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX not available on this platform")
    p = tmp_path / "notify.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(p))
    try:
        assert sdwatch.send(str(p), "READY=1") is True
        srv.settimeout(2)
        assert srv.recv(100) == b"READY=1"
    finally:
        srv.close()


def test_send_to_dead_path_returns_false(tmp_path):
    # 无 AF_UNIX 的平台在 socket 构造处抛 AttributeError，同样应被吞成 False
    assert sdwatch.send(str(tmp_path / "nope.sock"), "READY=1") is False


# ---------------------------------------------------------------- watchdog_loop

def test_watchdog_loop_sends_until_stop():
    sends = []

    async def main():
        stop = asyncio.Event()

        def sender(msg):
            sends.append(msg)
            if len(sends) >= 3:
                stop.set()
            return True

        await asyncio.wait_for(sdwatch.watchdog_loop(0.01, stop, sender=sender), timeout=5)

    asyncio.run(main())
    assert len(sends) == 3 and all(m == "WATCHDOG=1" for m in sends)


def test_watchdog_loop_non_positive_interval_is_noop():
    called = []

    async def main():
        await sdwatch.watchdog_loop(0, asyncio.Event(), sender=called.append)

    asyncio.run(main())
    assert called == []


def test_watchdog_loop_survives_sender_exception():
    sends = []

    async def main():
        stop = asyncio.Event()

        def sender(msg):
            sends.append(msg)
            if len(sends) >= 2:
                stop.set()
            raise RuntimeError("boom")

        await asyncio.wait_for(sdwatch.watchdog_loop(0.01, stop, sender=sender), timeout=5)

    asyncio.run(main())
    assert len(sends) == 2  # sender 抛异常不终止循环，也不外泄


# ---------------------------------------------------------------- spawn_tasks

def test_spawn_tasks_disabled_without_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    async def main():
        assert sdwatch.spawn_tasks(asyncio.Event()) == []

    asyncio.run(main())


def test_spawn_tasks_with_watchdog(monkeypatch, fake_socket):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/gpm.sock")
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")

    async def main():
        stop = asyncio.Event()
        tasks = sdwatch.spawn_tasks(stop)
        assert len(tasks) == 1, "WATCHDOG_USEC 存在时应返回 1 个看门狗任务"
        await asyncio.sleep(0.05)
        stop.set()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    asyncio.run(main())
    msgs = [m for m, _ in fake_socket.sent]
    assert msgs[0] == b"READY=1", "READY=1 必须先于 WATCHDOG=1"
    assert b"WATCHDOG=1" in msgs


def test_spawn_tasks_ready_only_without_watchdog_usec(monkeypatch, fake_socket):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/gpm.sock")
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)

    async def main():
        stop = asyncio.Event()
        assert sdwatch.spawn_tasks(stop) == [], "未设置 WATCHDOG_USEC 时不应启看门狗任务"

    asyncio.run(main())
    assert [m for m, _ in fake_socket.sent] == [b"READY=1"]


# ---------------------------------------------------------------- app 装配（lifespan）

def _make_client(tmp_path, extra=None):
    from fastapi.testclient import TestClient

    from gpm.config import Config
    from gpm.server.app import create_app

    srv = {"database": str(tmp_path / "sdwatch-app.db"), "listen": "127.0.0.1:0"}
    srv.update(extra or {})
    app = create_app(Config({"server": srv}))
    return TestClient(app)


def test_app_lifespan_spawns_watchdog_when_notify_socket_set(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/gpm.sock")
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    sent = []
    monkeypatch.setattr(sdwatch, "send", lambda p, m: sent.append(m) or True)

    client = _make_client(tmp_path)
    with client:
        assert client.get("/api/health").json()["ok"] is True
    assert sent and sent[0] == "READY=1", "启动完成必须先上报 READY=1"
    assert "WATCHDOG=1" in sent, "WATCHDOG_USEC 存在时 lifespan 应挂上看门狗循环"


def test_app_lifespan_quiet_without_notify_socket(monkeypatch, tmp_path):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    sent = []
    monkeypatch.setattr(sdwatch, "send", lambda p, m: sent.append(m) or True)

    client = _make_client(tmp_path)
    with client:
        assert client.get("/api/health").json()["ok"] is True
    assert sent == [], "非 systemd 环境（无 NOTIFY_SOCKET）不应发送任何 sd_notify"


def test_app_applies_thread_pool_tokens(tmp_path):
    anyio = pytest.importorskip("anyio")
    client = _make_client(tmp_path, {"thread_pool_tokens": 7})
    with client:
        # TestClient 的 portal 就是应用的 event loop：在 loop 线程里读 limiter
        try:
            portal = client.portal
        except AttributeError:  # pragma: no cover - starlette 老版本无 portal
            pytest.skip("TestClient has no portal attribute")
        limiter = portal.call(anyio.to_thread.current_default_thread_limiter)
        assert limiter.total_tokens == 7, "lifespan 应按 server.thread_pool_tokens 收敛线程池"
