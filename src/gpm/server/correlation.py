"""同时段故障聚合与相关关系分析（第八期）。

回答的问题：**「同一时段里的这些故障，是不是同一件事？」**——值班的人最怕的不是
一条故障，是十条互相不知是否相关的故障卡满一屏。

设计边界（与 jev 同一纪律：不编造）：

- **只对齐可证明的维度**：同一节点 / 同一任务 / 同一目标域名 / 同一 DNS 线路 /
  同错误类别 / 旁证关联（外部告警已挂到本地事件）/ 时间聚集。每个假设都带
  「几条里几条命中」的证据计数，绝不输出没有证据的因果链。
- **只给假设不给结论**：措辞一律「疑似」。时间聚集但没有公共维度的簇，明说
  「共同原因未定」，并指出补充哪些 CMDB 字段可以提升定位。
- **不聚合的也如实列出**：零散故障进 singles，不给它们硬凑关系。

时间口径：故障区间 = [started_at, ended_at || started_at + burst]。
两故障「时间聚集」= 区间有交集，或起点相差 ≤ burst_seconds（突发窗口）。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..common.util import now
from ..config import Config

# —— 以下阈值由 cfg.corr.* 提供；模块级同名常量与 DEFAULTS 对齐，运行期 init(cfg) 覆盖。
BURST_SECONDS = 300        # 突发窗口：起点相差 ≤ N 秒视为「同时段」
MIN_EDGE = 3               # 两故障连边的最低证据权重（旁证关联=4 必连）
MAX_MEMBERS = 12           # 簇成员上限：超过说明是全局风暴，关联分析已无粒度价值
WINDOW_MAX_HOURS = 24 * 7  # 单次分析窗口上限

_cfg: Config | None = None


def init(cfg) -> None:
    global _cfg, BURST_SECONDS, MIN_EDGE, MAX_MEMBERS, WINDOW_MAX_HOURS
    _cfg = cfg
    try:
        c = cfg.corr
        BURST_SECONDS = max(30, int(c.get("burst_seconds", BURST_SECONDS) or BURST_SECONDS))
        MIN_EDGE = max(2, int(c.get("min_edge", MIN_EDGE) or MIN_EDGE))
        MAX_MEMBERS = max(2, int(c.get("max_members", MAX_MEMBERS) or MAX_MEMBERS))
        WINDOW_MAX_HOURS = max(1, int(c.get("window_max_hours", WINDOW_MAX_HOURS) or WINDOW_MAX_HOURS))
    except Exception:
        pass


# 无 CMDB 时承载资产属性的最小标签键（完整字段设计见 .docs/CMDB_FIELDS.md）。
# 相关性引擎用它们判断「能否进一步定位」；缺失即信息缺口，如实报告而不是硬猜。
CMDB_CORE_KEYS = ("owner", "biz", "service", "env", "region", "isp", "depends_on")
_CMDB_GAP_NOTE = {
    "owner": "故障无负责人标签 → 无法自动 @ 到人，只能拉群找人",
    "biz": "无业务/服务归属 → 无法把「节点故障」翻译成「影响哪个业务」",
    "env": "无环境标记 → 分不清生产/演练故障，排障优先级靠猜",
    "region": "无地域标签 → 无法验证「同地域同时故障=机房侧」这类假设",
    "isp": "无运营商标签 → 无法验证「同运营商用户故障=链路侧」假设",
    "depends_on": "无依赖关系 → 「同一上游连累一片」只能靠人脑回忆拓扑",
    "service": "无服务归属 → 故障无法聚合到服务维度",
}

# 维度 → 中文假设文案。权重：连边强度的主观量化，节点/目标类维度是排障第一入口。
_DIM_LABELS = {
    "link": "外部旁证",
    "node": "同一节点",
    "host": "同一目标/域名",
    "task": "同一任务",
    "dns": "同一 DNS 线路",
    "errclass": "同类错误",
    "burst": "时间聚集",
}


def _host_of(url_or_target: str) -> str:
    s = str(url_or_target or "").strip()
    if not s:
        return ""
    if "://" in s:
        try:
            return (urlparse(s).hostname or "").lower()
        except ValueError:
            return ""
    return s.split("/")[0].split(":")[0].lower()


_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)+", re.I)


def _hosts_in_text(text: str) -> set:
    return {m.group(0).lower().rstrip(".") for m in _DOMAIN_RE.finditer(str(text or ""))}


def _fault_of_incident(inc: dict, tasks: dict, nodes: dict) -> dict:
    task = tasks.get(str(inc.get("task_id") or "")) or {}
    node = nodes.get(str(inc.get("node_id") or "")) or {}
    hosts: set = set()
    if inc.get("url"):
        hosts.add(_host_of(str(inc.get("url") or "")))
    for u in task.get("urls") or []:
        h = _host_of(str(u or ""))
        if h:
            hosts.add(h)
    h = _host_of(str(task.get("target") or ""))
    if h:
        hosts.add(h)
    reason = inc.get("reason")
    if not isinstance(reason, dict):
        reason = {}
    return {
        "kind": "incident",
        "ref": int(inc.get("id") or 0),
        "title": "【%s】%s @ %s" % ("节点事件" if inc.get("kind") == "node" else "探测故障",
                                    task.get("name") or inc.get("kind") or "?",
                                    node.get("name") or inc.get("node_id") or "?"),
        "task_id": str(inc.get("task_id") or ""),
        "task_name": str(task.get("name") or ""),
        "node_id": str(inc.get("node_id") or ""),
        "node_name": str(node.get("name") or ""),
        "node_tags": dict(node.get("tags") or {}),
        "source": "local",
        "start": int(inc.get("started_at") or 0),
        "end": int(inc.get("ended_at") or 0),
        "open": inc.get("ended_at") is None,
        "error_class": str(reason.get("error_class") or ""),
        "dns": str(inc.get("dns") or ""),
        "hosts": hosts,
        "labels": {},
    }


def _fault_of_external(a: dict) -> dict:
    labels = a.get("labels")
    if not isinstance(labels, dict):
        labels = {}
    hosts = set()
    h = _host_of(str(a.get("url") or ""))
    if h:
        hosts.add(h)
    for k in ("instance", "host", "hostname", "service", "target"):
        for v in _hosts_in_text(str(labels.get(k) or "")):
            hosts.add(v)
    for v in _hosts_in_text(str(a.get("title") or "")):
        hosts.add(v)
    return {
        "kind": "external",
        "ref": int(a.get("id") or 0),
        "title": "【外部·%s】%s" % (a.get("source") or "?", a.get("title") or a.get("source_id") or "?"),
        "task_id": "",
        "task_name": "",
        "node_id": "",
        "node_name": "",
        "node_tags": {},
        "source": str(a.get("source") or ""),
        "start": int(a.get("started_at") or a.get("received_at") or 0),
        "end": int(a.get("ended_at") or 0),
        "open": a.get("status") != "resolved",
        "error_class": "",
        "dns": "",
        "hosts": hosts,
        "labels": {str(k): str(v) for k, v in labels.items()},
    }


def _overlap(fa: dict, fb: dict) -> bool:
    """时间聚集：区间有交集，或起点相差 ≤ burst（突发窗口）。"""
    b_len = BURST_SECONDS
    ea = fa["end"] or (fa["start"] + b_len)
    eb = fb["end"] or (fb["start"] + b_len)
    return fa["start"] <= eb and fb["start"] <= ea


def _pair_dims(fa: dict, fb: dict) -> list:
    """两故障的全部命中维度：[(dim, 说明)]。只对齐事实，不做推断。

    （外部告警 ↔ 本地事件的旁证关联在 analyze 里按 linked_incident 处理，
    因为它需要库里的关联表而不是成对比较。）"""
    dims = []
    if fa["kind"] == "incident" and fb["kind"] == "incident":
        if fa["node_id"] and fa["node_id"] == fb["node_id"]:
            dims.append(("node", "节点 %s" % (fa["node_name"] or fa["node_id"])))
        if fa["task_id"] and fa["task_id"] == fb["task_id"]:
            dims.append(("task", "任务 %s" % (fa["task_name"] or fa["task_id"])))
        if fa["dns"] and fa["dns"] == fb["dns"]:
            dims.append(("dns", "DNS 线路 %s" % fa["dns"]))
        if fa["error_class"] and fa["error_class"] == fb["error_class"]:
            dims.append(("errclass", "错误类别 %s" % fa["error_class"]))
    common_hosts = (fa.get("hosts") or set()) & (fb.get("hosts") or set())
    if common_hosts:
        dims.append(("host", "目标/域名 %s" % ", ".join(sorted(common_hosts)[:3])))
    if _overlap(fa, fb):
        dims.append(("burst", "时间区间重叠/突发窗口内"))
    return dims


_WEIGHTS = {"link": 4, "node": 3, "host": 3, "task": 2, "dns": 2, "errclass": 1, "burst": 1}


def _cluster_hypothesis(members: list, dims_count: dict, span: dict) -> str:
    """簇假设：按连边权重从强到弱取首个命中的维度（≥1 对即成立——两成员的簇
    天然只有一对，写成 ≥2 会把最小也最常见的簇推到无假设）；旁证 > 节点 >
    目标 > 线路 > 任务。burst/errclass 是弱信号，单独出现不构成假设，
    落到兜底文案（共同原因未定）。"""
    m = len(members)
    span_txt = "%s ~ %s" % (span.get("from_text") or "?", span.get("to_text") or "?")
    if dims_count.get("link"):
        return ("其中 %d/%d 条已互为旁证（外部告警挂到本地事件）——同一故障的两种来源，"
                "排障时看一条链路即可" % (dims_count["link"], max(1, m - 1)))
    if dims_count.get("node"):
        nodes = sorted({x["node_name"] or x["node_id"] for x in members if x["kind"] == "incident"})[:3]
        return ("疑似节点侧：窗口内 %d 条故障先后集中落在节点 %s（跨度 %s；"
                "先查该节点资源/进程/网络，再怀疑目标各自坏了）"
                % (dims_count["node"], "、".join(n for n in nodes if n) or "?", span_txt))
    if dims_count.get("host"):
        hosts = sorted(set().union(*[x.get("hosts") or set() for x in members]))[:3]
        return ("疑似目标侧：%d 对故障共同指向 %s（跨度 %s；先看目标服务自身与其上游依赖）"
                % (dims_count["host"], "、".join(h for h in hosts if h) or "?", span_txt))
    if dims_count.get("dns"):
        return ("疑似解析侧：%d 对故障使用同一 DNS 线路（跨度 %s；先验证该线路当前答案与连通性）"
                % (dims_count["dns"], span_txt))
    if dims_count.get("task"):
        return ("同一任务的 %d 对流同时故障（跨度 %s）：先查任务目标与参数是否变更"
                "（见事件详情的同期变更），再查目标服务" % (dims_count["task"], span_txt))
    return ("时间上聚集（%s）但现有字段不足以认定公共原因 —— 疑似共同上游/网络路径；"
            "补齐 CMDB 的 depends_on / region / isp 字段后可自动验证" % span_txt)


def _cmdb_gaps(members: list) -> list:
    """信息缺口：按 CMDB 核心键盘点成员覆盖情况（只报事实与用途，不编）。"""
    have: set = set()
    for x in members:
        tags = x.get("node_tags") or {}
        for k, v in tags.items():
            kl = str(k).lower()
            if kl in CMDB_CORE_KEYS and str(v).strip():
                have.add(kl)
        for k in (x.get("labels") or {}):
            kl = str(k).lower()
            if kl in CMDB_CORE_KEYS:
                have.add(kl)
    return [{"key": k, "note": _CMDB_GAP_NOTE[k]} for k in CMDB_CORE_KEYS if k not in have]


def _fmt_ts(ts: int) -> str:
    import time as _t
    return _t.strftime("%m-%d %H:%M", _t.localtime(ts)) if ts else "?"


def analyze(storage, t_from: int, t_to: int = 0) -> dict:
    """窗口内全部故障（本地事件 + 外部告警）→ 相关簇 + 零散故障 + 缺口盘点。"""
    ts_now = now()
    t_to = int(t_to or ts_now)
    t_from = int(t_from or (ts_now - 6 * 3600))
    if t_to - t_from > WINDOW_MAX_HOURS * 3600:
        t_from = t_to - WINDOW_MAX_HOURS * 3600

    incidents = storage.list_incidents(limit=500, t_from=t_from, t_to=t_to)
    tasks = {str(t["id"]): t for t in storage.list_tasks()}
    nodes = {str(n["id"]): n for n in storage.list_nodes()}
    ext = [a for a in (storage.list_external_alerts(limit=500, t_from=t_from) or [])
           if int(a.get("received_at") or 0) <= t_to or int(a.get("started_at") or 0) <= t_to]
    # 旁证映射：alert_id -> incident_id。external_alert_links_for 按**事件 id** 过滤
    # （storage 的 WHERE 是 incident_id IN），所以必须传本地事件 id——传告警 id 会在
    # 两套自增 id 空间错开后静默丢光旁证边（复合核实的 P0）
    link_by_alert: dict = {}
    for iid, lst in (storage.external_alert_links_for(
            [int(i["id"]) for i in incidents]) or {}).items():
        for a in lst:
            link_by_alert[int(a["id"])] = int(iid)

    faults = [_fault_of_incident(i, tasks, nodes) for i in incidents]
    faults += [_fault_of_external(a) for a in ext]
    faults = [f for f in faults if f["start"]]
    # 主键去重（incidents 与 external id 空间独立，(kind, ref) 唯一）
    seen, uniq = set(), []
    for f in faults:
        k = (f["kind"], f["ref"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    faults = sorted(uniq, key=lambda x: x["start"])
    for f in faults:
        f["linked_incident"] = link_by_alert.get(f["ref"]) if f["kind"] == "external" else None

    n = len(faults)
    # —— 连边 + 并查集 ——
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edges: list = []
    for i in range(n):
        for j in range(i + 1, n):
            dims = _pair_dims(faults[i], faults[j])
            if faults[i]["kind"] == "external" and faults[i].get("linked_incident") \
                    and faults[j]["kind"] == "incident" \
                    and faults[j]["ref"] == faults[i]["linked_incident"]:
                dims = [("link", "外部告警已挂到该本地事件作旁证")] + dims
            if faults[j]["kind"] == "external" and faults[j].get("linked_incident") \
                    and faults[i]["kind"] == "incident" \
                    and faults[i]["ref"] == faults[j]["linked_incident"]:
                dims = [("link", "外部告警已挂到该本地事件作旁证")] + dims
            w = sum(_WEIGHTS[d[0]] for d in dims)
            # burst(1) 单独出现不构成连边证据（任何同时段的故障都会命中它）：
            # 低于 MIN_EDGE 就不连，否则「全窗口故障连成一坨」。link 权重 4 天然过线。
            if w >= MIN_EDGE:
                edges.append((i, j, dims))
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list = []
    singles: list = []
    edge_by_pair = {(e[0], e[1]): e[2] for e in edges}
    for root, idxs in groups.items():
        members = [faults[i] for i in idxs]
        if len(idxs) == 1:
            singles.append(members[0])
            continue
        idx_set = set(idxs)
        dims_count: dict = {}
        for (a, b), dims in edge_by_pair.items():
            if a in idx_set and b in idx_set:
                for d in dims:
                    dims_count[d[0]] = dims_count.get(d[0], 0) + 1
        # 支持度按「簇内对数」归一：几对成员命中了该维度
        pairs_total = max(1, len(idxs) * (len(idxs) - 1) // 2)
        start = min(x["start"] for x in members)
        end = max((x["end"] or x["start"] + BURST_SECONDS) for x in members)
        span = {"from": start, "to": min(end, t_to),
                "from_text": _fmt_ts(start), "to_text": _fmt_ts(min(end, t_to))}
        clusters.append({
            "members": members[:MAX_MEMBERS],
            "member_total": len(members),
            "truncated": len(members) > MAX_MEMBERS,
            "dims": [{"dim": k, "label": _DIM_LABELS.get(k, k),
                      "pairs": v, "pairs_total": pairs_total,
                      "support": "%d/%d" % (v, pairs_total)}
                     for k, v in sorted(dims_count.items(), key=lambda kv: -kv[1])],
            "hypothesis": _cluster_hypothesis(members, dims_count, span),
            "span": span,
            # 注意：以下计数按**全簇**统计（members 只是展示截断）
            "open_count": sum(1 for x in members if x["open"]),
            "gaps": _cmdb_gaps(members),
        })
    # 排序按全簇规模（member_total）：>MAX_MEMBERS 的簇并列时不再受截断影响
    clusters.sort(key=lambda c: -c["member_total"])

    return {
        "window": {"t_from": t_from, "t_to": t_to,
                   "from_text": _fmt_ts(t_from), "to_text": _fmt_ts(t_to),
                   "burst_seconds": BURST_SECONDS},
        "stats": {"faults": n, "incidents": sum(1 for f in faults if f["kind"] == "incident"),
                  "external": sum(1 for f in faults if f["kind"] == "external"),
                  "clusters": len(clusters), "singles": len(singles)},
        "clusters": clusters,
        "singles": [{"kind": x["kind"], "source": x["source"], "title": x["title"],
                     "status": "进行中" if x["open"] else "已恢复",
                     "start": x["start"], "start_text": _fmt_ts(x["start"]),
                     "ref": x["ref"], "task_id": x["task_id"], "node_id": x["node_id"]}
                    for x in singles[:100]],
        "note": ("维度只对齐可证明的事实（同节点/同目标/同线路/旁证等），假设措辞为「疑似」；"
                 "时间聚集但无公共维度的簇会如实说明。CMDB 缺口列出补哪些字段能进一步提升自动定位。"),
    }
