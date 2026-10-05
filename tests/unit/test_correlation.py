"""correlation.analyze 单元测试：同时段故障聚合与相关关系（第八期）。

核心纪律的回归钉：
- 只对齐可证明维度；burst（时间聚集）单独出现不构成连边（min_edge=3 > 时间权重 1）
- 假设措辞「疑似」，带证据计数；旁证关联（外部告警挂本地事件）是最强信号
- CMDB 缺口如实盘点（缺失的核心资产键），不编造定位结论
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpm.server import correlation  # noqa: E402

T0 = 1_800_000_000


class FakeCorrStorage:
    """correlation.analyze 需要的最小只读面。"""

    def __init__(self, incidents=None, tasks=None, nodes=None, external=None, links=None):
        self._incidents = incidents or []
        self._tasks = tasks or []
        self._nodes = nodes or []
        self._external = external or []
        self._links = links or {}      # {incident_id: [alert_row]}

    def list_incidents(self, limit=30, open_only=False, t_from=0, t_to=0):
        return [i for i in self._incidents
                if (not t_from or (i.get("ended_at") or T0 + 99999) >= t_from)
                and (not t_to or i.get("started_at", 0) <= t_to)]

    def list_tasks(self):
        return self._tasks

    def list_nodes(self):
        return self._nodes

    def list_external_alerts(self, limit=100, source="", status="", t_from=0, firing_only=False):
        return [a for a in self._external
                if not t_from or int(a.get("started_at") or a.get("received_at") or 0) >= t_from]

    def external_alert_links_for(self, incident_ids):
        """按**事件 id** 过滤（与真实 Storage 的 WHERE incident_id IN 语义一致）：
        曾经 correlation 把告警 id 传进来，靠 fake 忽略入参把 P0 掩盖了。"""
        want = set(int(i) for i in incident_ids)
        return {iid: lst for iid, lst in self._links.items() if int(iid) in want}


def _inc(iid, task_id, node_id, start, end=None, error_class="timeout", dns=""):
    return {"id": iid, "task_id": task_id, "node_id": node_id, "dns": dns, "url": "",
            "started_at": start, "ended_at": end, "duration_ms": None,
            "kind": "probe", "reason": {"error_class": error_class}}


def _task(tid, name, target=""):
    return {"id": tid, "name": name, "type": "ping", "target": target, "urls": [], "enabled": True}


def _node(nid, name, tags=None):
    return {"id": nid, "name": name, "status": "online", "tags": tags or {}}


def test_same_node_faults_cluster_with_node_hypothesis():
    """同节点 3 条故障（不同任务、时间重叠）→ 一个簇，主导假设「疑似节点侧」。"""
    s = FakeCorrStorage(
        incidents=[_inc(1, "t1", "n1", T0), _inc(2, "t2", "n1", T0 + 60),
                   _inc(3, "t3", "n1", T0 + 120)],
        tasks=[_task("t1", "ping-a"), _task("t2", "ping-b"), _task("t3", "ping-c")],
        nodes=[_node("n1", "win-local")])
    r = correlation.analyze(s, T0 - 600, T0 + 600)
    assert r["stats"]["faults"] == 3 and r["stats"]["clusters"] == 1, r["stats"]
    c = r["clusters"][0]
    dims = {d["dim"] for d in c["dims"]}
    assert "node" in dims
    assert "疑似节点侧" in c["hypothesis"]
    # 无任何资产标签 → CMDB 缺口如实列出
    gap_keys = {g["key"] for g in c["gaps"]}
    assert {"owner", "biz", "region"} <= gap_keys


def test_node_tags_shrink_gaps():
    """节点带 owner/region 标签 → 对应缺口消失（缺口只报真缺的）。"""
    s = FakeCorrStorage(
        incidents=[_inc(1, "t1", "n1", T0), _inc(2, "t2", "n1", T0 + 60)],
        tasks=[_task("t1", "a"), _task("t2", "b")],
        nodes=[_node("n1", "win-local", {"owner": "ops", "region": "cn-north"})])
    r = correlation.analyze(s, T0 - 600, T0 + 600)
    gap_keys = {g["key"] for g in r["clusters"][0]["gaps"]}
    assert "owner" not in gap_keys and "region" not in gap_keys and "biz" in gap_keys


def test_distant_faults_stay_single():
    """不同节点/任务、时间也错开 → 各自零散，不硬凑关系。"""
    s = FakeCorrStorage(
        incidents=[_inc(1, "t1", "n1", T0), _inc(2, "t2", "n2", T0 + 3600 * 5)],
        tasks=[_task("t1", "a"), _task("t2", "b")],
        nodes=[_node("n1", "a"), _node("n2", "b")])
    r = correlation.analyze(s, T0 - 600, T0 + 3600 * 6)
    assert r["stats"]["clusters"] == 0 and r["stats"]["singles"] == 2, r["stats"]


def test_burst_only_never_clusters():
    """时间聚集但没有任何公共维度 → 不连边（burst 权重 1 < min_edge 3），如实进零散。"""
    s = FakeCorrStorage(
        incidents=[_inc(1, "t1", "n1", T0, error_class="timeout"),
                   _inc(2, "t2", "n2", T0 + 30, error_class="dns_fail")],
        tasks=[_task("t1", "a", target="a.example.com"),
               _task("t2", "b", target="b.example.org")],
        nodes=[_node("n1", "a"), _node("n2", "b")])
    r = correlation.analyze(s, T0 - 600, T0 + 600)
    assert r["stats"]["clusters"] == 0, r["clusters"]
    assert r["stats"]["singles"] == 2


def test_external_link_forms_cluster():
    """外部告警已挂到本地事件作旁证 → 连成簇，假设说明「互为旁证」。

    告警 id(100) 与事件 id(5) 刻意错开：曾经把告警 id 传给按事件 id 过滤的
    external_alert_links_for，旁证边在 id 空间错开后静默丢失（P0）。"""
    alert = {"id": 100, "source": "dingtalk", "source_id": "x1", "title": "核心服务故障",
             "status": "firing", "started_at": T0 + 10, "ended_at": 0, "labels": {},
             "url": "", "received_at": T0 + 10}
    inc = _inc(5, "t1", "n1", T0)
    inc["ended_at"] = None
    s = FakeCorrStorage(
        incidents=[inc],
        tasks=[_task("t1", "a")], nodes=[_node("n1", "a")],
        external=[alert],
        links={5: [dict(alert)]})
    r = correlation.analyze(s, T0 - 600, T0 + 600)
    assert r["stats"]["clusters"] == 1 and r["stats"]["singles"] == 0, r["stats"]
    c = r["clusters"][0]
    assert any(d["dim"] == "link" for d in c["dims"])
    assert "旁证" in c["hypothesis"]


def test_shared_host_cluster_from_external_label():
    """外部告警 labels.instance 与本地任务目标同域名 → host 维度聚类。"""
    ext = {"id": 3, "source": "generic", "source_id": "g1",
           "title": "api.example.com 5xx 突增", "status": "firing",
           "started_at": T0, "ended_at": 0,
           "labels": {"instance": "api.example.com"}, "url": "", "received_at": T0}
    s = FakeCorrStorage(
        incidents=[_inc(7, "t1", "n1", T0), _inc(8, "t1", "n2", T0 + 120)],
        tasks=[_task("t1", "curl-api", target="https://api.example.com/health")],
        nodes=[_node("n1", "a"), _node("n2", "b")],
        external=[ext])
    r = correlation.analyze(s, T0 - 600, T0 + 600)
    assert r["stats"]["clusters"] == 1, r["stats"]
    dims = {d["dim"] for d in r["clusters"][0]["dims"]}
    assert "host" in dims


def test_empty_window_explains_nothing_to_invent():
    """空窗口：结构完整、计数为 0，不给任何编造的结论。"""
    r = correlation.analyze(FakeCorrStorage(), T0 - 600, T0 + 600)
    assert r["stats"]["faults"] == 0 and r["clusters"] == [] and r["singles"] == []
    assert "note" in r and "疑似" in r["note"]
