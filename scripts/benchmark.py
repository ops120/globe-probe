"""基准：批量上报吞吐、去重开销、聚合重算耗时。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app
from gpm.server.storage import BUCKET_SECONDS, Storage


def main(n: int = 5000):
    import tempfile
    tmp = tempfile.mkdtemp()
    db = str(Path(tmp) / "bench.db")
    cfg = Config({"server": {"database": db}})
    storage = Storage(db)
    client = TestClient(create_app(cfg, storage))

    reg = client.post("/api/agent/register", json={
        "name": "bench", "register_token": cfg.agent["register_token"]}).json()
    nid = reg["node_id"]
    token = __import__("hashlib").sha256(f"bench:{cfg.agent['register_token']}".encode()).hexdigest()
    tasks = []
    for i in range(10):
        tasks.append(client.post("/api/tasks", json={
            "name": f"bench-{i}", "type": "ping", "target": "223.5.5.5",
            "interval_seconds": 10, "dns": ["223.5.5.5", "8.8.8.8"]}).json())
    client.post("/api/agent/sync", json={"node_id": nid, "token": token, "config_version": 0})

    now = int(time.time())
    # 构造 n 条结果：10 任务 × 2 线路 × 250 时间点
    results = []
    k = 0
    for t in tasks:
        for dns in ("223.5.5.5", "8.8.8.8"):
            for j in range(n // (10 * 2)):
                results.append({
                    "ts": now - 3600 + j, "task_id": t["id"], "type": "ping",
                    "dns": dns, "url": "", "status": "ok" if j % 20 else "fail",
                    "error_class": "", "dns_server": dns, "resolved_ip": "223.5.5.5",
                    "dns_time_ms": 5.0,
                    "metrics": {"sent": 4, "received": 4, "loss_rate": 0.0,
                                "rtt_min": 10.0, "rtt_avg": 12.0 + (k % 30), "rtt_max": 30.0},
                    "config_version": 1})
                k += 1

    print(f"=== 基准：{len(results)} 条结果（10 任务 × 2 线路 × {n // 20} 点） ===")
    lat = []
    sent = 0
    t0 = time.perf_counter()
    for i in range(0, len(results), 500):
        batch = results[i:i + 500]
        ta = time.perf_counter()
        r = client.post("/api/agent/results", json={
            "node_id": nid, "token": token, "results": batch})
        lat.append((time.perf_counter() - ta) * 1000)
        assert r.status_code == 200, r.text
        sent += r.json()["accepted"]
    total = time.perf_counter() - t0
    lat.sort()
    print(f"ingest: {sent} 条 / {total:.2f}s = {sent / total:.0f} 行/秒；"
          f"批延迟 P50={lat[len(lat)//2]:.1f}ms P95={lat[int(len(lat)*0.95)]:.1f}ms")

    # 去重开销：重复上报（分块 ≤1000）
    t0 = time.perf_counter()
    acc = dups = 0
    for i in range(0, 2500, 1000):
        r = client.post("/api/agent/results", json={
            "node_id": nid, "token": token, "results": results[i:i + 1000]}).json()
        acc += r["accepted"]
        dups += r["duplicates"]
    dup_time = time.perf_counter() - t0
    print(f"去重: 2500 条重复 -> accepted={acc} duplicates={dups} 耗时 {dup_time * 1000:.0f}ms")

    # 聚合重算
    for bucket in ("1m", "5m", "1h"):
        step = BUCKET_SECONDS[bucket]
        b_from = (now - 3600) // step * step
        t0 = time.perf_counter()
        storage.agg_recompute(bucket, b_from, now)
        dt = (time.perf_counter() - t0) * 1000
        cnt = storage.db.execute("SELECT COUNT(*) c FROM aggregates WHERE bucket=?", (bucket,)).fetchone()["c"]
        print(f"聚合 {bucket}: {dt:.0f}ms 重算 {cnt} 桶")

    # 查询
    t0 = time.perf_counter()
    r = client.get(f"/api/query/uptime?task_id={tasks[0]['id']}&bucket=60&t_from={now - 3600}&t_to={now}")
    print(f"uptime 查询: {(time.perf_counter() - t0) * 1000:.0f}ms")
    t0 = time.perf_counter()
    r = client.get(f"/api/export?task_id={tasks[0]['id']}&fmt=csv&t_from={now - 3600}&t_to={now}")
    body = r.text
    print(f"导出 CSV: {(time.perf_counter() - t0) * 1000:.0f}ms, {len(body) // 1024}KB")
    print(f"数据库大小: {Path(db).stat().st_size // 1024}KB")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
