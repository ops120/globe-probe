"""历史对比（环比/同比）回归：让「两条线真的可比」，而不是看起来有两条线。

每一条都对着一类现场问题（.docs/ONCALL_OPTIMIZATION_2.md §1.4 / 第五期）：
- 窗口锚在「现在」→ 对比线各占半轴，实测「环比昨日」只在 4/24 格上可比、最差 0 格；
- 聚合只写已完结小时 → 「最近24小时」末格恒为 null，对比线同轴位却是完整小时（空窗比整窗）；
- 只有曲线没有结论 → 24 格里只有 4 格可比时用户完全看不出来；
- 同环比标反 → 「上周同日」「30 天前」都是**环比**，且系统里根本没有同比。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "cmp.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)          # 必须与 app 共用同一个实例（聚合重算才有意义）
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name):
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "version": "0.1.0", "system": {}})
    assert r.status_code == 200, r.text
    import hashlib
    return r.json()["node_id"], hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def seed_hour(client, nid, token, tid, hour_ts, ok_n, fail_n, rtt=10.0):
    """在某个小时桶里塞 ok_n 条成功 + fail_n 条失败（每分钟一条）。"""
    res = []
    for i in range(ok_n):
        res.append({"ts": hour_ts + 10 + i * 60, "task_id": tid, "type": "ping",
                    "status": "ok", "metrics": {"rtt_avg": rtt, "loss_rate": 0.0}})
    for i in range(fail_n):
        res.append({"ts": hour_ts + 30 + i * 60, "task_id": tid, "type": "ping",
                    "status": "fail", "error_class": "timeout",
                    "metrics": {"rtt_avg": None, "loss_rate": 1.0}})
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})


def last_closed_hour(now):
    """最后一个**已完结**小时的桶起点（聚合只写到这里）。"""
    return (now // 3600) * 3600 - 3600


def recompute_all(s, t_from, t_to):
    """1m → 5m → 1h：**顺序不能反**。1h 聚合是从 5m 表派生的（storage.agg_recompute 的
    else 分支读上一级聚合表），只算 1h 会得到空结果 —— 这是本用例第一次跑时踩到的坑。"""
    for bkt in ("1m", "5m", "1h"):
        s.agg_recompute(bkt, t_from, t_to)


def make_task(client, name="cmp-ping"):
    return client.post("/api/tasks", json={
        "name": name, "type": "ping", "target": "1.1.1.1",
        "interval_seconds": 60}).json()["id"]


# ---------------------------------------------------------------- 窗口口径

def test_axis_ends_at_last_closed_hour(tmp_path):
    """轴末格必须是「上一个整点」，不能把尚未聚合完的当前小时放进轴里。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    now = int(time.time())
    seed_hour(client, nid, token, tid, last_closed_hour(now), 3, 0)
    recompute_all(s, last_closed_hour(now) - 3600, last_closed_hour(now) + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=yesterday&metric=avail").json()
    assert r["hours"][-1] == last_closed_hour(now), (r["hours"][-1], now)
    assert all(h < now // 3600 * 3600 for h in r["hours"]), "轴里出现了尚未完结的当前小时"
    assert len(r["hours"]) == 24


def test_last_axis_point_is_not_always_null(tmp_path):
    """原实现末格恒为 null（当前小时桶永不写入）→ 与对比期的完整小时不可比。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    seed_hour(client, nid, token, tid, end, 5, 0)
    recompute_all(s, end - 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=yesterday&metric=avail").json()
    assert r["today"][-1] is not None, "末格仍为空：窗口没有对齐到已完结小时"
    assert r["today"][-1] == 100.0


def test_counts_reported_per_axis_point(tmp_path):
    """每格样本数要返回：让「这格只有 2 个样本」在界面上可见。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    seed_hour(client, nid, token, tid, end, 4, 0)
    recompute_all(s, end - 3600, end + 3600)
    r = client.get(f"/api/compare?task_id={tid}&metric=avail").json()
    assert len(r["today_counts"]) == len(r["hours"]) == len(r["other_counts"])
    assert r["today_counts"][-1] == 4
    assert r["today_counts"][0] == 0            # 没有数据的格样本数为 0


# ---------------------------------------------------------------- 环比 / 同比

def test_kind_separates_huanbi_and_tongbi(tmp_path):
    """环比=相邻周期；同比=去年同期。原先把「上周同日/30天前」标成同比，是口径错误。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    for mode, kind in (("prev", "环比"), ("yesterday", "环比"), ("lastweek", "环比"),
                       ("lastmonth", "环比"), ("lastyear", "同比")):
        r = client.get(f"/api/compare?task_id={tid}&mode={mode}&metric=avail").json()
        assert r["kind"] == kind, (mode, r["kind"])
    r = client.get(f"/api/compare?task_id={tid}&mode=lastyear&metric=avail").json()
    assert r["label"] == "去年同期"


def test_lastyear_reads_data_from_one_year_ago(tmp_path):
    """同比要有真实数据来源：1h 聚合保留 730 天，去年同期是够得着的。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    year = 365 * 86400
    seed_hour(client, nid, token, tid, end - year, 10, 0)      # 去年同期：全成功
    seed_hour(client, nid, token, tid, end, 5, 5)              # 当前：一半失败
    recompute_all(s, end - year - 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=lastyear&metric=avail").json()
    assert r["has_other"] is True, "同比读不到去年同期数据"
    assert r["other"][-1] == 100.0
    assert r["today"][-1] == 50.0
    assert r["summary"]["delta"]["avail_pp"] == -50.0


# ---------------------------------------------------------------- 时段汇总

def test_summary_weighted_by_samples(tmp_path):
    """时段汇总按 Σok/Σcount 加权（不是各格平均），并在单元测试里独立复算。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    seed_hour(client, nid, token, tid, end - 3600, 10, 0)      # 100%
    seed_hour(client, nid, token, tid, end, 1, 9)              # 10%
    recompute_all(s, end - 2 * 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&metric=avail&window_hours=2").json()
    t = r["summary"]["today"]
    assert t["count"] == 20 and t["ok"] == 11
    assert abs(t["avail"] - 55.0) < 0.05, t       # (10+1)/20 = 55%，不是 (100+10)/2
    # 对比期无数据 → Δ 必须为 None 而不是 0（0 会被误读成「没有变化」）
    assert r["summary"]["delta"]["avail_pp"] is None
    assert r["summary"]["other"]["avail"] is None


def test_summary_delta_matches_independent_computation(tmp_path):
    """Δ 必须等于两期独立复算之差（页面直接展示这个数，不能是另一套算法）。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    seed_hour(client, nid, token, tid, end, 8, 2)              # 80%
    seed_hour(client, nid, token, tid, end - 86400, 6, 4)      # 昨日同小时 60%
    recompute_all(s, end - 86400 - 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=yesterday&metric=avail&window_hours=1").json()
    assert r["summary"]["today"]["avail"] == 80.0
    assert r["summary"]["other"]["avail"] == 60.0
    assert r["summary"]["delta"]["avail_pp"] == 20.0


# ---------------------------------------------------------------- 覆盖度

def test_coverage_counts_overlap(tmp_path):
    """覆盖度要如实报告：两边各多少格有数据、多少格真正可比。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    seed_hour(client, nid, token, tid, end, 5, 0)
    seed_hour(client, nid, token, tid, end - 86400, 5, 0)
    recompute_all(s, end - 86400 - 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=yesterday&metric=avail&window_hours=2").json()
    cov = r["coverage"]
    assert cov["total"] == 2
    assert cov["today"] == 1 and cov["other"] == 1
    assert cov["overlap"] == 1


def test_non_overlapping_periods_report_zero_overlap(tmp_path):
    """两时段各有数据但**不在同一轴位**时，overlap=0 必须如实上报。

    这正是线上「环比昨日」最差的情形（两条线各占半轴、一格都不重叠）：前端据此
    不画两条断线，而是给结论与原因。
    """
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    seed_hour(client, nid, token, tid, end, 5, 0)                   # 只在最近的格
    seed_hour(client, nid, token, tid, end - 86400 - 5 * 3600, 5, 0)  # 昨日很靠前的格
    recompute_all(s, end - 86400 - 6 * 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=yesterday&metric=avail&window_hours=6").json()
    assert r["has_other"] is True and r["has_today"] is True
    assert r["coverage"]["overlap"] == 0, r["coverage"]


# ---------------------------------------------------------------- prev 自适应

def test_prev_prefers_window_with_real_overlap(tmp_path):
    """prev 必须挑出**真能逐格对比**的窗口，而不是「两边各自有数据」就选中。

    这个用例专门盯住一个真实出现过的 bug：算重叠时 o_b 的键是**偏移后**的时间戳，
    却用未偏移的 h 去查 → 每个窗口都算出 0 重叠 → 永远退到上限窗口（线上表现为
    「prev 选了 24h 窗口、只有 4/24 格可比」）。

    **关键是让 pick_max 足够大**：pick_max 太小（历史很短）时即使逻辑写错也能蒙对，
    这正是第一版用例没能抓住它的原因。这里种一条 ~30 小时前的样本把历史拉长，
    使 pick_max≈15 > 6（真正达半的窗口大小）。
    """
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client)
    end = last_closed_hour(int(time.time()))
    # 一条很旧的样本：把 history_hours 拉大 → pick_max = min(24, history/2) 变大
    seed_hour(client, nid, token, tid, end - 30 * 3600, 3, 0)
    # 连续 12 个已完结小时有数据（这是唯一能给出一半以上重叠的数据形态）
    for k in range(12):
        seed_hour(client, nid, token, tid, end - k * 3600, 6, 0)
    recompute_all(s, end - 31 * 3600, end + 3600)

    r = client.get(f"/api/compare?task_id={tid}&mode=prev&metric=avail").json()
    assert r["history_hours"] > 24, r["history_hours"]      # 前置：pick_max 已被拉大
    assert r["coverage"]["overlap"] > 0, "prev 选出了 0 重叠的窗口"
    assert r["coverage"]["overlap"] * 2 >= r["coverage"]["total"], (
        "自适应窗口选出了重叠不足一半的组合", r["coverage"])
    assert r["window_hours"] < 24, (
        "pick_max 足够大时仍退到上限窗口，说明候选窗口的重叠根本没算对", r["window_hours"])


def test_prev_handles_no_data_at_all(tmp_path):
    """完全没有数据时不能抛异常，要如实返回空窗口。"""
    client, cfg, s = make_client(tmp_path)
    register(client, cfg, "n1")
    tid = make_task(client)
    r = client.get(f"/api/compare?task_id={tid}&mode=prev&metric=avail").json()
    assert r["has_today"] is False and r["has_other"] is False
    assert r["coverage"]["overlap"] == 0
    assert len(r["hours"]) == len(r["today"]) == len(r["other"])
