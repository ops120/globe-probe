"""tests/unit/test_audit.py —— 操作审计单测（假 storage，不联网、不碰真库）。

守护审计模块的对外契约：

1. describe 把全部写路由翻成中文动作 / 目标类型，未知路径回落且绝不抛异常；
2. target_id 取最后一个「像 id」的路径段（末尾 test / members 这类字面量不算）；
3. is_mutating 只认 /api/ 下、非 /api/agent 的 POST/PUT/PATCH/DELETE；
4. record 把参数原样透传给 storage.audit_add，并回显写入的行；
5. query 补出 time / ok（status=0 → ok False），to_csv 的表头 / 列序 / 转义正确。
"""
from __future__ import annotations

import csv
import io
import time

import pytest

from gpm.server.audit import (CSV_HEADER, WRITE_METHODS, describe, is_mutating,
                              query, record, target_id, to_csv)

# 2026-01-01 00:00:00 UTC（只用于断言 time 字段的格式化结果）
BASE = 1767225600

# api_web.py + api_agent.py 当前注册的全部写路由：describe 必须逐条命中，不能落到「其它」
WRITE_ROUTES = [
    ("POST", "/api/tasks", "新建任务", "任务"),
    ("PUT", "/api/tasks/t1a2b3c4", "修改任务", "任务"),
    ("DELETE", "/api/tasks/t1a2b3c4", "删除任务", "任务"),
    ("PUT", "/api/nodes/n1a2b3c4", "修改节点", "节点"),
    ("DELETE", "/api/nodes/n1a2b3c4", "删除节点", "节点"),
    ("POST", "/api/groups", "新建分组", "节点分组"),
    ("PUT", "/api/groups/g1a2b3c4", "修改分组", "节点分组"),
    ("DELETE", "/api/groups/g1a2b3c4", "删除分组", "节点分组"),
    ("PUT", "/api/groups/g1a2b3c4/members", "设置分组成员", "节点分组"),
    ("POST", "/api/tokens", "新建 Token", "注册 Token"),
    ("PUT", "/api/tokens/tk1a2b3c4", "修改 Token", "注册 Token"),
    ("DELETE", "/api/tokens/tk1a2b3c4", "删除 Token", "注册 Token"),
    ("POST", "/api/geo/networks", "新增 GeoIP 网段", "GeoIP 网段"),
    ("DELETE", "/api/geo/networks/gn1a2b3c4", "删除 GeoIP 网段", "GeoIP 网段"),
    ("POST", "/api/alerts/channels", "新建通知渠道", "通知渠道"),
    ("PUT", "/api/alerts/channels/ch1a2b3c4", "修改通知渠道", "通知渠道"),
    ("DELETE", "/api/alerts/channels/ch1a2b3c4", "删除通知渠道", "通知渠道"),
    ("POST", "/api/alerts/channels/ch1a2b3c4/test", "测试通知渠道", "通知渠道"),
    ("POST", "/api/alerts/rules", "新建告警规则", "告警规则"),
    ("PUT", "/api/alerts/rules/ar1a2b3c4", "修改告警规则", "告警规则"),
    ("DELETE", "/api/alerts/rules/ar1a2b3c4", "删除告警规则", "告警规则"),
    ("POST", "/api/alerts/windows", "新建维护窗口", "维护窗口"),
    ("DELETE", "/api/alerts/windows/mw1a2b3c4", "删除维护窗口", "维护窗口"),
    ("POST", "/api/alerts/evaluate", "手动评估告警", "告警评估"),
    ("POST", "/api/agent/register", "节点注册", "节点接口"),
    ("POST", "/api/agent/sync", "节点同步配置", "节点接口"),
    ("POST", "/api/agent/results", "节点上报数据", "节点接口"),
    ("POST", "/api/report/push", "推送报表", "报表"),
    ("GET", "/api/export", "导出数据", "导出"),
]


def _row(rid, ts, *, status=0, action="", target="", who="", tid="",
         ip="10.0.0.1", detail=""):
    """造一条 audit_log 原始行（列与 storage.audit_list 返回一致）。"""
    return {"id": rid, "ts": ts, "who": who, "action": action, "target": target,
            "target_id": tid, "status": status, "ip": ip, "detail": detail}


class FakeStorage:
    """假 storage：只实现审计用到的 audit_add / audit_list / audit_counts。

    audit_add 记录每次收到的参数并返回自增 id；audit_list 在内存行上做
    与真库一致的过滤 / 排序（ts, id 倒序）。
    """

    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]
        self.added = []          # audit_add 收到的 kwargs
        self.list_calls = []     # audit_list 收到的 kwargs
        self._next_id = max([int(r.get("id") or 0) for r in self.rows] or [0]) + 1

    def audit_add(self, ts, who, action, target, target_id="", status=0, ip="",
                  detail=""):
        call = {"ts": ts, "who": who, "action": action, "target": target,
                "target_id": target_id, "status": status, "ip": ip, "detail": detail}
        self.added.append(call)
        row = {"id": self._next_id, **call}
        self._next_id += 1
        self.rows.append(row)
        return row["id"]

    def audit_list(self, limit=100, action="", target="", since=0):
        self.list_calls.append({"limit": limit, "action": action, "target": target,
                                "since": since})
        picked = []
        for r in sorted(self.rows, key=lambda x: (int(x.get("ts") or 0),
                                                  int(x.get("id") or 0)), reverse=True):
            if action and r.get("action") != action:
                continue
            if target and not str(r.get("target") or "").startswith(target):
                continue
            if since and int(r.get("ts") or 0) < since:
                continue
            picked.append(dict(r))
        return picked[:limit]

    def audit_counts(self):
        return {"total": len(self.rows), "day": len(self.rows)}


# ---------------------------------------------------------------- describe

@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/tasks", ("新建任务", "任务")),
    ("PUT", "/api/tasks/t123", ("修改任务", "任务")),
    ("DELETE", "/api/nodes/n9", ("删除节点", "节点")),
    ("POST", "/api/alerts/channels/ch1/test", ("测试通知渠道", "通知渠道")),
    ("POST", "/api/agent/results", ("节点上报数据", "节点接口")),
])
def test_describe_examples_from_spec(method, path, expected):
    assert describe(method, path) == expected


@pytest.mark.parametrize("method,path,action,target", WRITE_ROUTES)
def test_describe_covers_every_write_route(method, path, action, target):
    assert describe(method, path) == (action, target)


def test_describe_unknown_path_falls_back_to_first_segment():
    # 落在 /api 之后的资源段（更可读，且与 UI 的「动作」列一致）
    assert describe("POST", "/api/unknown/thing") == ("POST unknown", "其它")
    assert describe("GET", "/api/overview") == ("GET overview", "其它")
    # 本轮新增的路由必须被识别（不要落回「其它」）
    assert describe("PUT", "/api/report/digest/settings") == ("修改巡检推送设置", "巡检报告")
    assert describe("POST", "/api/alerts/outbox/12/retry") == ("立即重投通知", "通知队列")
    assert describe("DELETE", "/api/alerts/outbox/12") == ("删除重投记录", "通知队列")
    assert describe("POST", "/api/event/225/ack") == ("确认事件", "事件")


def test_describe_strips_query_and_trailing_slash():
    assert describe("POST", "/api/tasks?dry=1") == ("新建任务", "任务")
    assert describe("POST", "/api/tasks/") == ("新建任务", "任务")
    assert describe("DELETE", "/api/nodes/n9#top") == ("删除节点", "节点")


def test_describe_never_raises_and_always_returns_two_str():
    for method in ("", "get", "POST", None):
        for path in ("", "/", "//", "/api", "/api/", "api/tasks", "/nope/x"):
            action, target = describe(method, path)
            assert isinstance(action, str) and isinstance(target, str)
            assert target          # 目标类型永远非空；方法为空时空路径才可能给出空动作


# ---------------------------------------------------------------- target_id

@pytest.mark.parametrize("path,expected", [
    ("/api/tasks", ""),
    ("/api/tasks/t1", "t1"),
    ("/api/nodes/n1", "n1"),
    ("/api/alerts/channels/ch1/test", "ch1"),
    ("/api/alerts/rules/r1", "r1"),
    ("/api/alerts/channels/ch1a2b3c4/test", "ch1a2b3c4"),
    ("/api/groups/g1/members", "g1"),
    ("/api/tasks/t1?x=1", "t1"),
    ("/api/export", ""),
    ("/api/query/uptime", ""),
    ("", ""),
])
def test_target_id(path, expected):
    assert target_id(path) == expected


# ---------------------------------------------------------------- is_mutating

@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/tasks", False),
    ("HEAD", "/api/tasks", False),
    ("OPTIONS", "/api/nodes/n1", False),
    ("POST", "/api/agent/results", False),
    ("POST", "/api/agent/register", False),
    ("DELETE", "/api/agent/sync", False),
    ("POST", "/api/tasks", True),
    ("PUT", "/api/tasks/t1", True),
    ("PATCH", "/api/nodes/n1", True),
    ("DELETE", "/api/nodes/n9", True),
    ("POST", "/api/tasks?x=1", True),
    ("delete", "/api/tokens/tk1", True),
    ("POST", "/metrics", False),
    ("POST", "api/tasks", False),
    ("POST", "/api", False),
    ("POST", "/api/agent", False),
    (None, "/api/tasks", False),
])
def test_is_mutating(method, path, expected):
    assert is_mutating(method, path) is expected


def test_write_methods_is_exactly_the_four_write_verbs():
    assert WRITE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------- record

def test_record_forwards_args_and_returns_written_row():
    st = FakeStorage()
    row = record(st, method="PUT", path="/api/tasks/t123", status=200, who="alice",
                 ip="10.0.0.5", ts=BASE, detail="间隔 10s -> 30s")
    assert st.added == [{"ts": BASE, "who": "alice", "action": "修改任务",
                         "target": "任务", "target_id": "t123", "status": 200,
                         "ip": "10.0.0.5", "detail": "间隔 10s -> 30s"}]
    assert row == {"id": 1, "ts": BASE, "who": "alice", "action": "修改任务",
                   "target": "任务", "target_id": "t123", "status": 200,
                   "ip": "10.0.0.5", "detail": "间隔 10s -> 30s"}


def test_record_default_detail_and_channel_test_action():
    st = FakeStorage()
    row = record(st, method="POST", path="/api/alerts/channels/ch1/test", status=500,
                 who="ops", ip="127.0.0.1", ts=BASE)
    assert st.added[0]["action"] == "测试通知渠道"
    assert st.added[0]["target"] == "通知渠道"
    assert st.added[0]["target_id"] == "ch1"
    assert st.added[0]["detail"] == ""
    assert row["id"] == 1 and row["status"] == 500


def test_record_returns_id_from_storage():
    st = FakeStorage([_row(7, BASE)])
    row = record(st, method="DELETE", path="/api/nodes/n9", status=204, who="bob",
                 ip="", ts=BASE + 1)
    assert row["id"] == 8          # 假 storage 自增
    assert st.added[0]["target_id"] == "n9"
    assert st.added[0]["action"] == "删除节点"


# ---------------------------------------------------------------- query

def test_query_forwards_filters_and_adds_time_ok():
    st = FakeStorage([
        _row(3, BASE, status=200, action="新建任务", target="任务", who="alice", tid="t1"),
        _row(2, BASE - 10, status=404, action="删除节点", target="节点", who="bob", tid="n9"),
        _row(1, BASE - 20, status=0, action="新建任务", target="任务", who="carol"),
    ])
    out = query(st, limit=50, action="新建任务", target="任务", since=BASE - 5)
    assert st.list_calls == [{"limit": 50, "action": "新建任务", "target": "任务",
                              "since": BASE - 5}]
    assert [r["id"] for r in out] == [3]          # since / action / target 都生效
    row = out[0]
    assert row["time"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BASE))
    assert row["ok"] is True
    assert set(row) == {"id", "ts", "time", "who", "action", "target", "target_id",
                        "status", "ip", "detail", "ok"}

    # 不加 since 时能看到 status=0 的那条：未记录 → ok False
    rows = query(st, limit=10, action="新建任务", target="任务")
    assert [r["id"] for r in rows] == [3, 1]
    assert rows[1]["status"] == 0 and rows[1]["ok"] is False
    assert rows[1]["time"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BASE - 20))


def test_query_ok_flag_follows_http_status():
    st = FakeStorage([
        _row(4, BASE, status=200, action="新建任务", target="任务"),
        _row(3, BASE, status=302, action="新建任务", target="任务"),
        _row(2, BASE, status=400, action="新建任务", target="任务"),
        _row(1, BASE, status=500, action="新建任务", target="任务"),
        _row(0, BASE, status=0, action="新建任务", target="任务"),
    ])
    out = query(st)
    assert [r["ok"] for r in out] == [True, True, False, False, False]
    assert [r["status"] for r in out] == [200, 302, 400, 500, 0]


def test_query_defaults_are_forwarded_and_empty_result_stays_empty():
    st = FakeStorage()
    assert query(st) == []
    assert st.list_calls == [{"limit": 100, "action": "", "target": "", "since": 0}]


def test_query_tolerates_missing_optional_fields():
    st = FakeStorage([{"id": 1, "ts": BASE, "status": 200, "action": "新建任务",
                       "target": "任务"}])
    row = query(st)[0]
    assert row["target_id"] == "" and row["ip"] == "" and row["detail"] == ""
    assert row["ok"] is True


# ---------------------------------------------------------------- to_csv

def test_to_csv_header_and_column_order():
    st = FakeStorage([_row(1, BASE, status=200, action="新建任务", target="任务",
                           who="alice", tid="t1", ip="10.0.0.1", detail="普通")])
    text = to_csv(query(st))
    assert not text.startswith("\ufeff")      # BOM 由调用方加，这里不加
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == list(CSV_HEADER)
    assert parsed[0] == ["时间", "操作者", "动作", "目标类型", "目标ID", "状态",
                         "来源IP", "结果", "详情"]
    assert parsed[1] == [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BASE)),
                         "alice", "新建任务", "任务", "t1", "200", "10.0.0.1",
                         "成功", "普通"]


def test_to_csv_escapes_comma_quote_and_newline():
    tricky = '多行,带"引号"\n和第二行'
    rows = [{"id": 1, "ts": BASE, "time": "2026-01-01 08:00:00", "who": 'a,b',
             "action": "新建任务", "target": "任务", "target_id": "t1",
             "status": 200, "ip": "10.0.0.1", "detail": tricky, "ok": True}]
    parsed = list(csv.reader(io.StringIO(to_csv(rows))))
    assert len(parsed) == 2                     # 内嵌换行被引号包住，没有多出一行
    assert parsed[1][1] == "a,b"
    assert parsed[1][8] == tricky
    assert len(parsed[1]) == len(CSV_HEADER)


@pytest.mark.parametrize("status,ok,expected", [
    (200, True, "成功"),
    (302, True, "成功"),
    (400, False, "失败"),
    (500, False, "失败"),
    (0, False, "未记录"),
])
def test_to_csv_result_column(status, ok, expected):
    rows = [{"ts": BASE, "who": "x", "action": "新建任务", "target": "任务",
             "target_id": "", "status": status, "ip": "", "detail": "", "ok": ok}]
    parsed = list(csv.reader(io.StringIO(to_csv(rows))))
    assert parsed[1][7] == expected


def test_to_csv_accepts_raw_storage_rows():
    rows = [_row(1, BASE, status=200, action="删除节点", target="节点", who="alice",
                 tid="n9", ip="", detail="")]
    parsed = list(csv.reader(io.StringIO(to_csv(rows))))
    assert parsed[1][0] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BASE))
    assert parsed[1][7] == "成功"                # 没有 ok 字段时按 status 推断


def test_to_csv_empty_or_none_rows_keeps_header_only():
    assert list(csv.reader(io.StringIO(to_csv([])))) == [list(CSV_HEADER)]
    assert list(csv.reader(io.StringIO(to_csv(None)))) == [list(CSV_HEADER)]
