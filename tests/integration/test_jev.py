"""JEV 故障判断（第七期 31-38）回归。

参考 JEV 的四条硬约束，每条都固化成用例：
- 证据候选由代码切分（模型只能选，池外 id 一律判无效）
- 固定假设集 + 逐假设独立判断（只回 support/confidence，不回长文）
- **代码拥有控制流**：阈值/结论/分歧判定全在代码，调阈值只改代码
- 一致性三态：一致 / 存在分歧 / 依据薄弱（依据薄弱时绝不输出根因）
- 前置门禁：证据不可信时拒绝判断，不调用判据
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server import jev  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "jev.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


def make_client_with_token(tmp_path, admin_token="k1f9c0ffee24beef7acc0de"):
    """带 admin_token 的 client（P2-1：JEV run 曾漏配 check_write 的回归用）。"""
    from gpm.server.storage import Storage
    db = str(tmp_path / "jev-auth.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0",
                             "admin_token": admin_token}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage, admin_token


def register(client, cfg, name):
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "version": "0.1.0", "system": {}})
    assert r.status_code == 200, r.text
    return r.json()["node_id"], hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def make_task(client, name):
    r = client.post("/api/tasks", json={
        "name": name, "type": "ping", "target": "1.1.1.1", "interval_seconds": 10})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def open_incident(client, nid, token, tid, ts0=None):
    ts0 = ts0 or int(time.time()) - 60
    res = [{"ts": ts0 + i * 10, "task_id": tid, "type": "ping", "dns": "", "url": "",
            "status": "fail", "error_class": "timeout", "metrics": {}} for i in range(3)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})


# ---------------------------------------------------------------- 证据池（31）

def test_evidence_pool_is_built_by_code():
    detail = {"incident": {"id": 1, "task_id": "t", "node_id": "n",
                           "reason": {"error_class": "dns_timeout", "error": "解析超时"}},
              "changes": [{"action": "停用任务", "detail": "t: enabled 1 -> 0"}],
              "dns_changes": [{"answers": ["1.2.3.4"]}],
              "dying": [{"cpu": 93.0, "mem": 80.0}]}
    ev = jev.build_evidence(detail)
    assert ev, "证据池不能为空"
    ids = [e["id"] for e in ev]
    assert ids == ["E%d" % (i + 1) for i in range(len(ids))], "id 必须稳定且连续"
    kinds = {e["kind"] for e in ev}
    assert "error_class" in kinds and "change" in kinds and "dns_change" in kinds
    assert any("DNS" in e["text"] for e in ev if e["kind"] == "error_class")


def test_evidence_pool_covers_node_events():
    detail = {"incident": {"id": 2, "task_id": "", "node_id": "n",
                           "reason": {"event": "offline"}}}
    ev = jev.build_evidence(detail)
    assert any(e["kind"] == "node_heartbeat" for e in ev)


# ---------------------------------------------------------------- 类型化输出（32）+ 池外引用（31）

def test_judgment_rejects_unknown_hypothesis_and_non_numeric():
    good, rejected = jev.normalize_judgments(
        [{"hypothesis": "DNS", "support": 0.8, "confidence": 0.7},
         {"hypothesis": "瞎猜", "support": 0.9, "confidence": 0.9},
         {"hypothesis": "网络", "support": "很高", "confidence": 0.9},
         {"hypothesis": "端口", "support": 0.7}],
        jev.HYPOTHESES, ["E1"])
    assert [g["hypothesis"] for g in good] == ["DNS"]
    assert len(rejected) == 3, rejected


def test_judgment_rejects_out_of_pool_citation():
    """引用池外证据 = 幻觉，整条作废。这是反幻觉的硬闸。"""
    good, rejected = jev.normalize_judgments(
        [{"hypothesis": "网络", "support": 0.9, "confidence": 0.9,
          "cited": ["E1", "E99"]}],
        jev.HYPOTHESES, ["E1"])
    assert good == []
    assert "池外证据" in rejected[0]


def test_judgment_accepts_valid_citation():
    good, _ = jev.normalize_judgments(
        [{"hypothesis": "网络", "support": 0.9, "confidence": 0.9, "cited": ["E1"]}],
        jev.HYPOTHESES, ["E1"])
    assert len(good) == 1


# ---------------------------------------------------------------- 代码拥有控制流（34）+ 三态（35）

def test_decision_agree():
    d = jev.decide([{"hypothesis": "网络", "support": 0.8, "confidence": 0.8},
                    {"hypothesis": "DNS", "support": 0.1, "confidence": 0.4}], {})
    assert d["state"] == "agree" and d["root_cause"] == "网络"


def test_decision_diverge_when_margin_small():
    d = jev.decide([{"hypothesis": "网络", "support": 0.8, "confidence": 0.8},
                    {"hypothesis": "端口", "support": 0.75, "confidence": 0.8}], {})
    assert d["state"] == "diverge" and d["root_cause"] == "网络"
    assert "分歧" in d["note"]


def test_decision_weak_when_support_low():
    """依据薄弱：绝不输出根因。"""
    d = jev.decide([{"hypothesis": "网络", "support": 0.2, "confidence": 0.9}], {})
    assert d["state"] == "weak" and d["root_cause"] is None
    assert "不编根因" in d["note"]


def test_decision_weak_when_confidence_low():
    d = jev.decide([{"hypothesis": "网络", "support": 0.9, "confidence": 0.1}], {})
    assert d["state"] == "weak" and d["root_cause"] is None


def test_decision_weak_when_no_judgments():
    d = jev.decide([], {})
    assert d["state"] == "weak" and d["root_cause"] is None


def test_consistency_three_states():
    assert jev.consistency("网络", {"state": "agree", "root_cause": "网络"}) == "一致"
    assert jev.consistency("网络", {"state": "agree", "root_cause": "DNS"}) == "存在分歧"
    assert jev.consistency("", {"state": "agree", "root_cause": "DNS"}) == "一致"
    assert jev.consistency("网络", {"state": "weak", "root_cause": None}) == "依据薄弱"


def test_thresholds_are_in_code():
    """阈值在代码里：同输入同阈值结论稳定，调阈值只改代码。"""
    js = [{"hypothesis": "网络", "support": 0.6, "confidence": 0.8}]
    assert jev.decide(js, {})["state"] == "agree"
    old = jev.WEAK_SUPPORT
    try:
        jev.WEAK_SUPPORT = 0.9          # 只改常量，不改判据
        assert jev.decide(js, {})["state"] == "weak"
    finally:
        jev.WEAK_SUPPORT = old


# ---------------------------------------------------------------- 端到端（含前置门禁 38）

def test_run_produces_replayable_trace(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    open_incident(client, nid, token, tid)
    iid = s.list_incidents(1, open_only=True)[0]["id"]

    r = client.post("/api/jev/%d/run" % iid)
    assert r.status_code == 200, r.text
    tr = r.json()
    assert tr["incident_id"] == iid
    assert tr["rule"]["model_can_override_rule"] is False
    assert tr["verdict"]["state"] in ("一致", "存在分歧", "依据薄弱")
    assert tr["verdict"]["rule_conclusion"]["layer"], "规则结论必须给出"
    assert tr["evidence"] and tr["judge"] == "local"
    again = client.get("/api/jev/%d" % iid).json()
    assert again["incident_id"] == iid and again["rule"] == tr["rule"]


def test_precheck_gate_blocks_when_zombies_present(tmp_path):
    """证据不可信时拒绝判断，不调用判据（第七期 38 硬前置）。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    open_incident(client, nid, token, tid)
    iid = s.list_incidents(1, open_only=True)[0]["id"]
    s.db.execute("UPDATE tasks SET enabled=0 WHERE id=?", (tid,))
    s.db.commit()
    r = client.post("/api/jev/%d/run" % iid)
    assert r.status_code == 409, r.status_code
    assert "证据不可信" in r.json()["detail"]
    assert s.db.execute("SELECT COUNT(*) FROM jev_traces").fetchone()[0] == 0


def test_run_requires_admin_token(tmp_path):
    """P2-1 回归：POST /api/jev/{id}/run 是写口，必须与其余写口一样过 check_write。

    该端点曾声明了 x_admin_token Header 却从不校验 —— 配了 admin_token 时
    任何人都能触发 JEV 判断。矩阵：无 token 403 / 错 token 403 / 对 token 放行。
    """
    client, cfg, s, tok = make_client_with_token(tmp_path)
    nid, token = register(client, cfg, "n1")   # 节点注册/上报不带 admin token，不受影响
    # 建任务也要 token（本用例顺带证明这套 client 的 token 真的在拦写口）
    r = client.post("/api/tasks", json={
        "name": "t", "type": "ping", "target": "1.1.1.1", "interval_seconds": 10})
    assert r.status_code == 403, r.status_code
    tid = client.post("/api/tasks", json={
        "name": "t", "type": "ping", "target": "1.1.1.1", "interval_seconds": 10},
        headers={"X-Admin-Token": tok}).json()["id"]
    open_incident(client, nid, token, tid)
    iid = s.list_incidents(1, open_only=True)[0]["id"]

    # 无 token / 错 token：403，且不产生轨迹、不算一次判断
    assert client.post("/api/jev/%d/run" % iid).status_code == 403
    r = client.post("/api/jev/%d/run" % iid, headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403, r.status_code
    assert "X-Admin-Token" in r.json()["detail"]
    assert s.db.execute("SELECT COUNT(*) FROM jev_traces").fetchone()[0] == 0

    # 正确 token：放行（200 走完判断或 409 撞前置门禁都算放行，这里无僵尸应为 200）
    r = client.post("/api/jev/%d/run" % iid, headers={"X-Admin-Token": tok})
    assert r.status_code == 200, r.text
    assert r.json()["incident_id"] == iid
    # 读口不受影响（GET /api/jev/{iid} 本来就不需要 token）
    assert client.get("/api/jev/%d" % iid).status_code == 200


def test_http_judge_is_pluggable_and_checked():
    """HttpJudge 只负责「取判断」，阈值/结论仍在代码；输出同样过池外校验。"""
    class FakeHttp:
        name = "http"
        def judge(self, evidence, hypotheses, rule_hint):
            return [{"hypothesis": "服务端", "support": 0.85, "confidence": 0.9,
                     "cited": [evidence[0]["id"]]}]
    detail = {"incident": {"id": 9, "task_id": "t", "node_id": "n",
                           "reason": {"error_class": "http_5xx"}}}
    tr = jev.run(detail, judge=FakeHttp())
    assert tr["judge"] == "http"
    assert tr["verdict"]["jev_conclusion"]["root_cause"] == "服务端"

    class FakeBad:
        name = "http"
        def judge(self, evidence, hypotheses, rule_hint):
            return [{"hypothesis": "服务端", "support": 0.9, "confidence": 0.9, "cited": ["E999"]}]
    tr2 = jev.run(detail, judge=FakeBad())
    assert tr2["judgments"] == [] and tr2["rejected"]
    assert tr2["verdict"]["jev_conclusion"]["root_cause"] is None


def test_model_output_cannot_be_long_form():
    """只接受 support/confidence：多出来的字段被忽略，长文解释进不了结论。"""
    good, _ = jev.normalize_judgments(
        [{"hypothesis": "DNS", "support": 0.7, "confidence": 0.8,
          "explanation": "我认为这是因为……（一大段解释）"}],
        jev.HYPOTHESES, ["E1"])
    assert len(good) == 1 and "explanation" not in good[0]
