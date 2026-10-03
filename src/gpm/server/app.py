"""服务端装配：FastAPI app、后台 sweep（离线判定/聚合/保留策略）。"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .. import __version__
from ..common.util import now
from . import jsonlog
from .api_agent import setup_router as agent_router
from .api_web import setup_router as web_router
from .incidents import IncidentMachine
from .ingest import Ingest
from .storage import BUCKET_SECONDS, Storage

log = logging.getLogger("gpm.server")

# 应用元信息（页脚/接口展示用；与 pyproject.toml 的 authors/urls 保持一致）
AUTHOR = "ops120"
REPO = "https://github.com/ops120/globe-probe"


def create_app(cfg, storage: Storage | None = None):
    storage = storage or Storage(cfg.server["database"])
    # public_url 以配置文件为准（启动时写入 settings）。历史缺陷：这个键只有 setting_get
    # 一条来源、且没有任何地方写过它 —— 没有 config 键也没有接口，等于线上配不了，
    # 于是每条通知都没有【链接】段落（.docs/ONCALL_OPTIMIZATION_2.md 第三期 12）。
    try:
        if str(cfg.server.get("public_url") or "").strip():
            storage.setting_set("public_url", str(cfg.server["public_url"]).strip())
    except Exception as e:  # noqa: BLE001 - 配置写入失败不影响起服务
        log.warning("public_url 写入设置失败: %s", e)
    machine = IncidentMachine(storage, cfg.probe.get("fail_threshold", 3),
                              cfg.probe.get("recover_threshold", 2),
                              cfg.probe.get("flap_window_seconds", 600),
                              cfg.probe.get("flap_max_seconds", 21600))
    ingest = Ingest(storage, machine, cfg.server)
    state = {"storage": storage, "cfg": cfg, "ingest": ingest,
             "register_token": cfg.agent.get("register_token", "gpm-dev-register")}

    # 结构化日志开关（GPM_LOG_JSON=1，默认关闭保持原有纯文本行为）。这里与 lifespan
    # 各调一次是故意的：uvicorn.run() 在 create_app 之后才应用自己的日志配置，
    # lifespan（真正起服务时）再接一次才能覆盖 uvicorn 自身的启动/访问日志。
    jsonlog.setup()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        # 重启后重建事件状态机的内存视图。没有这一步，内存里所有 incident_id 都是 None，
        # 「重启后恢复」的流会留下永远关不掉的僵尸事件（.docs/ONCALL_OPTIMIZATION_2.md 根因 1.1）。
        try:
            n = machine.rebuild_all()
            if n:
                log.info("事件状态重建：%d 条仍有未恢复事件的流", n)
        except Exception as e:  # noqa: BLE001 - 重建失败不影响起服务
            log.error("事件状态重建失败: %s", e)
        tasks = [
            asyncio.create_task(_sweep_loop(state, stop)),
            asyncio.create_task(_agg_loop(state, stop)),
            asyncio.create_task(_retention_loop(state, stop)),
            asyncio.create_task(_alert_loop(state, stop)),
            asyncio.create_task(_retry_loop(state, stop)),
            asyncio.create_task(_digest_loop(state, stop)),
            asyncio.create_task(_channel_probe_loop(state, stop)),
            asyncio.create_task(_selfcheck_loop(state, stop)),
            asyncio.create_task(_pull_loop(state, stop)),
        ]
        # AnyIO 线程池上限：默认虽是 40，但显式收敛到配置值 —— 曾因线程爆发
        # MemoryError 假死，health 的 threads 字段应稳定在 tokens 附近，超出即异常。
        try:
            import anyio.to_thread
            tokens = int(cfg.server.get("thread_pool_tokens", 40) or 40)
            anyio.to_thread.current_default_thread_limiter().total_tokens = max(1, tokens)
        except Exception as e:  # noqa: BLE001 - 调整失败不影响启动
            log.warning("线程池上限设置失败（保持 AnyIO 默认）: %s", e)
        # systemd sd_notify 看门狗：仅 NOTIFY_SOCKET 存在时启用（见 deploy/gpm-server.service）
        try:
            from . import sdwatch
            tasks += sdwatch.spawn_tasks(stop)
        except Exception as e:  # noqa: BLE001 - sd_notify 失败绝不影响主流程
            log.debug("sd_notify 未启用: %s", e)
        jsonlog.setup()   # 见 create_app 顶部说明：覆盖 uvicorn.run 重置过的 handler
        log.info("服务端启动: %s (db=%s)", cfg.server["listen"], cfg.server["database"])
        yield
        stop.set()
        for t in tasks:
            t.cancel()
        log.info("服务端停止")

    app = FastAPI(title="gpm · 全球拨测监控平台", version="0.1.0", lifespan=lifespan)
    app.include_router(agent_router(state))
    app.include_router(web_router(state))

    @app.get("/api/health")
    async def health():
        """存活探测。**故意用 async def**：不占用 AnyIO 线程池，也不碰 DB/锁。

        这样即使线程池被拖住/耗尽（曾经因为线程启动 MemoryError 导致「假死」），
        这个接口仍会秒回 —— 界面与运维脚本才能区分「进程挂了」与「线程池卡住」。
        """
        return {"ok": True, "time": now(), "config_version": storage.config_version(),
                "version": __version__, "author": AUTHOR, "repo": REPO,
                "threads": threading.active_count()}

    @app.middleware("http")
    async def audit_middleware(request, call_next):
        """写操作留痕：谁（admin/本机）/ 何时 / 改了什么（中文动作）/ 结果。

        只记 /api/* 的写请求，节点侧上报接口（/api/agent/*）不记（量大且语义固定）。
        """
        resp = await call_next(request)
        try:
            from . import audit
            if audit.is_mutating(request.method, request.url.path):
                who = "admin" if request.headers.get("x-admin-token") else "本机"
                fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                ip = fwd or (request.client.host if request.client else "")
                audit.record(storage, method=request.method, path=request.url.path,
                             status=resp.status_code, who=who, ip=ip, ts=now(),
                             detail=request.url.path)
        except Exception as e:  # noqa: BLE001 - 审计失败绝不影响业务
            log.debug("审计中间件跳过: %s", e)
        return resp

    @app.get("/metrics")
    def prometheus_metrics():
        """Prometheus 文本格式指标（便于接入既有监控栈；失败时也返回可解析的注释行）。"""
        try:
            from . import metrics
            body = metrics.render(storage, ingest)
        except Exception as e:  # noqa: BLE001 - 指标不可用不应影响服务
            body = "# gpm metrics unavailable: " + type(e).__name__ + ": " + str(e)[:200] + "\n"
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        storage.log_error(now(), "server", "unhandled", f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content={"detail": f"内部错误: {exc}"})

    webui = Path(cfg.server.get("webui_dir") or (Path(__file__).parent.parent / "webui" / "static"))

    @app.get("/")
    def index():
        return FileResponse(webui / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/index.html")
    def index_html():
        """通知深链用的是 {public_url}/index.html?task=..&ts=..

        原先只路由了 "/"，于是「点击查看」链接一直是 **404**（线上实测），而验收脚本
        又特意在 /index.html 404 时退回 "/" 继续断言 —— 两边都没发现问题。
        这里把 /index.html 补上：既让通知里的链接可用，也让**已经发出去的历史通知**
        重新可点（.docs/ONCALL_OPTIMIZATION_2.md 第三期 11）。
        """
        return FileResponse(webui / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{name}")
    def static_file(name: str):
        p = (webui / name).resolve()
        if not str(p).startswith(str(webui.resolve())) or not p.exists():
            return JSONResponse(status_code=404, content={"detail": "not found"})
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    return app


# 渠道自检「上次尝试」内存账本（进程级）：失败渠道的 last_ok_at 不会被推进，
# 靠这里节流，避免每个轮询周期都给挂掉的渠道发探针（也就不会轰炸 IM 群）。
_SELFCHECK_ATTEMPTS: dict[str, int] = {}


def channel_selfcheck_tick(s: Storage, ts: int | None = None, sender=None,
                           attempts: dict[str, int] | None = None) -> list[dict]:
    """通知渠道周期自检一轮（ONCALL_OPTIMIZATION.md 第三期 10：渠道静默失效自证）。

    - 周期：settings.channel_selfcheck_minutes（默认 10 分钟，0=关闭该功能）；
    - 对 enabled 且到期的渠道调 notify.send 发一条静默探针（只调用 notify 的公开
      send，不改通知模块）；成功/失败都经 storage.channel_touch 落到
      last_ok_at / last_error（/metrics 的 gpm_notify_channel_up 依据它判断）；
    - 到期判定：ts - max(last_ok_at, 上次尝试时间) >= 周期——「自检成功过」与
      「刚尝试过（哪怕失败）」都会让渠道安静一个周期；
    - 任何异常只记日志/转失败结果，绝不影响主流程。

    返回本轮实际探测的渠道结果 [{channel_id, name, ok, detail}]（测试/排障用）。
    """
    ts = int(ts or now())
    try:
        minutes = int(s.setting_get("channel_selfcheck_minutes", "10") or 10)
    except Exception as e:  # noqa: BLE001
        log.debug("channel_selfcheck_minutes 读取失败，按默认 10: %s", e)
        minutes = 10
    if minutes <= 0:
        return []
    book = _SELFCHECK_ATTEMPTS if attempts is None else attempts
    out: list[dict] = []
    for ch in s.list_channels():
        if not ch.get("enabled"):
            continue
        cid = str(ch.get("id") or "")
        if not cid:
            continue
        last_ok = int(ch.get("last_ok_at") or 0)
        if ts - max(last_ok, int(book.get(cid, 0))) < minutes * 60:
            continue
        book[cid] = ts
        title = "【自检】通知渠道探活"
        text = ("gpm 周期自检探针（静默消息）\n- 渠道: " + str(ch.get("name") or cid)
                + "\n- 时间: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                + "\n收到本条即表示该渠道当前可用。")
        flat = {"type": ch.get("type"), **(ch.get("config") or {})}
        try:
            if sender is not None:
                ok, msg = sender(flat, title, text)
            else:
                from . import notify  # 延迟导入：与 alerting/事件路径同款约束
                ok, msg = notify.send(flat, title, text)
            ok, msg = bool(ok), str(msg)
        except Exception as e:  # noqa: BLE001 - 发送异常按失败处理
            ok, msg = False, f"{type(e).__name__}: {e}"
        try:
            s.channel_touch(cid, ok, "" if ok else msg[:200], ts)
        except Exception as e:  # noqa: BLE001
            log.error("渠道自检结果写回失败 (%s): %s", cid, e)
        out.append({"channel_id": cid, "name": str(ch.get("name") or cid),
                    "ok": ok, "detail": msg})
    return out


async def _channel_probe_loop(state: dict, stop: asyncio.Event):
    """渠道自检循环：每 60s 扫一次到期渠道（自检周期由 channel_selfcheck_minutes 控制）。"""
    s: Storage = state["storage"]
    interval = int(state["cfg"].server.get("channel_selfcheck_interval", 60) or 60)
    while not stop.is_set():
        try:
            # notify.send 是阻塞网络调用 → 丢线程池，别卡事件循环
            results = await asyncio.to_thread(channel_selfcheck_tick, s)
            for r in results:
                (log.info if r["ok"] else log.warning)(
                    "渠道自检 %s(%s): %s", r["name"], r["channel_id"], r["detail"])
        except Exception as e:  # noqa: BLE001 - 自检失败绝不影响主流程
            log.error("渠道自检失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _pull_loop(state: dict, stop: asyncio.Event):
    """第三方告警 API 拉取（第六期 26）：每 60s 检查一次到期来源。

    只拉「已启用 + 该来源支持 + 配了地址 + 过了退避」的；默认全关，不会自己去打外部接口。
    未实现的两家（腾讯云/GCP）在 due_sources 里就被过滤掉了，不会空转。
    """
    s: Storage = state["storage"]
    while not stop.is_set():
        try:
            from . import pullers
            for src in pullers.due_sources(s, now()):
                r = pullers.poll_source(s, src)
                if r.get("error"):
                    log.warning("拉取 %s 失败：%s", src, r["error"])
                elif r.get("fetched"):
                    log.info("拉取 %s：%d 条（新增 %d 更新 %d）",
                             src, r["fetched"], r["created"], r["updated"])
        except Exception as e:  # noqa: BLE001 - 拉取失败不影响主流程
            log.error("第三方告警拉取失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def _sweep_loop(state: dict, stop: asyncio.Event):
    s: Storage = state["storage"]
    while not stop.is_set():
        try:
            n = s.sweep_offline(state["cfg"].server.get("heartbeat_timeout", 60), now())
            if n:
                log.warning("%d 个节点心跳超时，标记离线", n)
        except Exception as e:  # noqa
            log.error("离线 sweep 失败: %s", e)
        try:
            # 陈旧事件自动收口：「沉默 ≠ 故障」。任务停用/节点移除后不再产生结果，
            # 状态机永远等不到 ok，事件会在值班页上无限期显示「已持续 N 小时」。
            stale = int(state["cfg"].probe.get("stale_after_seconds", 21600) or 0)
            closed = s.close_stale_incidents(now(), stale)
            if closed:
                log.warning("陈旧事件自动收口 %d 条（>%ds 无新样本）", len(closed), stale)
        except Exception as e:  # noqa
            log.error("陈旧事件 sweep 失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


async def _alert_loop(state: dict, stop: asyncio.Event):
    """告警规则评估循环：默认每 30s 一轮（可在 server.alert_eval_interval 调整）。"""
    s: Storage = state["storage"]
    interval = int(state["cfg"].server.get("alert_eval_interval", 30) or 30)
    while not stop.is_set():
        try:
            from . import alerting
            events = alerting.evaluate(s, now())
            if events:
                log.warning("告警评估产生 %d 条事件（firing/remind/resolved）", len(events))
        except Exception as e:  # noqa
            log.error("告警评估失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _selfcheck_loop(state: dict, stop: asyncio.Event):
    """每 60s 记录一次内存与线程数：出现「假死」时可回看是内存压力还是线程池耗尽。"""
    interval = 60
    while not stop.is_set():
        try:
            rss = -1.0
            try:
                import psutil  # 可选依赖（agent 的 stats extra 里带）
                rss = psutil.Process().memory_info().rss / 1048576.0
            except Exception:  # noqa: BLE001 - 没装 psutil 就只记线程数
                pass
            log.info("selfcheck: rss=%s threads=%d", ("%.1f MB" % rss) if rss > 0 else "n/a",
                     threading.active_count())
        except Exception as e:  # noqa: BLE001
            log.debug("selfcheck 失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _retry_loop(state: dict, stop: asyncio.Event):
    """通知重投循环：把派发失败的通知按退避重试（默认每 60s 扫一次队列）。"""
    s: Storage = state["storage"]
    interval = int(state["cfg"].server.get("notify_retry_interval", 60) or 60)
    while not stop.is_set():
        try:
            from . import alerting
            done = alerting.retry_pending(s, now(), limit=10)
            if done:
                log.info("通知重投：%d 条（成功 %d）", len(done),
                         sum(1 for d in done if d["status"] == "done"))
        except Exception as e:  # noqa: BLE001
            log.error("通知重投失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _digest_loop(state: dict, stop: asyncio.Event):
    """定时巡检报告：按 settings 里的开关与间隔推送（默认每 5 分钟检查一次是否到期）。"""
    s: Storage = state["storage"]
    interval = int(state["cfg"].server.get("digest_check_interval", 300) or 300)
    while not stop.is_set():
        try:
            if s.setting_get("digest_enabled", "0") == "1":
                hours = int(s.setting_get("digest_interval_hours", "24") or 24)
                last = int(s.setting_get("digest_last_ts", "0") or 0)
                if now() - last >= max(1, hours) * 3600:
                    from . import alerting
                    ids = [c for c in (s.setting_get("digest_channel_ids", "") or "").split(",") if c]
                    r = alerting.push_digest(s, hours, ids, now())
                    log.warning("巡检报告已推送：%s（渠道 %d/%d 成功）", r["title"], r["ok"], r["channels"])
        except Exception as e:  # noqa: BLE001
            log.error("巡检报告推送失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _agg_loop(state: dict, stop: asyncio.Event):
    s: Storage = state["storage"]
    while not stop.is_set():
        try:
            t = now()
            for bucket in ("1m", "5m", "1h", "1d"):
                step = BUCKET_SECONDS[bucket]
                cursor = int(s.meta_get(f"agg_cursor_{bucket}", "0"))
                complete_to = (t - step) // step * step  # 已完结的最后一个桶起点
                if cursor == 0:
                    cursor = complete_to  # 首次只聚合当前
                b_from = (cursor // step) * step
                if complete_to < b_from:
                    continue
                # 重算窗口向前多覆盖一个桶（迟到数据窗口）
                b_from = max(0, b_from - step)
                s.agg_recompute(bucket, b_from, complete_to + step)
                s.meta_set(f"agg_cursor_{bucket}", str(complete_to))
        except Exception as e:  # noqa
            log.error("聚合 sweep 失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass


async def _retention_loop(state: dict, stop: asyncio.Event):
    s: Storage = state["storage"]
    srv = state["cfg"].server
    while not stop.is_set():
        try:
            n = s.retention(srv.get("retention_raw_days", 30), srv.get("retention_1m_days", 90),
                            srv.get("retention_5m_days", 180), srv.get("retention_1h_days", 730),
                            srv.get("retention_hb_days", 7), now(),
                            alerts_days=srv.get("retention_alerts_days", 30),
                            audit_days=srv.get("retention_audit_days", 30),
                            outbox_days=srv.get("retention_outbox_days", 7),
                            incidents_days=srv.get("retention_incidents_days", 180),
                external_days=srv.get("retention_external_days", 30))
            s.meta_set("last_retention", str(now()))
            if any(n.values()):
                log.info("保留策略清理完成: %s", n)
        except Exception as e:  # noqa
            log.error("保留策略清理失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
