"""Web API：任务管理、节点、查询（条带/曲线/对比/明细/事件）、导出。"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..common.models import NodeUpdate, TaskCreate, TaskUpdate, validate_params
from ..common.util import new_id, now, sha256, validate_domain, validate_host_port, \
    validate_target
from . import alerting, geo
from .diagnose import classify, verdict
from .storage import BUCKET_SECONDS

log = logging.getLogger("gpm.web")


def _interval_floor(probe: dict, task_type: str) -> int:
    """按任务类型的间隔下限：mtr 60s、dns 30s、其余（ping/curl/tcp）10s。"""
    if task_type == "mtr":
        return probe.get("min_mtr_interval_seconds", 60)
    if task_type == "dns":
        return probe.get("min_dns_interval_seconds", 30)
    return probe.get("min_interval_seconds", 10)


def _target_valid(task_type: str, target: str) -> bool:
    """类型相关目标校验：curl 看 urls（target 任意/空）；tcp 要 host:port；dns 要域名/IP。"""
    if task_type == "curl":
        return True
    if task_type == "tcp":
        return validate_host_port(target or "")
    if task_type == "dns":
        return validate_domain(target or "")
    return validate_target(target or "")


def _oncall_bucket(task: dict, node: dict, last_status, last_age_s,
                    stale_after: int) -> str:
    """值班卡片分档：让第一屏只显示「现在值不值得动手」的东西。

    live   正在失败：有新鲜失败样本（或节点确实离线）
    silent 沉默待确认：样本已过期，但还没到陈旧阈值——可能只是任务被停用/节点刚掉线
    stale  陈旧待收口：无样本或已超 stale_after，正常应被 sweep 自动收口

    「沉默 ≠ 故障」是这一档存在的理由：事件开着不等于现在还在坏，值班的人需要一眼
    看出「这张卡是新鲜的，还是我们只是没再收到样本」。
    """
    if not task:
        # 节点侧事件：离线就是真在坏；其余（不应出现）按陈旧处理
        return "live" if str(node.get("status")) == "offline" else "stale"
    if last_status == "fail" and last_age_s is not None:
        fresh_window = max(300, int(task.get("interval_seconds") or 60) * 3)
        if last_age_s <= fresh_window:
            return "live"
    if last_age_s is None or (stale_after and last_age_s > stale_after):
        return "stale"
    return "silent"


def setup_router(app_state) -> APIRouter:
    s = app_state["storage"]
    cfg = app_state["cfg"]
    router = APIRouter(prefix="/api")  # 每次调用独立 router，避免跨 app 闭包污染

    def check_write(x_admin_token: str | None):
        token = cfg.server.get("admin_token") or ""
        if token and x_admin_token != token:
            raise HTTPException(403, "需要 X-Admin-Token")

    # ---------- 任务 ----------
    # /api/tasks 进程内 TTL 缓存（欠账-4）：该接口逐任务算 streams + 24h 可用率（逐流 SQL），
    # 实测 1.7~2.3s。缓存 key=config_version：任务增删改/节点分配/分组变化都会
    # _bump_config_version() → 下一次请求立即重算；TTL 内重复请求直接返回缓存的同一
    # JSON 结构。?fresh=1 绕过缓存（测试/排障用）。
    # 口径说明：缓存的 avail_24h / streams / current_status 是「计算时刻」的值，
    # 命中缓存期间最多陈旧 tasks_cache_seconds 秒（期间新产生的探测数据不实时反映，
    # 15s 内的口径漂移对运维展示可接受；config_version 变更不受 TTL 影响立即失效）。
    tasks_cache_seconds = max(0, int(cfg.server.get("tasks_cache_seconds", 15) or 0))
    _tasks_cache: dict = {"ver": None, "at": 0.0, "data": None}
    _tasks_lock = threading.Lock()

    def _compute_tasks() -> list:
        tasks = s.list_tasks()
        out = []
        for t in tasks:
            d = dict(t)
            streams = s.result_streams(t["id"])
            # 最近状态：各流最近一条
            last_status, last_ts = None, 0
            ok_n = fail_n = total_n = 0
            day_ago = now() - 86400
            for st in streams:
                rows = s.agg_read("1m", t["id"], st["node_id"], st["dns"], st["url"],
                                  day_ago, now())
                total_n += sum(r["count"] for r in rows)
                ok_n += sum(r["ok"] for r in rows)
                for r in rows:
                    if r["ts"] > last_ts and r["count"]:
                        last_ts = r["ts"]
                        last_status = "ok" if r["avail_rate"] >= 1 else (
                            "fail" if r["avail_rate"] == 0 else "partial")
            d["streams"] = len(streams)
            d["avail_24h"] = round(ok_n / total_n, 4) if total_n else None
            # 没有任何聚合数据时，区分「采集不到」与「探测被跳过」（如 mtr 未安装），
            # 否则运维会把工具缺失误判成采集故障
            if last_status is None and streams and all(
                    st.get("latest_status") == "skipped" for st in streams):
                last_status = "skipped"
                newest = max(streams, key=lambda x: x.get("latest_ts") or 0)
                d["skip_reason"] = (newest.get("latest_error")
                                    or newest.get("latest_error_class") or "探测被跳过")
            d["current_status"] = last_status
            out.append(d)
        return out

    @router.get("/tasks")
    def list_tasks(fresh: int = 0):
        if tasks_cache_seconds <= 0 or fresh:
            return _compute_tasks()          # 关闭开关 / 显式绕过：行为与旧版一致
        ver = s.config_version()             # 内存缓存读取（无 DB/锁）
        t0 = time.monotonic()
        with _tasks_lock:
            c = _tasks_cache
            if c["data"] is not None and c["ver"] == ver and t0 - c["at"] < tasks_cache_seconds:
                return c["data"]
        data = _compute_tasks()              # 计算放锁外：并发未命中最多多算几次，结果一致
        with _tasks_lock:
            _tasks_cache.update(ver=ver, at=time.monotonic(), data=data)
        return data

    @router.post("/tasks")
    def create_task(body: TaskCreate, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        probe = cfg.probe
        interval = max(body.interval_seconds, _interval_floor(probe, body.type))
        tid = new_id("t")
        try:
            t = s.create_task(tid, body.name, body.type, body.target, body.urls, body.params,
                              body.dns, interval, now(), nodes=body.nodes)
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"任务名已存在: {body.name}")
            raise
        return t

    @router.put("/tasks/{tid}")
    def update_task(tid: str, body: dict, request: Request,
                    x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        try:
            fields = TaskUpdate(**body).model_dump(exclude_unset=True)
        except Exception as e:
            raise HTTPException(422, str(e))
        task = s.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        probe = cfg.probe
        ttype = task["type"]
        # 目标校验按任务类型：curl 以 urls 为准（target 可为空），tcp 要 host:port，
        # dns 要合法域名/IP，ping/mtr 必须是合法目标
        if "target" in fields and not _target_valid(ttype, fields["target"] or ""):
            raise HTTPException(422, f"非法目标: {fields['target']!r}")
        if "params" in fields:
            try:
                validate_params(ttype, fields["params"] or {})
            except ValueError as e:
                raise HTTPException(422, str(e))
        if isinstance(fields.get("interval_seconds"), int):
            fields["interval_seconds"] = max(fields["interval_seconds"],
                                             _interval_floor(probe, ttype))
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        try:
            updated = s.update_task(tid, fields, now())
        except KeyError:
            raise HTTPException(404, "任务不存在")
        except ValueError as e:
            raise HTTPException(422, str(e))
        # 启停是运维最关心的变更：在中间件的「修改任务」之外显式记一条中文动作。
        # 中间件的通用记录保留不动，这里只对「enabled 真实发生变化」的请求补记。
        if "enabled" in fields and task["enabled"] != fields["enabled"]:
            try:
                from . import audit
                act = "启用任务" if fields["enabled"] else "停用任务"
                who = "admin" if x_admin_token else "本机"
                audit.record(s, method=request.method, path=request.url.path,
                             status=200, who=who,
                             ip=request.client.host if request.client else "",
                             ts=now(), detail=f"{task['name']}: enabled "
                                              f"{task['enabled']} -> {fields['enabled']}",
                             action=act)
            except Exception as e:  # noqa: BLE001 - 审计失败绝不影响业务
                log.debug("启停审计跳过: %s", e)
        return updated

    @router.delete("/tasks/{tid}")
    def delete_task(tid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        s.delete_task(tid, now())
        return {"deleted": tid}

    # ---------- 节点 ----------
    @router.get("/nodes")
    def list_nodes():
        return s.list_nodes()

    # ---------- 节点资源时序（CPU/内存）----------
    @router.get("/nodes/metrics")
    def node_metrics(node_id: str = "", t_from: int = 0, t_to: int = 0, bucket: int = 300):
        t_to = t_to or now()
        t_from = t_from or (t_to - 86400)
        bucket = max(60, min(bucket, 86400))
        nodes = s.list_nodes()
        want = [n for n in nodes if not node_id or n["id"] == node_id]
        return {"from": t_from, "to": t_to, "bucket": bucket,
                "series": [{"node_id": n["id"], "node_name": n["name"],
                            "points": s.node_metrics(n["id"], t_from, t_to, bucket)}
                           for n in want]}

    # ---------- 节点能力矩阵（ONCALL_OPTIMIZATION.md 第三期 11）----------
    @router.get("/nodes/capabilities")
    def nodes_capabilities(hours: int = 24):
        """回答「这个任务为什么这个节点没数据」：从近 N 小时探测记录与心跳推断能力。

        推断口径（无记录=未知，绝不把「没看到」说成「不支持」）：
        - mtr / tracert：type='mtr' 行 metrics_json 的 mode=mtr/tracert 出现过 → true；
          窗口内**最新**的能力信号是 error_class=tool_missing → false（之后又有成功
          记录则覆盖为 true）；窗口内无路径探测记录 → null。
        - psutil：心跳 cpu/mem 有值 → true；有心跳但 cpu/mem 全空 → false；无心跳 → null。
        - ipv6：窗口内出现过 IPv6 解析结果（resolved_ip 含冒号）→ true；否则 null
          （没有 v6 样本可能只是没分到 v6 目标，不下 false 结论）。
        - os：节点注册上报的 system.os。
        """
        hours = max(1, min(int(hours or 24), 24 * 30))
        t0 = now() - hours * 3600
        # 各节点最新一条「能力信号」：tracert/mtr 出现过 或 工具缺失（按 ts 升序走，
        # 后到的信号覆盖先到的 → 即「最新信号优先」）
        with s.lock:
            rows = s.db.execute(
                "SELECT node_id, ts, error_class, metrics_json FROM probe_results"
                " WHERE type='mtr' AND ts>=? ORDER BY ts", (t0,)).fetchall()
        last_sig: dict[str, str] = {}
        for r in rows:
            if (r["error_class"] or "") == "tool_missing":
                last_sig[r["node_id"]] = "tool_missing"
                continue
            try:
                mode = str(json.loads(r["metrics_json"] or "{}").get("mode") or "")
            except ValueError:
                mode = ""
            if mode in ("mtr", "tracert"):
                last_sig[r["node_id"]] = mode
        # IPv6 实证：窗口内解析出过 IPv6 地址（IPv4 字面量不含冒号）
        with s.lock:
            v6 = {r["node_id"] for r in s.db.execute(
                "SELECT DISTINCT node_id FROM probe_results"
                " WHERE ts>=? AND resolved_ip LIKE '%:%'", (t0,)).fetchall()}

        def _cap(sig: str, wanted: str) -> bool | None:
            if sig == "tool_missing":
                return False            # 最新信号是工具缺失 → 该节点路径探测不可用
            return True if sig == wanted else None

        out = []
        for n in s.list_nodes():
            hb_seen = bool(n.get("last_heartbeat"))
            if n.get("cpu") is not None or n.get("mem") is not None:
                psutil_cap: bool | None = True
            elif hb_seen:
                psutil_cap = False     # 心跳里如实上报了「采不到」
            else:
                psutil_cap = None      # 没心跳，无从判断
            system = n.get("system") or {}
            sig = last_sig.get(n["id"], "")
            out.append({
                "node_id": n["id"], "name": n.get("name") or n["id"],
                "os": system.get("os") or None,
                "mtr": _cap(sig, "mtr"),
                "tracert": _cap(sig, "tracert"),
                "psutil": psutil_cap,
                "ipv6": True if n["id"] in v6 else None,
            })
        return out

    @router.get("/nodes/{nid}")
    def node_detail(nid: str):
        n = s.node_by_id(nid)
        if not n:
            raise HTTPException(404, "节点不存在")
        d = dict(n)
        d["tags"] = json.loads(d.pop("tags_json") or "{}")
        d["system"] = json.loads(d.pop("system_json") or "{}")
        t_now = now()
        tasks = s.list_tasks()
        assigned = [t for t in tasks
                    if not t["nodes"] or nid in t["nodes"] or n["name"] in t["nodes"]]
        d["assigned_tasks"] = [{"id": t["id"], "name": t["name"], "type": t["type"]}
                               for t in assigned if t["enabled"]]
        # 24h 可用率：该节点全部分配任务的所有线路/URL 流一起算
        d["avail_24h"] = s.node_avail(nid, t_now - 86400,
                                      [t["id"] for t in assigned])["avail"]
        d["recent_incidents"] = [i for i in s.list_incidents(10) if i["node_id"] == nid][:3]
        return d

    @router.put("/nodes/{nid}")
    def update_node(nid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        """改名/改标签（部分更新，仅校验出现的字段）。"""
        check_write(x_admin_token)
        try:
            parsed = NodeUpdate(**body).model_dump(exclude_unset=True)
        except Exception as e:
            raise HTTPException(422, str(e))
        fields = {}
        if "name" in parsed:
            name = (parsed["name"] or "").strip()
            if not name:
                raise HTTPException(422, "节点名不能为空")
            fields["name"] = name
        if "tags" in parsed:
            tags = parsed["tags"] or {}
            if not isinstance(tags, dict) or not all(
                    isinstance(k, str) and isinstance(v, (str, int, float, bool))
                    for k, v in tags.items()):
                raise HTTPException(422, "标签必须是「字符串→标量」的扁平映射")
            fields["tags_json"] = json.dumps(tags, ensure_ascii=False)
        if not fields:
            raise HTTPException(422, "无有效更新字段（仅支持 name / tags）")
        try:
            return s.update_node(nid, fields, now())
        except KeyError:
            raise HTTPException(404, "节点不存在")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.delete("/nodes/{nid}")
    def delete_node(nid: str, x_admin_token: str | None = Header(default=None)):
        """级联删除：节点 + 其探测结果/聚合/心跳/事件，并从任务分配移除。不可逆。"""
        check_write(x_admin_token)
        try:
            info = s.delete_node(nid, now())
            return {"deleted": nid, "name": info["name"]}
        except KeyError:
            raise HTTPException(404, "节点不存在")

    @router.get("/overview")
    def overview():
        st = s.stats_counts()
        nodes = s.list_nodes()
        tasks = s.list_tasks()
        t_now = now()
        ok_n = fail_n = 0
        for t in tasks:
            for b in s.agg_buckets_existing("1h", t["id"], t_now - 86400, t_now):
                ok_n += b["ok"] or 0
                fail_n += b["fail"] or 0
        avail = round(ok_n / (ok_n + fail_n), 4) if (ok_n + fail_n) else None
        return {
            "tasks_total": len(tasks), "tasks_enabled": sum(1 for t in tasks if t["enabled"]),
            "nodes_total": len(nodes),
            "nodes_online": sum(1 for n in nodes if n["status"] == "online"),
            "incidents_open": st["incidents_open"], "results_total": st["results"],
            "avail_24h": avail,
        }

    # ---------- 查询 ----------
    @router.get("/query/uptime")
    def query_uptime(task_id: str, bucket: int = 60, t_from: int = 0, t_to: int = 0):
        """通断条带：每节点一行。bucket 秒（60/300/1800...）。status: 0 ok 1 fail 2 无数据。"""
        t_to = t_to or now()
        t_from = t_from or (t_to - 3600)
        step = max(bucket, 60)
        rows = s.db.execute(
            "SELECT ts, node_id, dns, url, count, ok FROM aggregates WHERE bucket='1m'"
            " AND task_id=? AND ts>=? AND ts<=? ORDER BY ts",
            (task_id, t_from, t_to)).fetchall()
        nodes = {n["id"]: n["name"] for n in s.list_nodes()}
        # 已知流（含只有 skipped 记录的，如 mtr 未安装）：先铺行，否则该流在条带里完全不出现
        streams = {(st["node_id"], st["dns"] or "", st["url"] or ""): st
                   for st in s.result_streams(task_id)}
        by_node: dict = {k: {} for k in streams}
        for r in rows:
            bucket_ts = r["ts"] // step * step
            key = (r["node_id"], r["dns"] or "", r["url"] or "")
            cellmap = by_node.setdefault(key, {})
            c = cellmap.setdefault(bucket_ts, {"ok": 0, "count": 0})
            c["ok"] += r["ok"]
            c["count"] += r["count"]
        out_rows = []
        for (nid, dns, url), cellmap in sorted(by_node.items(), key=lambda x: nodes.get(x[0][0], x[0][0])):
            cells = []
            b = t_from // step * step
            while b <= t_to:
                c = cellmap.get(b)
                if not c or not c["count"]:
                    cells.append({"ts": b, "st": 2, "rtt": None})
                else:
                    avail = c["ok"] / c["count"]
                    st = 0 if avail >= 1 else (1 if avail == 0 else 1)  # 部分失败按失败展示
                    cells.append({"ts": b, "st": st, "rtt": None})
                b += step
            label = nodes.get(nid, nid) + (f" · {dns}" if dns else "") + (f" · {url}" if url else "")
            stk = streams.get((nid, dns, url)) or {}
            skip = (stk.get("latest_error") or stk.get("latest_error_class") or "探测被跳过") \
                if stk.get("latest_status") == "skipped" else ""
            out_rows.append({"node_id": nid, "label": label, "dns": dns, "url": url,
                             "cells": cells, "skipped": skip})
        return {"from": t_from, "to": t_to, "step": step, "rows": out_rows}

    @router.get("/query/series")
    def query_series(task_id: str, node_id: str = "", dns: str = "", url: str = "",
                     metric: str = "rtt", granularity: str = "raw",
                     t_from: int = 0, t_to: int = 0):
        t_to = t_to or now()
        t_from = t_from or (t_to - 3600)
        out = []
        if granularity == "raw":
            if not node_id:
                raise HTTPException(400, "raw 粒度必须指定 node_id")
            if metric == "lines":
                # dns 任务逐线路明细：storage 走「完整 metrics」分支（无 v 列），
                # 前端 renderDnsLines 取最新一轮的 metrics.lines 渲染逐线路表
                rows = s.raw_series(task_id, node_id, dns, url, "lines", t_from, t_to)
                out = [{"ts": r["ts"], "status": r["status"], "error_class": r["error_class"],
                        "metrics": r["metrics"]} for r in rows]
                return {"from": t_from, "to": t_to, "points": out}
            if metric not in ("rtt", "total", "loss"):
                raise HTTPException(400, "raw 仅支持 rtt/total/loss/lines")
            rows = s.raw_series(task_id, node_id, dns, url, metric, t_from, t_to)
            out = [{"ts": r["ts"], "v": r["v"], "status": r["status"],
                    "error_class": r["error_class"],
                    "resolved_ip": r.get("resolved_ip", "")} for r in rows]
        else:
            bucket = granularity if granularity in BUCKET_SECONDS else "1m"
            if node_id:
                rows = s.agg_read(bucket, task_id, node_id, dns, url, t_from, t_to)
            else:
                rows = s.agg_buckets_existing(bucket, task_id, t_from, t_to)
            key = {"rtt": "rtt_avg", "rtt_p95": "rtt_p95", "loss": "loss_rate",
                   "avail": "avail_rate"}.get(metric, "rtt_avg")
            out = [{"ts": r["ts"], "v": r[key], "count": r["count"],
                    "avail_rate": r["avail_rate"]} for r in rows]
        return {"points": out, "granularity": granularity}

    @router.get("/query/streams")
    def query_streams(task_id: str):
        """任务的结果流（节点×线路×URL 组合），供前端筛选器。"""
        nodes = {n["id"]: n["name"] for n in s.list_nodes()}
        streams = s.result_streams(task_id)
        for st in streams:
            st["node_name"] = nodes.get(st["node_id"], st["node_id"])
        return streams

    @router.get("/query/curl_codes")
    def query_curl_codes(task_id: str, bucket: int = 60, t_from: int = 0, t_to: int = 0,
                         url: str = ""):
        t_to = t_to or now()
        t_from = t_from or (t_to - 3600)
        step = max(bucket, 60)
        rows = s.db.execute(
            "SELECT ts, http_code_json, url FROM aggregates WHERE bucket='1m' AND task_id=?"
            " AND ts>=? AND ts<=?", (task_id, t_from, t_to)).fetchall()
        agg: dict = {}
        for r in rows:
            if url and (r["url"] or "") != url:
                continue
            b = r["ts"] // step * step
            codes = json.loads(r["http_code_json"] or "{}")
            c = agg.setdefault(b, {})
            for code, n in codes.items():
                cls = "2xx" if code.startswith("2") else ("3xx" if code.startswith("3") else
                      ("4xx" if code.startswith("4") else ("5xx" if code.startswith("5") else "other")))
                c[cls] = c.get(cls, 0) + n
        times = sorted(agg)
        out = [{"ts": t, **agg[t]} for t in times]
        return {"points": out}

    @router.get("/query/mtr")
    def query_mtr(task_id: str, node_id: str = "", dns: str = "", url: str = "",
                  ts: int = 0, bucket: int = 0, limit: int = 20):
        """路径（mtr/tracert）明细。

        - 默认：每个流（节点×线路×URL）最近一次，按时间倒序，带 node_name。
          按流返回而不是全局取最近 N 条：同一任务里某个节点可能没有路径能力，
          全局取最近几条会让有跳数的流被 skipped 流挤掉（前端只能看到空白）。
        - ts>0 且指定 node_id：返回该流在 [ts, ts+bucket) 内的一条（通断条带点击联动）；
          桶内没有则退到 ts 之前最近一条，避免点了格子却是空白。
        """
        nodes = {n["id"]: n["name"] for n in s.list_nodes()}
        cols = "ts, node_id, dns, url, status, error_class, metrics_json"

        def _row(r):
            d = dict(r)
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            d["node_name"] = nodes.get(d["node_id"], d["node_id"])
            return d

        if ts and node_id:
            base = (f"SELECT {cols} FROM probe_results WHERE task_id=? AND type='mtr'"
                    " AND node_id=? AND dns=? AND url=?")
            args = [task_id, node_id, dns or "", url or ""]
            r = None
            with s.lock:
                if bucket > 0:
                    r = s.db.execute(base + " AND ts>=? AND ts<? ORDER BY ts ASC LIMIT 1",
                                     args + [ts, ts + bucket]).fetchone()
                if not r:
                    r = s.db.execute(base + " AND ts<? ORDER BY ts DESC LIMIT 1",
                                     args + [ts]).fetchone()
            return [_row(r)] if r else []

        streams = s.result_streams(task_id)
        if node_id:
            streams = [st for st in streams if st["node_id"] == node_id]
        if dns:
            streams = [st for st in streams if (st["dns"] or "") == dns]
        if url:
            streams = [st for st in streams if (st["url"] or "") == url]
        out = []
        with s.lock:
            for st in streams:
                r = s.db.execute(
                    f"SELECT {cols} FROM probe_results WHERE task_id=? AND type='mtr'"
                    " AND node_id=? AND dns=? AND url=? ORDER BY ts DESC LIMIT 1",
                    (task_id, st["node_id"], st["dns"] or "", st["url"] or "")).fetchone()
                if r:
                    out.append(_row(r))
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out[:limit] if limit and limit > 0 else out

    @router.get("/query/mtr_trend")
    def query_mtr_trend(task_id: str, hours: int = 24, node_id: str = "", dns: str = "",
                        url: str = ""):
        """逐跳趋势：窗口内的原始 mtr/tracert 结果按跳号聚合（最多扫 500 条原始行）。

        返回 hops=[{hop, seen(出现次数), loss_avg(平均Loss%), rtt_avg(平均RTT),
        host(最后主机), asn}] + window 说明；按跳号升序。
        """
        hours = max(1, min(int(hours or 24), 168))
        t_to = now()
        t_from = t_to - hours * 3600
        sql = ("SELECT ts, node_id, dns, url, metrics_json FROM probe_results "
               "WHERE task_id=? AND type='mtr' AND ts>=? AND ts<=?")
        args: list = [task_id, t_from, t_to]
        if node_id:
            sql += " AND node_id=?"
            args.append(node_id)
        if dns:
            sql += " AND dns=?"
            args.append(dns)
        if url:
            sql += " AND url=?"
            args.append(url)
        sql += " ORDER BY ts DESC LIMIT 500"
        with s.lock:
            rows = s.db.execute(sql, args).fetchall()
        agg: dict = {}
        for r in rows:
            try:
                hops = json.loads(r["metrics_json"] or "{}").get("hops") or []
            except ValueError:
                continue
            for h in hops:
                try:
                    hn = int(h.get("hop"))
                except (TypeError, ValueError):
                    continue
                a = agg.setdefault(hn, {"seen": 0, "loss_sum": 0.0, "rtt_sum": 0.0,
                                        "rtt_n": 0, "host": "", "asn": None, "ts": 0})
                a["seen"] += 1
                a["loss_sum"] += float(h.get("loss_pct") or 0)
                # 平均 RTT 取 hops[].avg；全超时跳的 0 值不计入均值
                avg_rtt = float(h.get("avg") or 0)
                if avg_rtt > 0:
                    a["rtt_sum"] += avg_rtt
                    a["rtt_n"] += 1
                # 行按 ts 倒序：第一次遇到某跳号即为最近一轮的 host/asn
                if a["ts"] < r["ts"]:
                    a["ts"] = r["ts"]
                    a["host"] = str(h.get("host") or "???")
                    a["asn"] = h.get("asn")
        hops_out = [{
            "hop": hn, "seen": a["seen"],
            "loss_avg": round(a["loss_sum"] / a["seen"], 1),
            "rtt_avg": round(a["rtt_sum"] / a["rtt_n"], 1) if a["rtt_n"] else None,
            "host": a["host"] or "???", "asn": a["asn"],
        } for hn, a in sorted(agg.items())]
        return {"task_id": task_id,
                "window": {"hours": hours, "from": t_from, "to": t_to,
                           "results": len(rows), "limit": 500},
                "hops": hops_out}

    @router.get("/query/incidents")
    def query_incidents(limit: int = 30, open_only: bool = False):
        return s.list_incidents(limit, open_only)

    # ---------- 值班页一屏（ONCALL_OPTIMIZATION.md 第二期 5）----------
    @router.get("/oncall")
    def oncall(limit: int = 50):
        """当前 open incidents 的值班视图：一行回答「坏在哪层/坏多大/坏了多久」。

        逐条组装：分层初判（diagnose.classify）+ 范围判定（diagnose.verdict，states 取
        「该任务各节点最近一轮探测状态」，≤10 分钟窗，口径与 /api/tasks 的
        current_status 一致——用 result_streams 的最新一条，过期样本视为无数据）+
        对应流最近一次探测 + 确认状态。数据全部来自 storage 现有方法。
        """
        t_now = now()
        window = 600                       # 「最近一轮」窗口：10 分钟
        nodes = {n["id"]: n for n in s.list_nodes()}
        tasks = {t["id"]: t for t in s.list_tasks()}
        rank = {"fail": 2, "skipped": 1, "ok": 0}   # 同一轮内多条流：最差状态代表节点
        incs = s.list_incidents(max(1, min(limit, 200)), open_only=True)
        # 范围判定的输入按「任务」去重后一次取齐：每流窗口内最新一行（单条窗口化
        # SQL，替代逐事件 result_streams 的 N+1——线上 11 条 open 事件曾耗时 3.7s）。
        # 语义与旧实现等价：某流全时间最新一条落在窗内 → 同一行；落在窗外 → 两版都排除。
        tids = list({str(i.get("task_id") or "") for i in incs} - {""})
        recent: dict[str, dict[str, tuple[int, str]]] = {t: {} for t in tids}
        if tids:
            ph = ",".join("?" for _ in tids)
            with s.lock:
                rows = s.db.execute(
                    "SELECT task_id,node_id,ts,status FROM ("
                    " SELECT task_id,node_id,ts,status,"
                    " ROW_NUMBER() OVER (PARTITION BY task_id,node_id,dns,url"
                    "                     ORDER BY ts DESC) rn"
                    " FROM probe_results WHERE task_id IN (" + ph + ") AND ts BETWEEN ? AND ?)"
                    " WHERE rn=1", (*tids, t_now - window, t_now)).fetchall()
            for r in rows:
                by_node = recent.setdefault(str(r["task_id"]), {})
                nid_key = str(r["node_id"])
                cur = by_node.get(nid_key)
                if cur is None or int(r["ts"]) > cur[0] or (
                        int(r["ts"]) == cur[0] and rank.get(str(r["status"]), -1) > rank.get(cur[1], -1)):
                    by_node[nid_key] = (int(r["ts"]), str(r["status"]))
        # 陈旧阈值与 sweep 的 close_stale_incidents 保持同一配置，避免「页面说陈旧、后端不认」
        stale_after = max(0, int(cfg.probe.get("stale_after_seconds", 21600) or 0))
        items = []
        for inc in incs:
            tid = str(inc.get("task_id") or "")
            nid = str(inc.get("node_id") or "")
            task = tasks.get(tid) or {}
            node = nodes.get(nid) or {}
            reason = inc.get("reason") or {}
            error_class = str(reason.get("error_class") or "")
            if not tid:
                # 节点侧事件没有 error_class，classify 只会给出「待定位」——但节点事件本来
                # 就明确落在「节点侧」这一层，值班的人需要的是「该查什么」，不是一个待定位。
                layer, advice = ("节点侧",
                                 "节点心跳中断：查该节点进程/网络/主机资源；节点恢复后自动收口")
            else:
                layer, advice = classify(error_class)
            # 范围判定输入：该任务各节点最近一轮（≤10 分钟窗）探测状态。
            # 同节点多条流（多 URL/多线路）取时间戳最新的一轮，轮内按最差状态合并
            # （一个 URL 挂即该节点本轮有失败，与事件的产生口径一致）。
            states = [{"node_id": k, "node_name": (nodes.get(k) or {}).get("name") or k,
                       "status": v[1]} for k, v in recent.get(tid, {}).items()]
            last = s.latest_per_stream(tid, nid, inc.get("dns") or "",
                                       inc.get("url") or "", 1)
            last_ts = int(last[0]["ts"]) if last else 0
            last_status = last[0]["status"] if last else None
            last_age_s = max(0, t_now - last_ts) if last_ts else None
            items.append({
                "incident_id": inc["id"], "task_id": tid,
                "task_name": task.get("name") or tid,
                "type": task.get("type") or "", "node_id": nid,
                "node_name": node.get("name") or nid,
                "dns": inc.get("dns") or "", "url": inc.get("url") or "",
                "error_class": error_class, "layer": layer, "advice": advice,
                "scope": verdict(states),
                "started_at": inc.get("started_at"),
                "duration_s": max(0, t_now - int(inc.get("started_at") or t_now)),
                "last_status": last_status, "last_ts": last_ts,
                "last_age_s": last_age_s, "bucket": _oncall_bucket(
                    task, node, last_status, last_age_s, stale_after),
                "acked": bool(inc.get("acked_at")),
            })
        return {"ts": t_now, "items": items}

    @router.get("/compare")
    def compare(task_id: str, mode: str = "yesterday", metric: str = "rtt",
                window_hours: int = 0, node_id: str = "", dns: str = "", url: str = ""):
        """按小时对比：最近整 24 小时 vs 上一周期（环比/同比）。走 1h 聚合表。

        口径（2026-10-03 复核修正，见 .docs/ONCALL_OPTIMIZATION_2.md §1.4 / 第五期）：

        1. 窗口一律对齐到**已完结的小时**。聚合只写到最后一个完结桶（app.py 的
           complete_to），当前小时桶永不写入；原实现把当前小时也放进轴里，于是
           「最近24小时」最后一格恒为 null，而对比线在同一轴位是完整小时 ——
           等于拿空窗比整窗，末点天然不对称。
        2. 环比 / 同比分开：prev/yesterday/lastweek/lastmonth 都是**环比**（相邻周期），
           新增 lastyear 为**同比**（去年同期）。1h 聚合保留 730 天，同比有数据基础。
        3. 同时返回**时段级汇总**（加权可用率/延迟/丢包 + Δ）与**覆盖度**（两边各多少格
           有效、多少格可重叠）。原实现只给两条曲线：24 格里只有 4 格可比时，页面上
           完全看不出来，这正是「环比数据不对」的观感来源。

        metric 决定比什么（历史缺陷：只比 rtt_avg，curl/mtr 该列为 NULL → 整页空白）：
          rtt   延迟均值（ms，仅 ping 类有）
          avail 可用率（%，所有任务类型都有）
          loss  丢包率（%，ping 类有）
        """
        t_now = now()
        offsets = {"yesterday": 86400, "lastweek": 7 * 86400,
                   "lastmonth": 30 * 86400, "lastyear": 365 * 86400}
        off = offsets.get(mode, 86400)
        labels = {"yesterday": "昨日同期", "lastweek": "上周同日",
                  "lastmonth": "30 天前同期", "lastyear": "去年同期"}
        # 同比 = 与去年同期比；环比 = 与相邻周期比。前端按这个字段分组标注，
        # 避免再出现「把上周同日标成同比」的口径错误。
        kinds = {"prev": "环比", "yesterday": "环比", "lastweek": "环比",
                 "lastmonth": "环比", "lastyear": "同比"}
        metrics = {"rtt": ("rtt_avg", "ms", 1.0),
                   "avail": ("avail_rate", "%", 100.0),
                   "loss": ("loss_rate", "%", 100.0)}
        col, unit, scale = metrics.get(metric, metrics["rtt"])
        n = max(1, min(int(window_hours) if window_hours else 24, 24 * 31))

        with s.lock:
            hmin = s.db.execute("SELECT MIN(ts) mn FROM probe_results").fetchone()["mn"] or 0
        # 保留 2 位：刚上线几分钟的平台 round 到 1 位会变成 0.0，前端把 0 当「无历史」
        # → 「自动切前一时段」永不触发、原因文案走错分支（CI 实例实测发现的 bug）
        history_hours = round((t_now - hmin) / 3600, 2) if hmin else 0

        def buckets(t_from: int, t_to: int) -> dict:
            """闭区间 [t_from, t_to] 内的 1h 桶，按 ts 索引（走节点维度时自动收窄）。"""
            if t_to < t_from:
                return {}
            if node_id:
                rows = s.agg_read("1h", task_id, node_id, dns, url, t_from, t_to)
            else:
                rows = s.agg_buckets_existing("1h", task_id, t_from, t_to)
            return {int(r["ts"]): r for r in rows}

        def win(nb: int) -> list[int]:
            """最近 nb 个**已完结**小时的轴：末格是「上一个整点」，不含当前小时。"""
            end = (t_now // 3600) * 3600 - 3600
            return list(range(end - (nb - 1) * 3600, end + 3600, 3600))

        if mode == "prev":
            # 「前一时段」：窗口自适应，保证刚上线的平台也能看到两条真正**可比**的曲线。
            # 选择条件原先只是「两边各自有数据」，实测会挑出 24h 窗口却只有 4 格可比的
            # 组合（两条线各占半轴）——那正是「环比数据不对」的观感来源。现在要求
            # **至少一半的格两边都有数据**，从大到小取第一个满足的窗口；实在没有就退到
            # 「有可比格的最大窗口」，再没有才退回上限窗口（此时前端会说明原因）。
            pick_max = max(1, min(24, int(window_hours) if window_hours
                                  else max(1, int(history_hours // 2) or 1)))
            chosen = None
            best = None                  # (overlap, w, hrs, t_b, o_b)：回退时取重叠最多的
            for w in range(pick_max, 0, -1):
                hrs = win(w)
                t_b = buckets(hrs[0], hrs[-1])
                o_b = buckets(hrs[0] - w * 3600, hrs[-1] - w * 3600)
                # 注意：o_b 的键是**偏移后**的时间戳，必须用 h-w*3600 去查。
                # 这与下游 series() 的取数口径是同一条：这里写错的话每个窗口都算出 0 重叠，
                # 于是永远退到「上限窗口」——修复前那个「24h 窗口只有 4 格可比」的病就会复发。
                ov = sum(1 for h in hrs if h in t_b and (h - w * 3600) in o_b)
                if ov == 0:
                    continue
                if best is None or ov > best[0]:
                    best = (ov, w, hrs, t_b, o_b)
                if ov * 2 >= w:          # 至少一半的格真正可比
                    chosen = (w, hrs, t_b, o_b)
                    break
            if chosen is None:
                if best is not None:     # 回退到「重叠最多」的窗口，而不是最大的窗口
                    chosen = (best[1], best[2], best[3], best[4])
                else:
                    hrs = win(pick_max)
                    chosen = (pick_max, hrs, buckets(hrs[0], hrs[-1]),
                              buckets(hrs[0] - pick_max * 3600, hrs[-1] - pick_max * 3600))
            w, hours, today, other = chosen
            label, today_label, used_window = f"前 {w} 小时", f"最近 {w} 小时", w
            shift = w * 3600
        else:
            hours = win(n)
            used_window = n
            today = buckets(hours[0], hours[-1])
            other = buckets(hours[0] - off, hours[-1] - off)
            label = labels.get(mode, mode)
            today_label = f"最近 {n} 小时"
            shift = off

        def series(by_h: dict, offset: int = 0) -> tuple[list, list]:
            """把对比时段的桶对齐回当前时段的时间轴，并带回每格样本数。

            历史 bug：对比桶是按 start-off 取出来的（键是偏移后的时间戳），却仍用未偏移
            的 hours 去查 → 对比线恒为空，三种模式都只剩同一条当前曲线。
            样本数一并返回：让「这格只有 2 个样本」在界面上可见，而不是画一条同样粗的线。
            """
            out, counts = [], []
            for h in hours:
                r = by_h.get(h - offset)
                v = r.get(col) if r else None
                out.append(round(v * scale, 2) if v is not None else None)
                counts.append(int(r["count"] or 0) if r else 0)
            return out, counts

        def period_total(by_h: dict, offset: int = 0) -> dict:
            """时段级汇总：可用率按 Σok/Σcount（加权），延迟/丢包按样本数加权。"""
            c = o = 0
            rtt_num = rtt_den = 0.0
            loss_num = loss_den = 0.0
            for h in hours:
                r = by_h.get(h - offset)
                if not r:
                    continue
                cnt = int(r["count"] or 0)
                c += cnt
                o += int(r["ok"] or 0)
                if r.get("rtt_avg") is not None and cnt:
                    rtt_num += float(r["rtt_avg"]) * cnt
                    rtt_den += cnt
                if r.get("loss_rate") is not None and cnt:
                    loss_num += float(r["loss_rate"]) * cnt
                    loss_den += cnt
            return {"count": c, "ok": o,
                    "avail": round(100.0 * o / c, 3) if c else None,
                    "rtt": round(rtt_num / rtt_den, 2) if rtt_den else None,
                    "loss": round(100.0 * loss_num / loss_den, 3) if loss_den else None}

        today_s, today_c = series(today)
        other_s, other_c = series(other, shift)
        t_sum = period_total(today)
        o_sum = period_total(other, shift)

        def delta(a, b):
            return round(a - b, 3) if (a is not None and b is not None) else None

        # 覆盖度：两条线各有多少格有效、有多少格**真正可比**（重叠）。
        # 重叠为 0 时前端不再画两条各占半轴的断线，而是直接给结论与原因。
        coverage = {"total": len(hours),
                    "today": sum(1 for v in today_s if v is not None),
                    "other": sum(1 for v in other_s if v is not None),
                    "overlap": sum(1 for i in range(len(hours))
                                   if today_s[i] is not None and other_s[i] is not None)}
        return {"mode": mode, "metric": metric, "unit": unit, "label": label,
                "kind": kinds.get(mode, "环比"),
                "today_label": today_label, "window_hours": used_window,
                "has_other": any(v is not None for v in other_s),
                "has_today": any(v is not None for v in today_s),
                "history_from": hmin, "history_hours": history_hours,
                "hours": hours, "today": today_s, "other": other_s,
                "today_counts": today_c, "other_counts": other_c,
                "coverage": coverage,
                "summary": {"today": t_sum, "other": o_sum,
                            "delta": {"avail_pp": delta(t_sum["avail"], o_sum["avail"]),
                                      "rtt_ms": delta(t_sum["rtt"], o_sum["rtt"]),
                                      "loss_pp": delta(t_sum["loss"], o_sum["loss"])}}}

    @router.get("/detail")
    def detail(task_id: str, node_id: str, ts: int, dns: str = "", url: str = "",
               bucket: int = 0):
        """单次探测详情。bucket>0 时查 [ts, ts+bucket) 窗口内最新一条（条带色块点击）。"""
        if bucket > 0:
            with s.lock:
                rows = s.db.execute(
                    "SELECT * FROM probe_results WHERE task_id=? AND node_id=? AND dns=? AND url=?"
                    " AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 1",
                    (task_id, node_id, dns, url, ts, ts + bucket)).fetchall()
            if not rows:
                raise HTTPException(404, "该时刻无探测记录（可能被去重或未产生）")
            d = dict(rows[0])
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            return d
        r = s.result_at(task_id, node_id, dns, url, ts)
        if not r:
            raise HTTPException(404, "该时刻无探测记录（可能被去重或未产生）")
        return r

    @router.get("/export")
    def export(task_id: str, t_from: int = 0, t_to: int = 0, fmt: str = "csv"):
        t_to = t_to or now()
        t_from = t_from or (t_to - 86400)
        with s.lock:
            rows = s.db.execute(
                "SELECT ts,task_id,node_id,type,dns,url,status,error_class,error,dns_server,"
                "resolved_ip,dns_time_ms,metrics_json FROM probe_results WHERE task_id=? "
                "AND ts>=? AND ts<=? ORDER BY ts", (task_id, t_from, t_to)).fetchall()
        if fmt == "json":
            data = []
            for r in rows:
                d = dict(r)
                d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
                data.append(d)
            return StreamingResponse(io.StringIO(json.dumps(data, ensure_ascii=False, indent=1)),
                                     media_type="application/json",
                                     headers={"Content-Disposition":
                                              f'attachment; filename="export_{task_id}.json"'})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "task_id", "node_id", "type", "dns", "url", "status", "error_class",
                    "error", "dns_server", "resolved_ip", "dns_time_ms", "metrics"])
        for r in rows:
            w.writerow([r["ts"], r["task_id"], r["node_id"], r["type"], r["dns"], r["url"],
                        r["status"], r["error_class"], r["error"], r["dns_server"],
                        r["resolved_ip"], r["dns_time_ms"], r["metrics_json"]])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition":
                                          f'attachment; filename="export_{task_id}.csv"'})

    # ---------- 节点分组 ----------
    @router.get("/groups")
    def list_groups():
        nodes = {n["id"]: n["name"] for n in s.list_nodes()}
        out = []
        for g in s.list_groups():
            d = dict(g)
            d["member_names"] = [nodes.get(m, m) for m in g["members"]]
            out.append(d)
        return out

    @router.post("/groups")
    def create_group(body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(422, "分组名不能为空")
        try:
            return s.create_group(new_id("g"), name, str(body.get("note") or ""), now())
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.put("/groups/{gid}")
    def update_group(gid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        fields = {}
        if "name" in body:
            nm = str(body.get("name") or "").strip()
            if not nm:
                raise HTTPException(422, "分组名不能为空")
            fields["name"] = nm
        if "note" in body:
            fields["note"] = str(body.get("note") or "")
        try:
            return s.update_group(gid, fields, now())
        except KeyError:
            raise HTTPException(404, "分组不存在")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.delete("/groups/{gid}")
    def delete_group(gid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        try:
            return {"deleted": gid, "name": s.delete_group(gid, now())["name"]}
        except KeyError:
            raise HTTPException(404, "分组不存在")

    @router.put("/groups/{gid}/members")
    def set_group_members(gid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        """整组覆盖式设置成员（传节点 id 列表）。"""
        check_write(x_admin_token)
        nodes = body.get("nodes")
        if not isinstance(nodes, list):
            raise HTTPException(422, "nodes 必须是节点 id 数组")
        try:
            return s.set_group_members(gid, [str(x) for x in nodes], now())
        except KeyError:
            raise HTTPException(404, "分组不存在")

    # ---------- 注册 Token 管理 ----------
    @router.get("/tokens")
    def list_tokens():
        return {"items": s.list_tokens(),
                "bootstrap": bool(cfg.agent.get("register_token"))}

    @router.post("/tokens")
    def create_token(body: dict, x_admin_token: str | None = Header(default=None)):
        """新建 Token：明文只在本次响应里返回一次（库里只存 sha256）。"""
        check_write(x_admin_token)
        import secrets as _secrets
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(422, "Token 名称不能为空")
        plain = "gpm_" + _secrets.token_urlsafe(24)
        try:
            t = s.create_token(new_id("tk"), name, sha256(plain), str(body.get("note") or ""), now())
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {**t, "token": plain}

    @router.put("/tokens/{tid}")
    def update_token(tid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        fields: dict = {}
        if "name" in body:
            nm = str(body.get("name") or "").strip()
            if not nm:
                raise HTTPException(422, "Token 名称不能为空")
            fields["name"] = nm
        if "note" in body:
            fields["note"] = str(body.get("note") or "")
        if "enabled" in body:
            fields["enabled"] = bool(body["enabled"])
        try:
            return s.update_token(tid, fields, now())
        except KeyError:
            raise HTTPException(404, "Token 不存在")

    @router.delete("/tokens/{tid}")
    def delete_token(tid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if not s.delete_token(tid):
            raise HTTPException(404, "Token 不存在")
        return {"deleted": tid}

    # ---------- GeoIP 世界地图 ----------
    @router.get("/geo/nodes")
    def geo_nodes():
        return {"nodes": geo.locate_nodes(s, cfg), "unknown": geo.unknown_nodes(s, cfg)}

    @router.get("/geo/networks")
    def geo_networks():
        """自定义「IP 段 → 位置」列表（IDC 内网段定位，优先于在线查询）。"""
        return s.list_geo_networks()

    @router.post("/geo/networks")
    def add_geo_network(body: dict, x_admin_token: str | None = Header(default=None)):
        """新增：{cidr, place, lat?, lng?, note?}；只给 place 时用内置区表解析坐标。"""
        check_write(x_admin_token)
        import ipaddress
        cidr = str(body.get("cidr") or "").strip()
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(422, f"CIDR 格式不正确: {cidr!r}（示例 10.10.10.0/24 或 2001:db8::/32）")
        place = str(body.get("place") or "").strip()
        lat, lng = body.get("lat"), body.get("lng")
        if lat is None or lng is None:
            loc = geo.resolve_place(place)
            if not loc:
                raise HTTPException(422, f"位置 {place!r} 无法解析：请写内置区表里的地名（如 上海/cn-east/东京），"
                                         f"或直接给 lat/lng")
            lat, lng = loc[0], loc[1]
            # 写的是区表「键」（如 cn-east）就用规范地名，写的是自由地名（如 上海IDC-A区）就保留用户的写法
            place = loc[2] if place.strip().lower() in geo.REGION_TABLE else (place or loc[2])
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            raise HTTPException(422, "lat/lng 必须是数字")
        if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
            raise HTTPException(422, "lat/lng 超出范围")
        try:
            return s.add_geo_network(new_id("gn"), str(net), place, lat_f, lng_f,
                                     str(body.get("note") or ""), now())
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.delete("/geo/networks/{gid}")
    def del_geo_network(gid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if not s.delete_geo_network(gid):
            raise HTTPException(404, "映射不存在")
        return {"deleted": gid}

    @router.get("/geo/places")
    def geo_places():
        """内置区表（供前端下拉/示例），返回 [键, 纬度, 经度, 标签]。"""
        return [{"key": k, "lat": v[0], "lng": v[1], "label": v[2]}
                for k, v in sorted(geo.REGION_TABLE.items())]

    @router.get("/geo/flows")
    def geo_flows(budget: int = 8):
        """节点 → 目标（解析 IP）的探测链路，供地图上做动态连线动画。"""
        return geo.locate_flows(s, cfg, budget=max(0, min(budget, 30)))

    # ---------- 事件（含节点侧）----------
    @router.get("/query/incidents_all")
    def incidents_all(limit: int = 50, kind: str = ""):
        nodes = {n["id"]: n["name"] for n in s.list_nodes()}
        tasks = {t["id"]: t["name"] for t in s.list_tasks()}
        out = []
        for i in s.list_incidents(limit * 2):
            if kind and i.get("kind") != kind:
                continue
            d = dict(i)
            d["node_name"] = nodes.get(d["node_id"], d["node_id"])
            d["task_name"] = tasks.get(d["task_id"], d["task_id"] or "")
            if d.get("kind") == "node":
                d["title"] = ("节点离线" if not d["ended_at"] else "节点恢复") + \
                             f" · {d['node_name']}"
            else:
                d["title"] = f"{d['task_name']} · {d['dns'] or '默认线路'}"
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def _rule_fields(body: dict, partial: bool = False) -> dict:
        """告警规则字段校验（metric/op/阈值/窗口/静默期/渠道）。"""
        out: dict = {}
        metric = body.get("metric")
        if metric is not None:
            if metric not in alerting.METRICS:
                raise HTTPException(422, f"不支持的指标: {metric}")
            out["metric"] = metric
        op = body.get("op")
        if op is not None:
            if op not in alerting.OPS:
                raise HTTPException(422, f"不支持的比较方式: {op}")
            out["op"] = op
        if body.get("threshold") is not None:
            try:
                out["threshold"] = float(body["threshold"])
            except (TypeError, ValueError):
                raise HTTPException(422, "阈值必须是数字")
        for key, lo, hi in (("window_seconds", 60, 30 * 86400),
                            ("silence_seconds", 0, 7 * 86400),
                            ("escalate_minutes", 0, 1440)):
            if body.get(key) is not None:
                try:
                    v = int(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(422, f"{key} 必须是整数")
                out[key] = max(lo, min(v, hi))
        if body.get("channel_ids") is not None:
            if not isinstance(body["channel_ids"], list):
                raise HTTPException(422, "channel_ids 必须是数组")
            known = {c["id"] for c in s.list_channels()}
            bad = [c for c in body["channel_ids"] if c not in known]
            if bad:
                raise HTTPException(422, f"渠道不存在: {bad}")
            out["channel_ids"] = [str(c) for c in body["channel_ids"]]
        if body.get("name") is not None:
            nm = str(body["name"]).strip()
            if not nm:
                raise HTTPException(422, "规则名不能为空")
            out["name"] = nm
        for key in ("task_id", "node_id", "group_id"):
            if key in body:
                out[key] = str(body.get(key) or "")
        if body.get("severity") is not None:
            sev = str(body["severity"])
            if sev not in ("warning", "critical"):
                raise HTTPException(422, "severity 只能是 warning / critical")
            out["severity"] = sev
        if body.get("enabled") is not None:
            out["enabled"] = bool(body["enabled"])
        if not partial:
            for req in ("name", "metric", "op", "threshold"):
                if req not in out:
                    raise HTTPException(422, f"缺少必填字段: {req}")
        return out

    # ---------- 告警：通知渠道 ----------
    @router.get("/alerts/channels")
    def list_channels():
        out = s.list_channels()
        try:
            from . import notify
            for c in out:
                c["valid"] = notify.validate({"type": c["type"], **(c.get("config") or {})}) is None
        except Exception:  # noqa: BLE001
            pass
        return out

    @router.post("/alerts/channels")
    def create_channel(body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        import importlib
        name = str(body.get("name") or "").strip()
        ctype = str(body.get("type") or "").strip()
        conf = body.get("config") or {}
        if not name:
            raise HTTPException(422, "渠道名不能为空")
        if ctype not in ("webhook", "wecom", "dingtalk", "feishu", "smtp"):
            raise HTTPException(422, "不支持的渠道类型（webhook/wecom/dingtalk/feishu/smtp）")
        try:
            notify = importlib.import_module("gpm.server.notify")
            err = notify.validate({"type": ctype, **(conf or {})})
        except Exception as e:  # noqa: BLE001
            err = f"通知模块不可用: {e}"
        if err:
            raise HTTPException(422, err)
        try:
            return s.create_channel(new_id("ch"), name, ctype, conf, now())
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.put("/alerts/channels/{cid}")
    def update_channel(cid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        fields = {}
        for k in ("name", "type", "config", "enabled"):
            if k in body:
                fields[k] = body[k]
        if fields.get("name") is not None and not str(fields["name"]).strip():
            raise HTTPException(422, "渠道名不能为空")
        try:
            return s.update_channel(cid, fields, now())
        except KeyError:
            raise HTTPException(404, "渠道不存在")

    @router.delete("/alerts/channels/{cid}")
    def delete_channel(cid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if not s.delete_channel(cid):
            raise HTTPException(404, "渠道不存在")
        return {"deleted": cid}

    @router.post("/alerts/channels/{cid}/test")
    def test_channel(cid: str, x_admin_token: str | None = Header(default=None)):
        """测试发送：UI「测试」按钮用，返回 (成功?, 说明)。"""
        check_write(x_admin_token)
        ch = next((c for c in s.list_channels() if c["id"] == cid), None)
        if not ch:
            raise HTTPException(404, "渠道不存在")
        ok, msg = alerting.test_channel(s, ch)
        return {"ok": ok, "detail": msg}

    # ---------- 告警：规则 ----------
    @router.get("/alerts/rules")
    def list_rules():
        return {"items": s.list_rules(), "metrics": alerting.METRICS, "ops": alerting.OPS}

    @router.post("/alerts/rules")
    def create_rule(body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        fields = _rule_fields(body)
        try:
            return s.create_rule(new_id("ar"), fields, now())
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.put("/alerts/rules/{rid}")
    def update_rule(rid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        fields = _rule_fields(body, partial=True)
        try:
            return s.update_rule(rid, fields, now())
        except KeyError:
            raise HTTPException(404, "规则不存在")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.delete("/alerts/rules/{rid}")
    def delete_rule(rid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if not s.delete_rule(rid):
            raise HTTPException(404, "规则不存在")
        return {"deleted": rid}

    # ---------- 告警：维护窗口 ----------
    @router.get("/alerts/windows")
    def list_windows():
        return s.list_windows()

    @router.post("/alerts/windows")
    def create_window(body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        try:
            starts, ends = int(body.get("starts_at") or 0), int(body.get("ends_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(422, "starts_at / ends_at 必须是时间戳")
        if not starts or ends <= starts:
            raise HTTPException(422, "结束时间必须晚于开始时间")
        return s.create_window(new_id("mw"), {**body, "starts_at": starts, "ends_at": ends}, now())

    @router.delete("/alerts/windows/{wid}")
    def delete_window(wid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if not s.delete_window(wid):
            raise HTTPException(404, "维护窗口不存在")
        return {"deleted": wid}

    # ---------- 告警：历史与手动评估 ----------
    @router.get("/alerts")
    def alert_history(limit: int = 50, status: str = ""):
        return {"items": s.alert_recent(limit=max(1, min(limit, 500)), status=status),
                "counts": s.alert_counts()}

    @router.post("/alerts/evaluate")
    def evaluate_now(x_admin_token: str | None = Header(default=None)):
        """立即评估一轮规则（不等后台周期），返回本轮事件。"""
        check_write(x_admin_token)
        return {"events": alerting.evaluate(s)}

    # ---------- SLA / 报表 ----------
    @router.get("/report/sla")
    def report_sla(t_from: int = 0, t_to: int = 0, task_id: str = "", node_id: str = ""):
        from . import report
        t_to = t_to or now()
        t_from = t_from or (t_to - 86400)
        return report.sla(s, t_from, t_to, task_id=task_id, node_id=node_id)

    @router.get("/report/daily")
    def report_daily(task_id: str, days: int = 30):
        from . import report
        return {"items": report.daily_series(s, task_id, max(1, min(days, 365)), now())}

    @router.get("/report/digest")
    def report_digest(hours: int = 24):
        from . import report
        title, text = report.digest_text(s, max(1, min(hours, 24 * 30)), now())
        return {"title": title, "text": text}

    # ---------- 通知重投队列 ----------
    @router.get("/alerts/outbox")
    def outbox_list(limit: int = 50, status: str = ""):
        return {"items": s.outbox_list(limit=max(1, min(limit, 200)), status=status),
                "counts": s.outbox_counts()}

    @router.post("/alerts/outbox/{oid}/retry")
    def outbox_retry(oid: int, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        ok, msg = alerting.retry_one(s, oid)
        return {"ok": ok, "detail": msg}

    @router.delete("/alerts/outbox/{oid}")
    def outbox_delete(oid: int, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if not s.outbox_delete(oid):
            raise HTTPException(404, "记录不存在")
        return {"deleted": oid}

    # ---------- 巡检报告：设置与手动推送 ----------
    def _digest_settings() -> dict:
        return {"enabled": s.setting_get("digest_enabled", "0") == "1",
                "interval_hours": int(s.setting_get("digest_interval_hours", "24") or 24),
                "channel_ids": [c for c in (s.setting_get("digest_channel_ids", "") or "").split(",") if c],
                "last_ts": int(s.setting_get("digest_last_ts", "0") or 0)}

    @router.get("/report/digest/settings")
    def digest_settings():
        return _digest_settings()

    @router.put("/report/digest/settings")
    def digest_settings_set(body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        if "enabled" in body:
            s.setting_set("digest_enabled", "1" if body["enabled"] else "0")
        if body.get("interval_hours") is not None:
            try:
                h = int(body["interval_hours"])
            except (TypeError, ValueError):
                raise HTTPException(422, "interval_hours 必须是整数")
            s.setting_set("digest_interval_hours", str(max(1, min(h, 24 * 30))))
        if body.get("channel_ids") is not None:
            if not isinstance(body["channel_ids"], list):
                raise HTTPException(422, "channel_ids 必须是数组")
            known = {c["id"] for c in s.list_channels()}
            bad = [c for c in body["channel_ids"] if c not in known]
            if bad:
                raise HTTPException(422, f"渠道不存在: {bad}")
            s.setting_set("digest_channel_ids", ",".join(str(c) for c in body["channel_ids"]))
        return _digest_settings()

    @router.post("/report/digest/push")
    def digest_push(body: dict | None = None, x_admin_token: str | None = Header(default=None)):
        """立即生成并推送巡检报告（定时推送由 server.digest_check_interval 驱动）。"""
        check_write(x_admin_token)
        b = body or {}
        hours = int(b.get("hours") or (int(s.setting_get("digest_interval_hours", "24") or 24)))
        ids = b.get("channel_ids")
        if ids is None:
            raw = s.setting_get("digest_channel_ids", "")
            ids = [c for c in raw.split(",") if c] or None
        return alerting.push_digest(s, max(1, min(hours, 24 * 30)), ids, now())

    # ---------- 操作审计 ----------
    @router.get("/audit")
    def audit_list(limit: int = 100, action: str = "", target: str = "", since: int = 0):
        try:
            from . import audit
            items = audit.query(s, limit=max(1, min(limit, 500)), action=action,
                                target=target, since=since)
        except Exception as e:  # noqa: BLE001 - 审计模块缺失/异常不应 5xx
            items = []
            log.warning("审计查询失败: %s", e)
        return {"items": items, "counts": s.audit_counts()}

    # ---------- 事件详情与确认 ----------
    @router.get("/event/{iid}")
    def event_detail(iid: int):
        try:
            from . import eventview
            return eventview.detail(s, iid, now())
        except KeyError:
            raise HTTPException(404, "事件不存在")

    @router.post("/event/{iid}/ack")
    def event_ack(iid: int, body: dict | None = None, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        b = body or {}
        who = "admin" if x_admin_token else str(b.get("who") or "local")
        if not s.incident_ack(iid, now(), who, str(b.get("note") or "")):
            raise HTTPException(404, "事件不存在")
        return s.incident_get(iid)

    return router
