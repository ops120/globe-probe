"""Web API：任务管理、节点、查询（条带/曲线/对比/明细/事件）、导出。"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import __version__
from ..common.models import NodeUpdate, TaskCreate, TaskUpdate, validate_params
from ..common.util import (
    listen_is_loopback,
    new_id,
    now,
    sha256,
    validate_domain,
    validate_host_port,
    validate_target,
)
from . import alerting, geo, hooks
from .diagnose import classify, runbook_for, verdict
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


_BUCKET_RANK = {"live": 0, "silent": 1, "stale": 2, "maintenance": 3}


# 「同一节点上多少个任务同时失败才给一张『疑似节点侧』横切卡」与「同期变更窗口」
# 是产品口径旋钮，从 cfg.view 读（view.node_suspect_min_tasks / changes_pad_seconds /
# changes_per_card）。曾经这里各放一份硬编码常量，与 eventview 的同名配置各管各的——
# 改配置只影响事件详情页、值班页纹丝不动，两页口径被改裂。
def _view_int(cfg_view: dict, key: str, default: int) -> int:
    try:
        return max(0, int(cfg_view.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _oncall_group_subtitle(count: int, nodes: list, targets: list) -> str:
    """一张聚合卡的副标题：说清「这一条代表了哪些东西」。"""
    bits = ["%d 条流" % count]
    if len(nodes) > 1:
        bits.append("%d 个节点" % len(nodes))
    elif nodes:
        bits.append(nodes[0])
    if len(targets) > 1:
        bits.append("%d 个目标" % len(targets))
    return " · ".join(bits)


CHANGES_PAD_SECONDS = 1800      # 事件窗口 ±30 分钟
CHANGES_PER_CARD = 3            # 卡片上只留最有用的几条（完整清单仍在事件详情弹窗里）


def _oncall_changes(s, items: list, t_now: int, pad_seconds: int = 1800,
                    per_card: int = 3) -> dict:
    """同期变更（第三期 14）：事件窗口 ±pad_seconds 内、**动过这个东西**的写操作。

    原先只有事件详情弹窗里有——值班的人要先点开弹窗才知道「这期间有人改过配置」，
    而「刚改完就炸」是排查时最省时间的线索之一。

    实现上按**一次查询**取齐所有事件的并集窗口，再在内存里按 target_id / 名称分发
    （逐条查会退化成 N+1，而 /api/oncall 是首屏接口）。只保留与「该任务/该节点」
    直接相关的记录：泛泛列 8 条无关审计等于噪声。
    """
    probe = [it for it in items if it["task_id"]]
    if not probe:
        return {}
    pad = pad_seconds
    t_from = min(int(it["started_at"] or t_now) for it in probe) - pad
    rows = s.audit_list(limit=500, since=t_from) or []
    out: dict = {}
    for it in probe:
        lo = int(it["started_at"] or t_now) - pad
        hi = t_now + pad
        keys = {str(it["task_id"]), str(it["node_id"])}
        names = [str(it.get("task_name") or ""), str(it.get("node_name") or "")]
        # 显式标注：字面量字典会被推断成 dict[str, object]，sort 的 key 就不可比了
        picked: list[dict] = []
        for r in rows:
            ts = int(r.get("ts") or 0)
            if ts < lo or ts > hi:
                continue
            rid = str(r.get("target_id") or "")
            detail = str(r.get("detail") or "")
            if rid not in keys and not any(n and n in detail for n in names):
                continue
            head = str(r.get("target") or "")
            picked.append({"ts": ts, "who": str(r.get("who") or ""),
                           "action": str(r.get("action") or ""),
                           "detail": " · ".join(x for x in (head, detail) if x)})
        if picked:
            picked.sort(key=lambda x: x["ts"], reverse=True)
            out[it["incident_id"]] = picked[:per_card]
    return out


# 第三方告警 ↔ 本地事件的关联口径：从 cfg.hook 读（hook.link_window_seconds /
# link_min_name_len）。这两个配置键曾经是死键——仓库里只有这里的硬编码常量在生效，
# 改配置毫无作用。cfg 经 app 启动的 init() 注入（与 hooks/pullers 等模块同模式）。
_CFG = None


def init(cfg) -> None:
    global _CFG
    _CFG = cfg


def _ext_link_window() -> int:
    try:
        return max(0, int((_CFG.hook.get("link_window_seconds") if _CFG else None) or 1800))
    except (TypeError, ValueError):
        return 1800


def _ext_link_min_name() -> int:
    try:
        return max(1, int((_CFG.hook.get("link_min_name_len") if _CFG else None) or 2))
    except (TypeError, ValueError):
        return 2


def _ext_tokens(a: dict) -> set:
    """第三方告警的可搜索词元：标题 + 各标签值。

    用 \\w（Unicode）而不是 [0-9a-z]：后者会把**中文名整体切碎**，于是中文任务名/节点名
    永远匹配不上（线上实测「e2e-关联目标」就是被切成 "e2e-" 的）。本产品的任务名允许中文，
    这里必须按 Unicode 词字符切。
    """
    bits = [str(a.get("title") or "")]
    labels = a.get("labels") or {}
    if isinstance(labels, dict):
        bits += [str(v) for v in labels.values()]
    return {t for t in re.split(r"[^\w\-\.]+", " ".join(bits).lower()) if t}


def _correlate_external(s, alert: dict, t_now: int, window: int | None = None) -> int:
    """把第三方告警关联到本地事件（第六期 28）。

    依据：本地事件的**任务名/节点名**出现在第三方告警的标题或标签里，且时间窗重叠。
    这是「接了第三方反而把第一屏重新塞满」的解药 —— 只有关联不上的外部告警才单独成卡，
    关联上的折叠成本地卡片的**旁证**。

    用词元匹配而不是子串匹配：否则节点 n1 会命中 n10、任务 t 会命中一切。
    名字 ≥4 字符时额外允许子串（"win-local" 藏在更长标签值里的情形）。
    """
    window = _ext_link_window() if window is None else int(window)
    toks = _ext_tokens(alert)
    if not toks:
        return 0
    tasks = {t["id"]: t for t in s.list_tasks()}
    nodes = {n["id"]: n for n in s.list_nodes()}
    start = int(alert.get("started_at") or alert.get("received_at") or t_now)
    end = int(alert.get("ended_at") or 0) or t_now
    lo, hi = start - window, end + window
    text = " ".join(toks)
    n = 0
    for inc in s.list_incidents(limit=200, t_from=lo, t_to=hi):
        tname = str((tasks.get(str(inc.get("task_id") or "")) or {}).get("name") or "")
        nname = str((nodes.get(str(inc.get("node_id") or "")) or {}).get("name") or "")
        hits = []
        for nm in (tname, nname):
            low = nm.lower()
            if len(low) < _ext_link_min_name():
                continue
            if low in toks or (len(low) >= 4 and low in text):
                hits.append(nm)
        if not hits:
            continue
        if s.external_alert_link(int(alert["id"]), int(inc["id"]), t_now,
                                 "目标/节点名匹配：%s" % "/".join(hits)):
            n += 1
    return n


def _oncall_groups(items: list, t_now: int, ext_orphans: list | None = None,
                   node_suspect_min_tasks: int = 3) -> list:
    """把逐流事件聚合为「行动项」：同一任务一张卡 + 按节点横切提示。

    .docs/ONCALL_OPTIMIZATION_2.md 第二期 7-9。线上实测 11 条事件里同一任务占多条
    （curl-baidu-multi 3 条 URL、ping-223 2 个节点），值班的人得在第一屏手动合并；
    而「同一节点上 N 个任务同时失败」这个最有价值的相关性信号完全没有暴露——
    它往往就是根因提示（节点侧坏了，而不是每个目标各自坏了）。

    分组只做**展示层合并**：底层每条事件都保留在 members 里，可展开、可单独确认。
    """
    def b_of(it: dict) -> str:
        if it.get("maintenance"):
            return "maintenance"
        return it.get("bucket") or "live"

    by_task: dict = {}
    node_events: list = []
    for it in items:
        if it["task_id"]:
            by_task.setdefault(it["task_id"], []).append(it)
        else:
            node_events.append(it)

    groups: list = []
    for tid, members in by_task.items():
        members = sorted(members, key=lambda x: (x.get("last_ts") or 0), reverse=True)
        head = members[0]
        nodes = sorted({m["node_name"] for m in members if m.get("node_name")})
        targets = sorted({(m["url"] or m["dns"] or "") for m in members
                          if (m.get("url") or m.get("dns"))})
        started = min(int(m["started_at"] or t_now) for m in members)
        groups.append({
            "key": "task:" + tid, "kind": "task", "task_id": tid,
            "title": head["task_name"], "type": head["type"],
            "bucket": min((b_of(m) for m in members),
                          key=lambda b: _BUCKET_RANK.get(b, 9)),
            "layer": head["layer"], "advice": head["advice"],
            "error_class": head["error_class"],
            "count": len(members),
            "incident_ids": [m["incident_id"] for m in members],
            "nodes": nodes, "targets": targets,
            "node_id": head["node_id"], "node_name": head["node_name"],
            "started_at": started, "duration_s": max(0, t_now - started),
            "last_ts": max(int(m["last_ts"] or 0) for m in members),
            "acked": all(m["acked"] for m in members),
            "maintenance": next((m.get("maintenance") for m in members
                                 if m.get("maintenance")), None),
            "runbook": next((m.get("runbook") for m in members if m.get("runbook")), ""),
            "changes": next((m.get("changes") for m in members if m.get("changes")), []),
            "external": next((m.get("external") for m in members if m.get("external")), []),
            "subtitle": _oncall_group_subtitle(len(members), nodes, targets),
            "members": members,
        })

    for it in node_events:
        groups.append({
            "key": "nodeev:%s" % it["incident_id"], "kind": "node",
            "task_id": "", "title": (it.get("node_name") or it.get("node_id") or "节点"),
            "type": "", "bucket": b_of(it), "layer": it["layer"], "advice": it["advice"],
            "error_class": "", "count": 1, "incident_ids": [it["incident_id"]],
            "nodes": [it.get("node_name") or ""], "targets": [],
            "node_id": it["node_id"], "node_name": it["node_name"],
            "started_at": int(it["started_at"] or t_now),
            "duration_s": it.get("duration_s") or 0,
            "last_ts": it.get("last_ts") or 0, "acked": it["acked"],
            "maintenance": it.get("maintenance"), "subtitle": "节点事件",
            "runbook": it.get("runbook") or "", "changes": [],
            "external": it.get("external") or [], "members": [it],
        })

    # 横切：同一节点上 ≥N 个任务同时失败 → 一张置顶的「疑似节点侧」卡。
    # 只统计「正在失败」的任务：沉默/陈旧本来就没有新样本，不能作为节点侧证据。
    live_by_node: dict = {}
    for g in groups:
        if g["kind"] != "task" or g["bucket"] != "live":
            continue
        for m in g["members"]:
            live_by_node.setdefault(m["node_id"], set()).add(m["task_id"])
    for nid, tids_set in sorted(live_by_node.items()):
        if len(tids_set) < node_suspect_min_tasks:
            continue
        members = [m for g in groups if g["kind"] == "task"
                   for m in g["members"] if m["node_id"] == nid and m["task_id"] in tids_set]
        nname = (members[0].get("node_name") if members else "") or nid
        started = min(int(m["started_at"] or t_now) for m in members)
        groups.append({
            "key": "nodesuspect:" + nid, "kind": "node_suspect",
            "task_id": "", "title": "%s · 疑似节点侧" % nname, "type": "",
            "bucket": "live",
            "layer": "节点侧",
            "advice": ("该节点上 %d 个任务同时失败：优先查节点出口/资源（CPU、内存）"
                       "与该节点共用的链路，而不是逐个目标排查" % len(tids_set)),
            "error_class": "",
            "count": len(members),
            "incident_ids": [m["incident_id"] for m in members],
            "nodes": [nname], "targets": sorted({m["task_name"] for m in members}),
            "node_id": nid, "node_name": nname,
            "started_at": started, "duration_s": max(0, t_now - started),
            "last_ts": max(int(m["last_ts"] or 0) for m in members),
            "acked": False,
            "maintenance": None,
            "runbook": runbook_for("节点侧"), "changes": [], "external": [],
            "subtitle": "%d 个任务同时失败" % len(tids_set),
            "members": members,
        })

    # 第六期 28/29：**关联不上**本地事件的第三方 firing 告警单独成卡。关联上的已经
    # 折叠进本地卡片的旁证里，不会在这里重复出现 —— 这正是「接了第三方不会把第一屏
    # 重新塞满」的机制。它们没有本地探测数据，所以只给「去源侧看」的入口，不编造层面。
    for a in (ext_orphans or []):
        started = int(a.get("started_at") or a.get("received_at") or t_now)
        groups.append({
            "key": "ext:%s:%s" % (a.get("source"), a.get("id")), "kind": "external",
            "task_id": "", "title": str(a.get("title") or a.get("source_id") or "第三方告警"),
            "type": "", "bucket": "live", "layer": "第三方",
            "advice": ("来自 %s 的告警：本平台没有该目标的直接探测数据，"
                       "先用源侧链接看详情；如需本地证据，给该目标建一个探测任务"
                       % (a.get("source") or "")),
            "error_class": "", "count": 1, "incident_ids": [],
            "nodes": [], "targets": [],
            "node_id": "", "node_name": "",
            "started_at": started, "duration_s": max(0, t_now - started),
            "last_ts": int(a.get("received_at") or 0), "acked": False,
            "maintenance": None, "runbook": "", "changes": [],
            "subtitle": "%s · 第三方" % (a.get("source") or ""),
            "external": [a], "members": [],
        })

    # 排序（第二期 9）：横切提示置顶 → 分档（正在失败 > 沉默 > 陈旧 > 维护中）
    # → 影响面（覆盖多少条流）→ 持续时长。原先是按事件开始时间倒序，一屏里
    # 最重要的「现在真在坏、且影响面大」的那条不一定在最上面。
    groups.sort(key=lambda g: (
        0 if g["kind"] == "node_suspect" else 1,
        _BUCKET_RANK.get(g["bucket"], 9),
        -int(g["count"] or 0),
        -int(g["duration_s"] or 0),
    ))
    return groups


def setup_router(app_state) -> APIRouter:
    s = app_state["storage"]
    cfg = app_state["cfg"]
    router = APIRouter(prefix="/api")  # 每次调用独立 router，避免跨 app 闭包污染

    # 写口鉴权（安全基线）：admin_token 未配置时曾是 fail-open——配合示例配置的
    # 0.0.0.0 监听等于局域网内任何人可建任务/导入配置/改通知渠道。收敛为：
    # 配了 token → 常量时间比较；没配 token → 仅环回监听放行（本机开发模式），
    # 非环回监听一律 403（app 启动时会打 WARNING 指引配置 token）。
    _admin_token = str(cfg.server.get("admin_token") or "")
    _loopback_listen = listen_is_loopback(str(cfg.server.get("listen") or ""))

    def check_write(x_admin_token: str | None):
        if _admin_token:
            if not secrets.compare_digest(str(x_admin_token or "").encode(),
                                          _admin_token.encode()):
                raise HTTPException(403, "需要 X-Admin-Token")
        elif not _loopback_listen:
            raise HTTPException(
                403, "未配置 server.admin_token 且监听在非环回地址，写接口已拒绝"
                     "（配置 admin_token 后重启服务端开放）")

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
            ok_n = total_n = 0
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
            # 最后数据时间：任务管理表格用它判断「任务是否还在跑」（0=近 24h 无任何数据）
            d["last_data_ts"] = last_ts or None
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

    def _invalidate_tasks_cache() -> None:
        """任务 CRUD 后调用：让「我刚改完就看到」生效，而不是等最多 15s。

        写路径上无 24h 可用率 / streams / current_status 的缓存，因此这里只清
        任务列表缓存；TTL 仍按 cfg.server["tasks_cache_seconds"] 走。
        """
        with _tasks_lock:
            _tasks_cache["ver"] = None

    @router.post("/tasks")
    def create_task(body: TaskCreate, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        probe = cfg.probe
        interval = max(body.interval_seconds, _interval_floor(probe, body.type))
        tid = new_id("t")
        try:
            t = s.create_task(tid, body.name, body.type, body.target, body.urls, body.params,
                              body.dns, interval, now(), nodes=body.nodes)
        except sqlite3.IntegrityError:
            # 曾经用 "UNIQUE" in str(e) 判重——sqlite 报错文案不可契约，撞名/主键冲突应按类型捕
            raise HTTPException(409, f"任务名已存在: {body.name}")
        _invalidate_tasks_cache()
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
        _invalidate_tasks_cache()
        return updated

    @router.delete("/tasks/{tid}")
    def delete_task(tid: str, x_admin_token: str | None = Header(default=None)):
        check_write(x_admin_token)
        s.delete_task(tid, now())
        _invalidate_tasks_cache()
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
                    st = 0 if avail >= 1 else 1  # 部分失败按失败展示（曾写成恒真的内层三元）
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
        # 维护窗口：in_maintenance() 早已存在，但此前只作用于告警评估——值班页不认它，
        # 于是计划内维护会以「正在失败」的样子占着第一屏（第二期 10）。
        for it in items:
            mw = s.in_maintenance(t_now, task_id=it["task_id"], node_id=it["node_id"])
            it["maintenance"] = ({"name": mw.get("name") or "维护窗口",
                                  "ends_at": mw.get("ends_at")} if mw else None)
        # 第三期 13/14：卡片上直接给「可粘贴的命令」与「同期变更」。
        # 口径从 cfg.view 读，与事件详情页（eventview）同一套配置键
        view_cfg = cfg.view
        chg = _oncall_changes(s, items, t_now,
                              pad_seconds=_view_int(view_cfg, "changes_pad_seconds", 1800),
                              per_card=_view_int(view_cfg, "changes_per_card", 3))
        for it in items:
            it["changes"] = chg.get(it["incident_id"], [])
            it["runbook"] = runbook_for(it["layer"])
        pub = alerting.public_url(s)
        # 第四期 16/17：先排除「监控自己坏了」，再谈故障 ——
        #   ① 节点资源饱和度：实测 win-local 长期 CPU 90~95%，这种节点上的失败先怀疑节点自身；
        #   ② 本平台可信度三数：探测新鲜度（最近样本距今）/ 渠道可用 / 事件自愈（不可信事件数）。
        #      不可信事件数就是 §三 那三条自查 SQL，正常恒为 0，>0 时页面自己报警。
        from . import metrics as _metrics
        with s.lock:
            _last_res = s.db.execute("SELECT MAX(ts) m FROM probe_results").fetchone()["m"] or 0
        _zombie = s.zombie_incidents()
        selfcheck = {
            "probe_age_s": (t_now - int(_last_res)) if _last_res else None,
            "channels": _metrics.channel_health(s, t_now),
            "zombie_events": sum(len(v) for v in _zombie.values()),
            "zombie_detail": _zombie,
        }
        nodes_health = [{
            "node_id": n["id"], "name": n["name"], "status": n.get("status") or "",
            "cpu": n.get("cpu"), "mem": n.get("mem"), "streams": n.get("hb_tasks") or 0,
            "heartbeat_age_s": ((t_now - int(n["last_heartbeat"]))
                                if n.get("last_heartbeat") else None),
        } for n in nodes.values()]
        # 第六期 28/29：本地事件带「旁证」（已关联的第三方告警），未关联的第三方
        # firing 告警单独成卡。关联上的不会重复占屏 —— 这就是「接了第三方不会把第一屏
        # 重新塞满」的机制；反过来，关联不上说明本平台没在探这个目标，也值得看见。
        ext_links = s.external_alert_links_for([it["incident_id"] for it in items])
        for it in items:
            it["external"] = ext_links.get(it["incident_id"], [])
        linked_ids = {a["id"] for lst in ext_links.values() for a in lst}
        ext_orphans = [a for a in s.list_external_alerts(limit=100, firing_only=True)
                       if a["id"] not in linked_ids]
        # 聚合后的「行动项」：同一任务一张卡 + 按节点横切（第二期 7-9）。
        # items 保持原样返回，前端与既有验收断言不受影响。
        return {"ts": t_now, "items": items,
                "groups": _oncall_groups(items, t_now, ext_orphans,
                                         node_suspect_min_tasks=_view_int(cfg.view, "node_suspect_min_tasks", 3)),
                "selfcheck": selfcheck, "nodes_health": nodes_health,
                "external": {"firing": len(ext_orphans), "linked": len(linked_ids)},
                # 第三期 12：未配置 public_url 时通知里**没有**「点击查看」链接，
                # 页面上要显著提示，否则运维只会以为「链接坏了」。
                "public_url": pub, "public_url_configured": bool(pub)}

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

    def _dl(payload: str, media_type: str, base: str, ext: str) -> StreamingResponse:
        return StreamingResponse(io.StringIO(payload), media_type=media_type,
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{base}.{ext}"'})

    def _csv_or_json(rows: list[dict], columns: list[str], base: str,
                     fmt: str) -> StreamingResponse:
        """CSV/JSON 双格式导出。CSV 统一前置 UTF-8 BOM——否则 Excel 打开中文必乱码。"""
        if fmt == "json":
            return _dl(json.dumps(rows, ensure_ascii=False, indent=1),
                       "application/json", base, "json")
        buf = io.StringIO()
        buf.write("\ufeff")                  # UTF-8 BOM：Excel 中文兼容
        w = csv.writer(buf)
        w.writerow(columns)
        for d in rows:
            w.writerow([json.dumps(d[c], ensure_ascii=False)
                        if isinstance(d.get(c), (dict, list)) else d.get(c, "")
                        for c in columns])
        return _dl(buf.getvalue(), "text/csv;charset=utf-8", base, "csv")

    def _export_cap(limit: int) -> int:
        """cfg.export.max_rows 是硬上限；调用方可用 limit 再收紧，但不能放大。"""
        cap = max(1, int(cfg.export.get("max_rows", 100000) or 100000))
        return min(max(1, int(limit or cap)), cap)

    @router.get("/export")
    def export(task_id: str, t_from: int = 0, t_to: int = 0, fmt: str = "csv"):
        t_to = t_to or now()
        t_from = t_from or (t_to - 86400)
        with s.lock:
            rows = s.db.execute(
                "SELECT ts,task_id,node_id,type,dns,url,status,error_class,error,dns_server,"
                "resolved_ip,dns_time_ms,metrics_json FROM probe_results WHERE task_id=? "
                "AND ts>=? AND ts<=? ORDER BY ts LIMIT ?",
                (task_id, t_from, t_to, _export_cap(0))).fetchall()
        data = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            data.append(d)
        return _csv_or_json(data, ["ts", "task_id", "node_id", "type", "dns", "url", "status",
                                   "error_class", "error", "dns_server", "resolved_ip",
                                   "dns_time_ms", "metrics"], f"export_{task_id}", fmt)

    # ---------- 导出：记录类（GET 开放，与查询端点同一信任模型；行数受 cfg.export.max_rows 约束） ----------

    @router.get("/export/incidents")
    def export_incidents(t_from: int = 0, t_to: int = 0, fmt: str = "csv", limit: int = 0):
        """事件导出（复盘/周报用）。默认最近 7 天，按开始时间倒序；open=是否仍未恢复。"""
        t_to = t_to or now()
        t_from = t_from or (t_to - 7 * 86400)
        with s.lock:
            rows = s.db.execute(
                "SELECT * FROM incidents WHERE started_at>=? AND started_at<=?"
                " ORDER BY started_at DESC LIMIT ?",
                (t_from, t_to, _export_cap(limit))).fetchall()
        data = []
        for r in rows:
            d = dict(r)
            try:
                d["reason"] = json.loads(d.pop("reason_json") or "{}")
            except Exception:
                d["reason"] = {}
            d["open"] = 1 if not d.get("ended_at") else 0
            data.append(d)
        return _csv_or_json(data, ["id", "task_id", "node_id", "dns", "url", "kind", "open",
                                   "started_at", "ended_at", "duration_ms", "last_ts",
                                   "reopen_count", "note", "reason"], "gpm-incidents", fmt)

    @router.get("/export/alerts")
    def export_alerts(t_from: int = 0, t_to: int = 0, rule_id: str = "", fmt: str = "csv",
                      limit: int = 0):
        """告警发送记录导出（alerts 表：规则命中 + 投递结果）。默认最近 7 天。"""
        t_to = t_to or now()
        t_from = t_from or (t_to - 7 * 86400)
        with s.lock:
            sql = "SELECT * FROM alerts WHERE ts>=? AND ts<=?"
            args: list = [t_from, t_to]
            if rule_id:
                sql += " AND rule_id=?"
                args.append(rule_id)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(_export_cap(limit))
            rows = s.db.execute(sql, args).fetchall()
        return _csv_or_json([dict(r) for r in rows],
                            ["ts", "rule_id", "rule_name", "metric", "key", "status", "severity",
                             "title", "text", "target_json", "delivered", "n_channels", "n_ok",
                             "detail"], "gpm-alerts", fmt)

    @router.get("/export/external_alerts")
    def export_external_alerts(source: str = "", status: str = "", t_from: int = 0,
                               fmt: str = "csv", limit: int = 0):
        """第三方告警导出（库里已按 source+source_id 幂等去重后的形态）。"""
        items = s.list_external_alerts(limit=_export_cap(limit), source=source, status=status,
                                       t_from=t_from)
        return _csv_or_json(items, ["id", "source", "source_id", "title", "severity", "status",
                                    "started_at", "ended_at", "received_at", "updated_at",
                                    "url", "labels"], "gpm-external-alerts", fmt)

    # ---------- 配置备份：导入导出（换库/迁移不用手工重建任务与通知链路） ----------

    @router.get("/export/config")
    def export_config():
        """配置备份 JSON：任务/分组/渠道/规则/维护窗口。

        故意不含 tokens（agent 凭据）与 nodes（运行时注册产物，agent 会自己回来）。
        渠道 config 含 webhook 密钥——可见性与通知配置页一致（内网信任模型），备份文件请妥善保管。
        """
        task_keys = ("id", "name", "type", "target", "urls", "params", "dns",
                     "interval_seconds", "enabled", "nodes")
        return {
            "kind": "gpm-config-backup",
            "gpm_version": __version__,
            "exported_at": now(),
            "config_version": s.config_version(),
            "tasks": [{k: t.get(k) for k in task_keys} for t in s.list_tasks()],
            "groups": [{"id": g["id"], "name": g["name"], "note": g.get("note") or "",
                        "members": g.get("members") or []} for g in s.list_groups()],
            "channels": [{k: c.get(k) for k in ("id", "name", "type", "config", "enabled")}
                         for c in s.list_channels()],
            "rules": [{k: r.get(k) for k in ("id", "name", "metric", "op", "threshold",
                                             "window_seconds", "task_id", "node_id",
                                             "group_id", "severity", "channel_ids",
                                             "silence_seconds", "escalate_minutes", "enabled")}
                      for r in s.list_rules()],
            "windows": [{k: w.get(k) for k in ("id", "name", "starts_at", "ends_at",
                                               "task_id", "node_id", "note")}
                        for w in s.list_windows()],
        }

    @router.post("/import/config")
    def import_config(body: dict, x_admin_token: str | None = Header(default=None)):
        """配置导入：upsert 语义（同 id 已存在 → 更新；不存在 → 创建，保留原 id）。

        任务 id 保留是为了让规则里的 task_id 引用在迁移后仍然成立。
        单项失败不拖垮整体：错误逐条收集在返回值里，其余照常导入。
        """
        check_write(x_admin_token)
        if not isinstance(body, dict) or not any(
                isinstance(body.get(k), list)
                for k in ("tasks", "groups", "channels", "rules", "windows")):
            raise HTTPException(status_code=400,
                                detail="不像 gpm 配置备份（缺 tasks/groups/channels/rules/windows 数组）")
        ts = now()
        res: dict = {}

        def bucket(name: str) -> dict:
            return res.setdefault(name, {"created": 0, "updated": 0, "errors": []})

        for t in body.get("tasks") or []:
            b = bucket("tasks")
            try:
                tid = str(t.get("id") or "").strip()
                if not tid:
                    raise ValueError("缺 id")
                ttype = str(t.get("type") or "http")
                params = t.get("params") or {}
                validate_params(ttype, params)
                interval = max(int(t.get("interval_seconds") or 30),
                               _interval_floor(cfg.probe, ttype))
                urls = t.get("urls") or []
                dns = t.get("dns") or []
                nodes = t.get("nodes") or []
                if s.get_task(tid):
                    s.update_task(tid, {"name": t.get("name") or tid,
                                        "target": t.get("target") or "", "urls": urls,
                                        "params": params, "dns": dns, "nodes": nodes,
                                        "interval_seconds": interval,
                                        "enabled": 1 if t.get("enabled", True) else 0}, ts)
                    b["updated"] += 1
                else:
                    s.create_task(tid, str(t.get("name") or tid), ttype,
                                  str(t.get("target") or ""), urls, params, dns, interval, ts,
                                  nodes=nodes)
                    if not t.get("enabled", True):     # create_task 恒为启用，停用态需补一刀
                        s.update_task(tid, {"enabled": 0}, ts)
                    b["created"] += 1
            except Exception as e:
                b["errors"].append(f"任务 {t.get('id') or '?'}: {e}")

        for g in body.get("groups") or []:
            b = bucket("groups")
            try:
                gid = str(g.get("id") or "").strip()
                if not gid:
                    raise ValueError("缺 id")
                if any(x["id"] == gid for x in s.list_groups()):
                    s.update_group(gid, {"name": g.get("name") or gid,
                                         "note": g.get("note") or ""}, ts)
                    b["updated"] += 1
                else:
                    s.create_group(gid, g.get("name") or gid, g.get("note") or "", ts)
                    b["created"] += 1
                s.set_group_members(gid, [str(m) for m in (g.get("members") or [])], ts)
            except Exception as e:
                b["errors"].append(f"分组 {g.get('id') or '?'}: {e}")

        for c in body.get("channels") or []:
            b = bucket("channels")
            try:
                cid = str(c.get("id") or "").strip()
                if not cid:
                    raise ValueError("缺 id")
                fields = {"name": c.get("name") or cid, "type": c.get("type") or "webhook",
                          "config": c.get("config") or {}}
                if any(x["id"] == cid for x in s.list_channels()):
                    if "enabled" in c:
                        fields["enabled"] = 1 if c.get("enabled") else 0
                    s.update_channel(cid, fields, ts)
                    b["updated"] += 1
                else:
                    s.create_channel(cid, fields["name"], fields["type"], fields["config"], ts)
                    if "enabled" in c and not c.get("enabled"):
                        s.update_channel(cid, {"enabled": 0}, ts)
                    b["created"] += 1
            except Exception as e:
                b["errors"].append(f"渠道 {c.get('id') or '?'}: {e}")

        for w in body.get("windows") or []:
            b = bucket("windows")
            try:
                wid = str(w.get("id") or "").strip()
                if not wid:
                    raise ValueError("缺 id")
                fields = {"name": w.get("name") or "维护窗口",
                          "starts_at": int(w.get("starts_at") or 0),
                          "ends_at": int(w.get("ends_at") or 0),
                          "task_id": w.get("task_id") or "",
                          "node_id": w.get("node_id") or "", "note": w.get("note") or ""}
                if any(x["id"] == wid for x in s.list_windows()):
                    s.delete_window(wid)
                    s.create_window(wid, fields, ts)
                    b["updated"] += 1
                else:
                    s.create_window(wid, fields, ts)
                    b["created"] += 1
            except Exception as e:
                b["errors"].append(f"维护窗口 {w.get('id') or '?'}: {e}")

        for r in body.get("rules") or []:
            b = bucket("rules")
            try:
                rid = str(r.get("id") or "").strip()
                if not rid:
                    raise ValueError("缺 id")
                fields = {"name": r.get("name") or rid, "metric": r.get("metric") or "",
                          "op": r.get("op") or "", "threshold": r.get("threshold"),
                          "window_seconds": r.get("window_seconds"),
                          "task_id": r.get("task_id") or "", "node_id": r.get("node_id") or "",
                          "group_id": r.get("group_id") or "",
                          "severity": r.get("severity") or "warning",
                          "channel_ids": r.get("channel_ids") or [],
                          "silence_seconds": r.get("silence_seconds"),
                          "escalate_minutes": r.get("escalate_minutes"),
                          "enabled": 1 if r.get("enabled", True) else 0}
                if any(x["id"] == rid for x in s.list_rules()):
                    s.update_rule(rid, fields, ts)
                    b["updated"] += 1
                else:
                    s.create_rule(rid, fields, ts)
                    b["created"] += 1
            except Exception as e:
                b["errors"].append(f"规则 {r.get('id') or '?'}: {e}")

        _invalidate_tasks_cache()      # 任务可能被改/建，让任务列表立刻生效而不是等 TTL
        return {"ok": True, "imported": res, "config_version": s.config_version()}

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
        # 动态基线（第十期）：必须指定任务；params 走可行域联动校验；op/threshold 给中性默认
        if metric == "anomaly":
            if not str(body.get("task_id") or "").strip():
                raise HTTPException(422, "动态基线规则必须指定任务（适用范围=指定任务）")
            params = body.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                # 严格区分 None（缺省）与 []/""（非法）——`params or {}` 会把后者悄悄吃掉
                raise HTTPException(422, "params 必须是对象")
            from . import baseline as _bl
            errs = _bl.validate_params(params)
            if errs:
                raise HTTPException(422, "；".join(errs))
            out["params"] = params
            out.setdefault("op", "gt")
            out.setdefault("threshold", 0.0)
        elif body.get("params") is not None:
            raise HTTPException(422, "仅动态基线（metric=anomaly）支持 params")
        # 更新场景：partial 时 metric 未随行就不动 params（防止只改名称把 params 清成 {}）
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
        if metric == "anomaly":
            # 强制项放在**所有字段处理之后**（每次实测都被后一段通用循环写回）：
            # 不开事件 → 升级链无依据（_escalations 亦已跳过）；评估按全任务流走，
            # 静默丢弃 node 范围会让 API 直连规则与 UI 强制 task 范围语义不一致
            out["escalate_minutes"] = 0
            out["node_id"] = ""
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
    @router.get("/baseline")
    def baseline_view(task_id: str, metric_field: str = "avail_rate",
                      k: float | None = None, direction: str | None = None,
                      min_samples: int | None = None, min_consecutive: int | None = None,
                      window_mode: str | None = None, baseline_days: int | None = None,
                      baseline_from: str | None = None, align: str | None = None,
                      exclude_windows: str = "[]"):
        """动态基线预览（第十期）：规则弹窗「预览基线带」与排障核对用。只读聚合表。

        返回 evaluable/基线数字/每条流的 z 序列；不可行域参数直接 422（对齐×天数×样本
        联动校验与规则保存同口径）。"""
        from . import baseline as _bl
        # 显式 0 是有意义的输入（k=0 会被钳到下限），不能用真值门「当未传」
        p: dict = {"metric_field": metric_field}
        for key, val in (("k", k), ("direction", direction), ("min_samples", min_samples),
                         ("min_consecutive", min_consecutive), ("window_mode", window_mode),
                         ("baseline_days", baseline_days), ("baseline_from", baseline_from),
                         ("align", align)):
            if val not in (None, ""):
                p[key] = val
        try:
            excludes = json.loads(exclude_windows or "[]")
        except ValueError:
            raise HTTPException(422, "exclude_windows 必须是 JSON 数组")
        if excludes:
            p["exclude_windows"] = excludes
        errs = _bl.validate_params(p)
        if errs:
            raise HTTPException(422, "；".join(errs))
        if task_id not in {t["id"] for t in s.list_tasks()}:
            raise HTTPException(404, "任务不存在")
        return _bl.baseline(s, task_id, p, now())

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
        """立即评估一轮规则（不等后台周期），返回本轮事件。

        多实例下必须走**单执行者租约**（与后台循环同一把 alert 租约）：否则两台
        同时手动触发会各评估一遍，同一规则+目标写两行告警、外发两条通知
        （实测动态验证：6 条外发应为 3 条）。未持租约时如实返回 skipped，
        不静默评估。"""
        check_write(x_admin_token)
        lease = (app_state.get("leases") or {}).get("alert")
        if lease is not None and not lease.hold():
            return {"events": [], "skipped": True,
                    "reason": "本实例未持有告警单执行者租约（另一实例正在评估）"}
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

    # ---------- 第三方告警（第六期 24/25/27/28/29/30）----------
    @router.post("/hooks/{source}")
    async def hook_receive(source: str, request: Request):
        """接收第三方告警（Grafana / Zabbix / 腾讯云 / GCP）。

        安全（第六期 30）：必须配置该来源的接入 Token 并带上；**未配置时拒绝接收**
        —— 不提供「无鉴权也能往库里写告警」的默认。另有单次 payload 上限与每来源限流。
        只读：本接口不产生任何对外请求，第三方系统不会被我们回写。
        """
        if source not in hooks.SOURCES:
            raise HTTPException(404, "未知来源：%s（可选：%s）"
                            % (source, ", ".join(hooks.SOURCES)))
        try:
            clen = int(request.headers.get("content-length") or 0)
        except ValueError:
            clen = 0
        if clen > hooks.MAX_BODY_BYTES:
            raise HTTPException(413, "payload 过大（>%d 字节）" % hooks.MAX_BODY_BYTES)
        if not hooks.expected_token(s, source):
            raise HTTPException(401, "未配置该来源的接入 Token，拒绝接收"
                                     "（到「通知配置 → 第三方接入」设置）")
        if not hooks.token_ok(s, source, request.headers.get(hooks.TOKEN_HEADER),
                              request.query_params.get(hooks.TOKEN_QUERY)):
            raise HTTPException(401, "接入 Token 不匹配")
        raw = await request.body()
        if len(raw) > hooks.MAX_BODY_BYTES:
            raise HTTPException(413, "payload 过大（>%d 字节）" % hooks.MAX_BODY_BYTES)
        if not hooks.rate_ok(source):
            raise HTTPException(429, "接收过于频繁，请稍后再试")
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "payload 不是合法 JSON")
        # 官方签名校验（预留扩展点，2026-10-06 决议 #5）：已注册 verifier 的来源在此
        # 强制执行（False 一律 401，宁拒勿放）；未注册来源返回 None 按共享 Token
        # 模式放行——Token 校验在上方，仍是硬门槛。放在限流之后：签名运算不做免费算力。
        sig_ok, sig_msg = hooks.validate_signature(source, request.headers, body)
        if sig_ok is False:
            raise HTTPException(401, "签名校验失败：%s" % sig_msg)
        ts = now()
        alerts = hooks.parse(source, body)
        ids = []
        for a in alerts:
            # raw 落库前脱敏（键名命中敏感词 / URL 里的 token 与 userinfo 一律抹掉）
            aid, _created = s.external_alert_upsert(
                source, str(a["source_id"]),
                dict(a, raw=hooks.redact(a.get("raw") or {})), ts)
            ids.append(aid)
            rec = s.external_alert_get(aid)
            if rec:
                _correlate_external(s, rec, ts)      # 关联去重：能折进本地卡的就不单独成卡
        return {"source": source, "received": len(alerts), "ids": ids, "ts": ts}

    @router.get("/external/alerts")
    def external_alerts(limit: int = 100, source: str = "", status: str = "",
                        firing_only: bool = False, days: int = 0):
        t_from = now() - max(0, int(days)) * 86400 if days else 0
        items = s.list_external_alerts(limit=max(1, min(limit, 500)), source=source,
                                       status=status, t_from=t_from,
                                       firing_only=firing_only)
        # 旁证关系一并返回：前端要知道「哪些已经折进本地卡片了」。
        # 入参是**告警 id**（按告警侧反查）——曾把告警 id 传给按事件 id 过滤的
        # external_alert_links_for，两套 id 撞巧才显示对旁证
        link_map = s.external_alert_link_map([i["id"] for i in items])
        for i in items:
            i["linked_incident"] = link_map.get(i["id"])
        return {"items": items, "count": len(items)}

    @router.get("/external/summary")
    def external_summary(days: int = 1):
        """按来源汇总（第六期 29）：报表里要能看出「这些告警从哪来、还开着多少」。"""
        return s.external_alert_summary(now(), max(0, min(int(days), 365)))

    @router.get("/external/settings")
    def external_settings():
        """接入配置状态。**不返回 Token 本身**，只说配没配。"""
        return {"sources": [{"source": src, "configured": bool(hooks.expected_token(s, src))}
                            for src in hooks.SOURCES],
                "token_header": hooks.TOKEN_HEADER, "token_query": hooks.TOKEN_QUERY,
                "signature_modes": hooks.signature_modes(),
                "hint": ("每家都要配一个接入 Token 才会接收；未配置时该来源一律 401。"
                         "官方签名校验为预留扩展点（signature_modes 标注各来源当前模式），"
                         "待各家凭据到位后注册即生效；共享 Token 当前是唯一硬门槛。"),
                "limits": {"max_body_bytes": hooks.MAX_BODY_BYTES,
                           "rate_per_min": hooks.RATE_LIMIT_PER_MIN}}

    @router.put("/external/settings")
    def external_settings_set(body: dict, x_admin_token: str | None = Header(default=None)):
        """设置接入 Token：`hook_token` 通用，`hook_token_<source>` 覆盖某一家。"""
        check_write(x_admin_token)
        for key in ("hook_token",) + tuple("hook_token_%s" % x for x in hooks.SOURCES):
            if key in body:
                s.setting_set(key, str(body[key] or "").strip()[:200])
        return external_settings()

    # ---------- JEV 故障判断（第七期 31-38）----------
    @router.get("/jev/{iid}")
    def jev_get(iid: int):
        """取已有的 JEV 轨迹（可回放）；没有则 404。"""
        from . import jev as _jev
        tr = _jev.load(s, iid)
        if not tr:
            raise HTTPException(404, "该事件还没有 JEV 判断轨迹")
        return tr

    @router.post("/jev/{iid}/run")
    def jev_run(iid: int, force: bool = False, x_admin_token: str | None = Header(default=None)):
        """跑一次 JEV 判断（或复用已落盘的轨迹）。

        **前置门禁（第七期 38）**：不可信事件数 != 0 时**直接拒绝**，不调用判据 ——
        输入若是僵尸/陈旧证据，模型只会把噪声包装成结论。
        """
        check_write(x_admin_token)   # 写口：与其余 36 处一致（P2-1），曾漏配
        from . import eventview as _ev
        from . import jev as _jev
        if not force:
            cached = _jev.load(s, iid)
            if cached:
                return cached
        zombies = s.zombie_incidents()
        n_zombie = sum(len(v) for v in zombies.values())
        if n_zombie:
            raise HTTPException(409, "证据不可信：存在 %d 条不可信事件（僵尸/陈旧），"
                                     "请先让收口逻辑跑完再判断；本轮不调用判据"
                                     % n_zombie)
        try:
            detail = _ev.detail(s, iid)
        except KeyError:
            raise HTTPException(404, "事件不存在")
        trace = _jev.run(detail, storage=s)
        trace["incident_id"] = iid
        _jev.save(s, trace)
        return trace

    @router.get("/external/pull")
    def external_pull_state():
        """拉取适配器状态（第六期 26）。**如实区分**「支持但没配地址」「已配好」「未实现」。"""
        from . import pullers
        return {"sources": [pullers.state(s, src) for src in hooks.SOURCES],
                "hint": ("webhook 推不到（内网隔离 / 源侧不支持推送）时可改用 API 拉。"
                         "本期**只实现了 Grafana(Alertmanager v2 /alerts) 与 Zabbix"
                         "(JSON-RPC trigger.get)**；腾讯云需要 TC3-HMAC 签名、GCP 需要 OAuth，"
                         "未实现——不会假装成功。")}

    @router.put("/external/pull/{source}")
    def external_pull_set(source: str, body: dict,
                          x_admin_token: str | None = Header(default=None)):
        """配置某来源的拉取：地址 / Token / 开关 / 周期。"""
        check_write(x_admin_token)
        if source not in hooks.SOURCES:
            raise HTTPException(404, "未知来源：%s" % source)
        from . import pullers
        if body.get("url") is not None:
            s.setting_set("pull_%s_url" % source, str(body["url"] or "").strip()[:500])
        if body.get("token") is not None:
            s.setting_set("pull_%s_token" % source, str(body["token"] or "").strip()[:200])
        if body.get("enabled") is not None:
            s.setting_set("pull_%s_enabled" % source, "1" if body["enabled"] else "0")
        if body.get("interval_seconds") is not None:
            try:
                iv = int(body["interval_seconds"])
            except (TypeError, ValueError):
                raise HTTPException(422, "interval_seconds 必须是整数")
            s.setting_set("pull_interval_seconds", str(max(60, min(iv, 86400))))
        return pullers.state(s, source)

    @router.post("/external/pull/{source}/run")
    def external_pull_run(source: str, x_admin_token: str | None = Header(default=None)):
        """立即拉一次（人工验证用）。返回如实结果：成功条数 / 未实现 / 错误原因。"""
        check_write(x_admin_token)
        if source not in hooks.SOURCES:
            raise HTTPException(404, "未知来源：%s" % source)
        from . import pullers
        return pullers.poll_source(s, source)

    @router.post("/external/correlate")
    def external_correlate(days: int = 7, x_admin_token: str | None = Header(default=None)):
        """手动重跑关联（新增了任务/节点、或改了名字之后用）。"""
        check_write(x_admin_token)
        ts = now()
        t_from = ts - max(1, min(int(days), 90)) * 86400
        n = linked = 0
        for a in s.list_external_alerts(limit=500, t_from=t_from):
            n += 1
            linked += _correlate_external(s, a, ts)
        return {"scanned": n, "linked": linked, "ts": ts}

    # ---------- 同时段故障关联分析（第八期）----------
    @router.get("/correlation")
    def correlation_view(t_from: int = 0, t_to: int = 0, hours: int = 6):
        """同一时段所有故障（本地事件+外部告警）的聚合与相关关系。

        只对齐可证明的维度（同节点/同目标/同线路/旁证/时间聚集），假设一律
        「疑似」措辞并带证据计数；每簇附 CMDB 信息缺口（补哪些字段能进一步
        自动定位）。只读、现算，不改任何状态。"""
        from . import correlation as _corr
        ts = now()
        t_to_v = int(t_to or ts)
        t_from_v = int(t_from or (ts - max(1, min(int(hours), 24 * 7)) * 3600))
        return _corr.analyze(s, t_from_v, t_to_v)

    # ---------- 通知深链前缀（第三期 11/12）----------
    @router.get("/settings/public-url")
    def public_url_get():
        """当前深链前缀。未配置时最好用的排障线索就是「配置在哪、怎么设」。"""
        v = alerting.public_url(s)
        return {"public_url": v, "configured": bool(v),
                "hint": ("未配置：通知里不会有「点击查看」链接（不编造链接）。"
                         "可在 config.yaml 的 server.public_url 设置，或在本页直接保存。")}

    @router.put("/settings/public-url")
    def public_url_set(body: dict, x_admin_token: str | None = Header(default=None)):
        """配置通知深链前缀（如 https://gpm.example.com）。空串=不带链接。

        历史缺陷：这个值原先只有 setting_get 一条来源，**没有任何地方写过它** ——
        没有 config 键也没有接口，等于线上根本配不了，于是每条通知都没有【链接】段落。
        """
        check_write(x_admin_token)
        v = str(body.get("public_url") or "").strip().rstrip("/")
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise HTTPException(422, "public_url 必须以 http:// 或 https:// 开头")
        s.setting_set("public_url", v)
        return {"public_url": v, "configured": bool(v)}

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
