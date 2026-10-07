"""systemd sd_notify 看门狗（纯标准库，无依赖）：Type=notify 服务的心跳上报。

只做两件事：

- READY=1：服务装配完成时发一次（systemd 收到后才把服务标记为 active）；
- WATCHDOG=1：按 WatchdogSec/2 的周期上报「还活着」，超时未上报 systemd 会
  判定卡死并按 Restart= 拉起（配合 deploy/gpm-server.service 使用）。

约定：NOTIFY_SOCKET 环境变量不存在（非 systemd 环境/Windows 调试）时全部
接口静默不动作；任何发送失败只记 debug 日志，绝不抛异常、绝不影响主流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

__all__ = ["available", "watchdog_interval", "send", "notify", "notify_ready",
           "watchdog_loop", "spawn_tasks", "MIN_INTERVAL"]

log = logging.getLogger("gpm.sdwatch")

# 看门狗上报周期下限（秒）：WatchdogSec/2 再小没有意义，反而放大抖动风险
MIN_INTERVAL = 2.0


def available() -> bool:
    """是否运行在 systemd Type=notify 环境下（systemd 会注入 NOTIFY_SOCKET）。"""
    return bool(os.environ.get("NOTIFY_SOCKET"))


def socket_path() -> str:
    """NOTIFY_SOCKET 的值；'@' 开头是 Linux 抽象套接字，替换成 '\\0' 前缀。"""
    p = os.environ.get("NOTIFY_SOCKET", "")
    if p.startswith("@"):
        p = "\0" + p[1:]
    return p


def watchdog_interval(usec=None) -> float:
    """看门狗上报周期：WatchdogSec/2（systemd 经 WATCHDOG_USEC 告知），下限 2s。

    未设置 / 非法 / <=0 返回 0.0，表示不启用周期上报。
    """
    raw = os.environ.get("WATCHDOG_USEC", "") if usec is None else usec
    try:
        sec = int(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0
    if sec <= 0:
        return 0.0
    return max(MIN_INTERVAL, sec / 2.0)


def send(path: str, message: str) -> bool:
    """往 sd_notify 的 UNIX 数据报套接字发一条消息；任何失败都返回 False。"""
    if not path or not message:
        return False
    family = getattr(socket, "AF_UNIX", None)
    if family is None:  # Windows 官方 CPython 无 AF_UNIX：sd_notify 只在 Linux 有意义
        log.debug("sd_notify 不可用：本平台无 AF_UNIX")
        return False
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode("utf-8"), path)
        return True
    except Exception as e:
        log.debug("sd_notify 发送失败: %s", e)
        return False


def notify(message: str) -> bool:
    """按 NOTIFY_SOCKET 环境变量发送；未启用时返回 False。"""
    if not available():
        return False
    return send(socket_path(), message)


def notify_ready() -> bool:
    """启动完成时上报一次 READY=1。"""
    return notify("READY=1")


async def watchdog_loop(interval: float, stop: asyncio.Event, sender=None):
    """周期上报 WATCHDOG=1，直到 stop 置位；interval<=0 直接返回。"""
    send_fn = sender or notify
    if interval <= 0:
        return
    while not stop.is_set():
        try:
            if not send_fn("WATCHDOG=1"):
                log.debug("WATCHDOG=1 上报失败（NOTIFY_SOCKET 不可用？）")
        except Exception as e:
            log.debug("WATCHDOG 上报异常: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def spawn_tasks(stop: asyncio.Event) -> list:
    """NOTIFY_SOCKET 存在时，返回要挂进 lifespan 的任务列表（READY=1 + 看门狗）。

    返回空列表表示未启用；调用方（app.lifespan）已整体 try/except 兜底。
    """
    if not available():
        return []
    interval = watchdog_interval()
    notify_ready()
    tasks: list = []
    if interval > 0:
        tasks.append(asyncio.create_task(watchdog_loop(interval, stop)))
        log.info("systemd 看门狗已启用: 每 %.1fs 上报 WATCHDOG=1", interval)
    else:
        log.info("sd_notify READY=1 已上报（未设置 WATCHDOG_USEC，跳过看门狗心跳）")
    return tasks
