"""服务端装配：FastAPI app、后台 sweep（离线判定/聚合/保留策略）。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .. import __version__
from ..common.util import now
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
    machine = IncidentMachine(storage, cfg.probe.get("fail_threshold", 3),
                              cfg.probe.get("recover_threshold", 2))
    ingest = Ingest(storage, machine, cfg.server)
    state = {"storage": storage, "cfg": cfg, "ingest": ingest,
             "register_token": cfg.agent.get("register_token", "gpm-dev-register")}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(_sweep_loop(state, stop)),
            asyncio.create_task(_agg_loop(state, stop)),
            asyncio.create_task(_retention_loop(state, stop)),
        ]
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
    def health():
        return {"ok": True, "time": now(), "config_version": storage.config_version(),
                "version": __version__, "author": AUTHOR, "repo": REPO}

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        storage.log_error(now(), "server", "unhandled", f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content={"detail": f"内部错误: {exc}"})

    webui = Path(cfg.server.get("webui_dir") or (Path(__file__).parent.parent / "webui" / "static"))

    @app.get("/")
    def index():
        return FileResponse(webui / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{name}")
    def static_file(name: str):
        p = (webui / name).resolve()
        if not str(p).startswith(str(webui.resolve())) or not p.exists():
            return JSONResponse(status_code=404, content={"detail": "not found"})
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    return app


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
            await asyncio.wait_for(stop.wait(), timeout=15)
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
            s.retention(srv.get("retention_raw_days", 30), srv.get("retention_1m_days", 90),
                        srv.get("retention_5m_days", 180), srv.get("retention_1h_days", 730),
                        srv.get("retention_hb_days", 7), now())
            s.meta_set("last_retention", str(now()))
        except Exception as e:  # noqa
            log.error("保留策略清理失败: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
