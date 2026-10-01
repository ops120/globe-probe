"""Web API：任务管理、节点、查询（条带/曲线/对比/明细/事件）、导出。"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from ..common.models import NodeUpdate, TaskCreate, TaskUpdate
from ..common.util import new_id, now, sha256, validate_target
from . import alerting, geo
from .storage import BUCKET_SECONDS


def setup_router(app_state) -> APIRouter:
    s = app_state["storage"]
    cfg = app_state["cfg"]
    router = APIRouter(prefix="/api")  # 每次调用独立 router，避免跨 app 闭包污染

    def check_write(x_admin_token: str | None):
        token = cfg.server.get("admin_token") or ""
        if token and x_admin_token != token:
            raise HTTPException(403, "需要 X-Admin-Token")

    # ---------- 任务 ----------
    @router.get("/tasks")
    def list_tasks():
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

    @router.post("/tasks")
    def create_task(body: TaskCreate, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        probe = cfg.probe
        lo = probe.get("min_mtr_interval_seconds", 60) if body.type == "mtr" \
            else probe.get("min_interval_seconds", 10)
        interval = max(body.interval_seconds, lo)
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
    def update_task(tid: str, body: dict, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        try:
            fields = TaskUpdate(**body).model_dump(exclude_unset=True)
        except Exception as e:
            raise HTTPException(422, str(e))
        task = s.get_task(tid)
        if not task:
            raise HTTPException(404, "任务不存在")
        probe = cfg.probe
        # 目标校验按任务类型：curl 以 urls 为准（target 可为空），ping/mtr 必须是合法目标
        if "target" in fields and task["type"] != "curl" \
                and not validate_target(fields["target"] or ""):
            raise HTTPException(422, f"非法目标: {fields['target']!r}")
        if isinstance(fields.get("interval_seconds"), int):
            lo = probe.get("min_mtr_interval_seconds", 60) if task["type"] == "mtr" \
                else probe.get("min_interval_seconds", 10)
            fields["interval_seconds"] = max(fields["interval_seconds"], lo)
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        try:
            return s.update_task(tid, fields, now())
        except KeyError:
            raise HTTPException(404, "任务不存在")
        except ValueError as e:
            raise HTTPException(422, str(e))

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
            st = streams.get((nid, dns, url)) or {}
            skip = (st.get("latest_error") or st.get("latest_error_class") or "探测被跳过") \
                if st.get("latest_status") == "skipped" else ""
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
            if metric not in ("rtt", "total", "loss"):
                raise HTTPException(400, "raw 仅支持 rtt/total/loss")
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

    @router.get("/query/incidents")
    def query_incidents(limit: int = 30, open_only: bool = False):
        return s.list_incidents(limit, open_only)

    @router.get("/compare")
    def compare(task_id: str, mode: str = "yesterday", metric: str = "rtt",
                window_hours: int = 0, node_id: str = "", dns: str = "", url: str = ""):
        """按小时对比：最近24小时 vs 昨日/上周同日/30天前。走 1h 聚合表。

        metric 决定比什么（历史缺陷：只比 rtt_avg，curl/mtr 该列为 NULL → 整页空白）：
          rtt   延迟均值（ms，仅 ping 类有）
          avail 可用率（%，所有任务类型都有）
          loss  丢包率（%，ping 类有）
        """
        t_now = now()
        offsets = {"yesterday": 86400, "lastweek": 7 * 86400, "lastmonth": 30 * 86400}
        off = offsets.get(mode, 86400)
        labels = {"yesterday": "昨日", "lastweek": "上周同日", "lastmonth": "30天前"}
        metrics = {"rtt": ("rtt_avg", "ms", 1.0),
                   "avail": ("avail_rate", "%", 100.0),
                   "loss": ("loss_rate", "%", 100.0)}
        col, unit, scale = metrics.get(metric, metrics["rtt"])

        with s.lock:
            hmin = s.db.execute("SELECT MIN(ts) mn FROM probe_results").fetchone()["mn"] or 0
        history_hours = round((t_now - hmin) / 3600, 1) if hmin else 0

        def hourly(start: int, span: int) -> dict:
            end = start + span
            if node_id:
                rows = s.agg_read("1h", task_id, node_id, dns, url, start, end)
            else:
                rows = s.agg_buckets_existing("1h", task_id, start, end)
            return {r["ts"]: r for r in rows}

        cur_start = t_now // 3600 * 3600
        if mode == "prev":
            # 「前一时段」：窗口自适应 —— 从上限（默认 历史/2、最多 12h）往下找第一个
            # 「两个时段都有数据」的窗口，保证刚上线的平台也能看到一条真正有两根线的对比
            limit = int(window_hours) if window_hours else max(1, min(12, int(history_hours // 2) or 1))
            picked = None
            for w in range(limit, 0, -1):
                span = w * 3600
                hrs = list(range(cur_start - (w - 1) * 3600, cur_start + 3600, 3600))
                t_b = hourly(cur_start - (w - 1) * 3600, span)
                o_b = hourly(cur_start - (w - 1) * 3600 - span, span)
                if sum(1 for h in hrs if h in t_b) and sum(1 for h in hrs if h in o_b):
                    picked = (w, hrs, t_b, o_b)
                    break
            if picked is None:
                w = limit
                span = w * 3600
                hrs = list(range(cur_start - (w - 1) * 3600, cur_start + 3600, 3600))
                picked = (w, hrs, hourly(cur_start - (w - 1) * 3600, span),
                          hourly(cur_start - (w - 1) * 3600 - span, span))
            w, hours, today, other = picked
            label, today_label, used_window = f"前 {w} 小时", f"最近 {w} 小时", w
        else:
            hours = list(range(cur_start - 23 * 3600, cur_start + 3600, 3600))
            today = hourly(cur_start - 23 * 3600, 86400)
            other = hourly(cur_start - off - 23 * 3600, 86400)
            label, today_label, used_window = labels.get(mode, mode), "最近24小时", 24

        def series(by_h, offset: int = 0):
            """把「对比时段」的桶按偏移对齐回当前时段的时间轴。

            历史 bug：对比桶是按 start-off 取出来的（键是偏移后的时间戳），
            却仍用未偏移的 hours 去查 → 对比线恒为空，三种模式都只剩同一条当前曲线。
            """
            out = []
            for h in hours:
                r = by_h.get(h - offset)
                v = r[col] if r is not None else None
                out.append(round(v * scale, 2) if v is not None else None)
            return out

        shift = span if mode == "prev" else off
        today_s, other_s = series(today), series(other, shift)
        return {"mode": mode, "metric": metric, "unit": unit, "label": label,
                "today_label": today_label, "window_hours": used_window,
                "has_other": any(v is not None for v in other_s),
                "has_today": any(v is not None for v in today_s),
                "history_from": hmin, "history_hours": history_hours,
                "hours": hours, "today": today_s, "other": other_s}

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
        fields = {}
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
                            ("silence_seconds", 0, 7 * 86400)):
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
