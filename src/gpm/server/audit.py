"""gpm 操作审计：把 HTTP 写请求翻成中文动作 / 目标类型，并落库、查询、导出。

模块只用标准库（csv / io / re / time），不 print、不 logging；storage 只用到三条接口：

- storage.audit_add(ts, who, action, target, target_id="", status=0, ip="", detail="")
- storage.audit_list(limit=100, action="", target="", since=0)
- storage.audit_counts()  （本模块不直接调用，留给 UI 统计用）

设计约定：

- 动作 / 目标类型的映射表 _RULES 覆盖 api_web.py 里注册的全部写路由
  （任务 / 节点 / 分组 / Token / 告警渠道 / 规则 / 维护窗口 / 评估 / 报表推送 /
  GeoIP 网段 / 导出），未知路径一律回落，绝不对请求抛异常；
- status 是 HTTP 状态码，0 表示「未记录」，在 ok 里按 False 处理；
- 时间戳统一用 time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) 格式化。
"""
from __future__ import annotations

import csv
import io
import re
import time

__all__ = ["describe", "target_id", "is_mutating", "record", "query", "to_csv",
           "CSV_HEADER", "WRITE_METHODS"]

# 需要审计的写方法（GET/HEAD/OPTIONS 等只读方法不算）
WRITE_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))

# CSV 列顺序（调用方自行决定是否加 BOM）
CSV_HEADER = ("时间", "操作者", "动作", "目标类型", "目标ID", "状态", "来源IP", "结果", "详情")

# 规则表：(适用的方法集合, 路径段模式, 中文动作, 目标类型)
# 路径段模式以 "api" 开头，逐段精确匹配；"*" 匹配任意单个路径段。
# 先具体后笼统，靠「段数相同」天然区分同前缀的不同子资源。
_RULES = (
    # ---------- 节点侧接口（/api/agent/*：不进审计，但 describe 也认得，便于 UI 展示）----------
    (frozenset({"POST"}), ("api", "agent", "register"), "节点注册", "节点接口"),
    (frozenset({"POST"}), ("api", "agent", "sync"), "节点同步配置", "节点接口"),
    (frozenset({"POST"}), ("api", "agent", "results"), "节点上报数据", "节点接口"),

    # ---------- 探测任务 ----------
    (frozenset({"POST"}), ("api", "tasks"), "新建任务", "任务"),
    (frozenset({"POST"}), ("api", "tasks", "*", "run"), "立即执行任务", "任务"),
    (frozenset({"POST"}), ("api", "tasks", "*", "enable"), "启用任务", "任务"),
    (frozenset({"POST"}), ("api", "tasks", "*", "disable"), "停用任务", "任务"),
    (frozenset({"PUT", "PATCH"}), ("api", "tasks", "*"), "修改任务", "任务"),
    (frozenset({"DELETE"}), ("api", "tasks", "*"), "删除任务", "任务"),

    # ---------- 节点 ----------
    (frozenset({"POST"}), ("api", "nodes"), "新建节点", "节点"),
    (frozenset({"POST"}), ("api", "nodes", "*", "enable"), "启用节点", "节点"),
    (frozenset({"POST"}), ("api", "nodes", "*", "disable"), "停用节点", "节点"),
    (frozenset({"PUT", "PATCH"}), ("api", "nodes", "*"), "修改节点", "节点"),
    (frozenset({"DELETE"}), ("api", "nodes", "*"), "删除节点", "节点"),

    # ---------- 节点分组 ----------
    (frozenset({"POST"}), ("api", "groups"), "新建分组", "节点分组"),
    (frozenset({"PUT", "PATCH"}), ("api", "groups", "*", "members"), "设置分组成员", "节点分组"),
    (frozenset({"PUT", "PATCH"}), ("api", "groups", "*"), "修改分组", "节点分组"),
    (frozenset({"DELETE"}), ("api", "groups", "*"), "删除分组", "节点分组"),

    # ---------- 注册 Token ----------
    (frozenset({"POST"}), ("api", "tokens"), "新建 Token", "注册 Token"),
    (frozenset({"PUT", "PATCH"}), ("api", "tokens", "*"), "修改 Token", "注册 Token"),
    (frozenset({"DELETE"}), ("api", "tokens", "*"), "删除 Token", "注册 Token"),

    # ---------- GeoIP 自定义网段 ----------
    (frozenset({"POST"}), ("api", "geo", "networks"), "新增 GeoIP 网段", "GeoIP 网段"),
    (frozenset({"PUT", "PATCH"}), ("api", "geo", "networks", "*"), "修改 GeoIP 网段", "GeoIP 网段"),
    (frozenset({"DELETE"}), ("api", "geo", "networks", "*"), "删除 GeoIP 网段", "GeoIP 网段"),

    # ---------- 告警：通知渠道 ----------
    (frozenset({"POST"}), ("api", "alerts", "channels", "*", "test"), "测试通知渠道", "通知渠道"),
    (frozenset({"POST"}), ("api", "alerts", "channels"), "新建通知渠道", "通知渠道"),
    (frozenset({"PUT", "PATCH"}), ("api", "alerts", "channels", "*"), "修改通知渠道", "通知渠道"),
    (frozenset({"DELETE"}), ("api", "alerts", "channels", "*"), "删除通知渠道", "通知渠道"),

    # ---------- 告警：规则 ----------
    (frozenset({"POST"}), ("api", "alerts", "rules", "*", "test"), "测试告警规则", "告警规则"),
    (frozenset({"POST"}), ("api", "alerts", "rules"), "新建告警规则", "告警规则"),
    (frozenset({"PUT", "PATCH"}), ("api", "alerts", "rules", "*"), "修改告警规则", "告警规则"),
    (frozenset({"DELETE"}), ("api", "alerts", "rules", "*"), "删除告警规则", "告警规则"),

    # ---------- 告警：维护窗口 ----------
    (frozenset({"POST"}), ("api", "alerts", "windows"), "新建维护窗口", "维护窗口"),
    (frozenset({"PUT", "PATCH"}), ("api", "alerts", "windows", "*"), "修改维护窗口", "维护窗口"),
    (frozenset({"DELETE"}), ("api", "alerts", "windows", "*"), "删除维护窗口", "维护窗口"),

    # ---------- 告警：评估 / 确认 ----------
    (frozenset({"POST"}), ("api", "alerts", "evaluate"), "手动评估告警", "告警评估"),
    (frozenset({"POST"}), ("api", "alerts", "ack"), "确认告警", "告警"),
    (frozenset({"POST"}), ("api", "alerts", "*", "ack"), "确认告警", "告警"),
    (frozenset({"POST"}), ("api", "alerts", "*", "resolve"), "处理告警", "告警"),

    # ---------- 报表 / 推送 ----------
    (frozenset({"POST"}), ("api", "report", "push"), "推送报表", "报表"),
    (frozenset({"POST"}), ("api", "report", "send"), "推送报表", "报表"),
    (frozenset({"POST"}), ("api", "report", "digest"), "推送巡检报告", "报表"),
    (frozenset({"POST"}), ("api", "report", "subscribe"), "订阅报表", "报表"),
    (frozenset({"POST"}), ("api", "report", "*", "push"), "推送报表", "报表"),

    # ---------- 告警：通知重投队列 ----------
    (frozenset({"POST"}), ("api", "alerts", "outbox", "*", "retry"), "立即重投通知", "通知队列"),
    (frozenset({"DELETE"}), ("api", "alerts", "outbox", "*"), "删除重投记录", "通知队列"),

    # ---------- 巡检报告推送设置 ----------
    (frozenset({"PUT", "PATCH"}), ("api", "report", "digest", "settings"), "修改巡检推送设置", "巡检报告"),
    (frozenset({"POST"}), ("api", "report", "digest", "push"), "推送巡检报告", "巡检报告"),

    # ---------- 事件详情 / 确认 ----------
    (frozenset({"POST"}), ("api", "event", "*", "ack"), "确认事件", "事件"),

    # ---------- 数据导出 ----------
    (frozenset({"POST", "GET"}), ("api", "export"), "导出数据", "导出"),
)

# 看起来像目标 id 的路径段：
#   t1 / n9 / ch1 / r1 / g1 / tk1a2b3c4 / ar0f1e2d3（new_id 的「前缀 + 十六进制」）
#   纯数字（123 / 42）、UUID
_ID_RE = re.compile(
    r"^(?:"
    r"\d{1,18}"
    r"|[A-Za-z]{1,6}[0-9a-fA-F]{1,32}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r")$"
)


# 不是 id 的已知路径字面量：像 "uptime" / "sync" / "sla" / "resolve" 这类
# 「字母 + 十六进制字符」的普通单词会被 _ID_RE 误判成 id，这里显式排除。
_NON_ID_SEGMENTS = frozenset((
    "api", "agent", "tasks", "nodes", "metrics", "groups", "members", "tokens",
    "geo", "networks", "places", "flows", "alerts", "channels", "rules", "windows",
    "evaluate", "ack", "resolve", "report", "sla", "daily", "digest", "push", "send",
    "subscribe", "export", "register", "sync", "results", "test", "overview", "query",
    "uptime", "series", "streams", "curl_codes", "mtr", "incidents", "incidents_all",
    "compare", "detail", "health", "static", "run", "enable", "disable", "data",
    "add", "edit", "update", "delete", "list", "get", "set", "reset", "start", "stop",
    "check", "status", "info", "all",
))


def _clean(path) -> str:
    """去掉 query / fragment，保证是字符串。"""
    p = "" if path is None else str(path)
    p = p.split("?", 1)[0].split("#", 1)[0]
    return p


def _segments(path) -> list:
    """把路径拆成非空段："/api/tasks/t1/" -> ["api", "tasks", "t1"]。"""
    return [s for s in _clean(path).split("/") if s]


def describe(method: str, path: str) -> tuple:
    """把 HTTP 方法与路径翻成中文：(动作, 目标类型)。

    例::

        ("POST", "/api/tasks")                     -> ("新建任务", "任务")
        ("PUT",  "/api/tasks/t123")                -> ("修改任务", "任务")
        ("DELETE", "/api/nodes/n9")                -> ("删除节点", "节点")
        ("POST", "/api/alerts/channels/ch1/test")  -> ("测试通知渠道", "通知渠道")
        ("POST", "/api/agent/results")             -> ("节点上报数据", "节点接口")

    未知路径回落为 (method + " " + 资源段, "其它")，资源段取 /api 之后的第一段
    （"/api/xxx/yyy" -> "POST xxx"），便于在 UI 上仍能看出改的是哪类资源；任何输入都不抛异常。
    """
    m = ("" if method is None else str(method)).upper()
    segs = _segments(path)
    for methods, pattern, action, target in _RULES:
        if m not in methods or len(segs) != len(pattern):
            continue
        if all(pat == "*" or seg.lower() == pat for seg, pat in zip(segs, pattern)):
            return action, target
    rest = [s for s in segs if s.lower() != "api"]
    fallback = (m + " " + (rest[0] if rest else "")).strip() or m
    return fallback, "其它"


def target_id(path: str) -> str:
    """取路径里最后一个「看起来像 id」的路径段，取不到返回空串。

    例::

        /api/tasks/t1                  -> "t1"
        /api/alerts/channels/ch1/test  -> "ch1"（末尾的 test 不是 id）
        /api/tasks                     -> ""
    """
    for seg in reversed(_segments(path)):
        if seg.lower() in _NON_ID_SEGMENTS:
            continue
        if _ID_RE.match(seg):
            return seg
    return ""


def is_mutating(method: str, path: str) -> bool:
    """是否是需要审计的写操作。

    方法在 POST/PUT/PATCH/DELETE，且路径以 /api/ 开头、不以 /api/agent 开头。
    """
    m = ("" if method is None else str(method)).upper()
    if m not in WRITE_METHODS:
        return False
    p = _clean(path)
    if not p.startswith("/api/"):
        return False
    if p.startswith("/api/agent"):
        return False
    return True


def record(storage, *, method: str, path: str, status: int, who: str, ip: str, ts: int,
           detail: str = "", action: str = "") -> dict:
    """写一条审计记录并返回写入的行（dict）。

    动作 / 目标类型来自 describe，目标 id 来自 target_id；
    传 action 时用调用方给定的动作（如 update_task 里的「启用任务/停用任务」，
    describe 从 PUT 路径只能翻出笼统的「修改任务」），目标类型仍走 describe；
    实际落库交给 storage.audit_add，本函数不写 SQL。
    """
    derived, target = describe(method, path)
    tid = target_id(path)
    row_id = storage.audit_add(ts, who, action or derived, target, target_id=tid,
                               status=status, ip=ip, detail=detail)
    return {"id": row_id, "ts": ts, "who": who, "action": action or derived, "target": target,
            "target_id": tid, "status": status, "ip": ip, "detail": detail}


def _fmt_ts(ts) -> str:
    """epoch 秒 -> "YYYY-MM-DD HH:MM:SS"（本地时区）；脏数据回落到 0。"""
    try:
        sec = int(ts or 0)
    except (TypeError, ValueError):
        sec = 0
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))
    except (OverflowError, OSError, ValueError):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(0))


def _status(status) -> int:
    """HTTP 状态码归一成 int；缺失 / 脏值按 0（未记录）处理。"""
    try:
        return int(status or 0)
    except (TypeError, ValueError):
        return 0


def query(storage, limit: int = 100, action: str = "", target: str = "",
          since: int = 0) -> list:
    """读审计（storage.audit_list）并为每条补上可读字段。

    返回字段：id / ts / time / who / action / target / target_id / status / ip /
    detail / ok；ok 为 0 < status < 400（status=0 表示未记录，按 False 处理）。
    """
    rows = storage.audit_list(limit=limit, action=action, target=target, since=since)
    out = []
    for r in rows or ():
        src = dict(r)
        ts = _status(src.get("ts"))
        st = _status(src.get("status"))
        out.append({
            "id": src.get("id"),
            "ts": ts,
            "time": _fmt_ts(ts),
            "who": src.get("who", ""),
            "action": src.get("action", ""),
            "target": src.get("target", ""),
            "target_id": src.get("target_id", "") or "",
            "status": st,
            "ip": src.get("ip", "") or "",
            "detail": src.get("detail", "") or "",
            "ok": 0 < st < 400,
        })
    return out


def _result_text(status: int, ok) -> str:
    """结果列：成功 / 失败 / 未记录（status=0 即「未记录」）。"""
    if ok is None:
        ok = 0 < status < 400
    if ok:
        return "成功"
    if status == 0:
        return "未记录"
    return "失败"


def to_csv(rows: list) -> str:
    """导出 CSV 文本（不含 BOM，调用方自己加）。

    列：时间,操作者,动作,目标类型,目标ID,状态,来源IP,结果,详情；
    逗号 / 引号 / 换行等特殊字符交给 csv 模块转义。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows or ():
        src = dict(r)
        st = _status(src.get("status"))
        writer.writerow([
            src.get("time") or _fmt_ts(src.get("ts")),
            src.get("who", "") or "",
            src.get("action", "") or "",
            src.get("target", "") or "",
            src.get("target_id", "") or "",
            st,
            src.get("ip", "") or "",
            _result_text(st, src.get("ok")),
            src.get("detail", "") or "",
        ])
    return buf.getvalue()
