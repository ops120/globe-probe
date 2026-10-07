"""端到端真实数据实测（本机）：

启动真实 gpm server + agent 进程（独立端口/独立数据库）→ 创建真实任务
（DNS 多线路 / curl 多URL / 故障注入 / mtr）→ 等待数据积累 → 通过 API 验收 → 输出报告。
用法：python scripts/e2e_real.py [--duration 120]
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8621
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e-data"


def api(path, method="GET", body=None, expect_error=False):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if expect_error:
            return {"status": e.code}
        raise


def write_configs():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "agent").mkdir(exist_ok=True)
    db = (DATA / "e2e.db").as_posix()
    adir = (DATA / "agent").as_posix()
    (DATA / "server.yaml").write_text(
        'server:\n'
        f'  listen: "127.0.0.1:{PORT}"\n'
        f'  database: "{db}"\n'
        'agent:\n'
        '  register_token: "e2e-token"\n'
        'logging:\n  level: "INFO"\n', encoding="utf-8")
    (DATA / "agent.yaml").write_text(
        'agent:\n'
        f'  server_url: "{BASE}"\n'
        '  register_token: "e2e-token"\n'
        '  name: "e2e-node"\n'
        f'  data_dir: "{adir}"\n'
        '  heartbeat_interval: 10\n'
        '  report_interval: 3\n', encoding="utf-8")


def main(duration: int = 120):
    results = []
    write_configs()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    log_s = open(DATA / "server.log", "w", encoding="utf-8")
    log_a = open(DATA / "agent.log", "w", encoding="utf-8")
    procs = []
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "gpm", "--config", str(DATA / "server.yaml"), "server"],
            cwd=ROOT, env=env, stdout=log_s, stderr=subprocess.STDOUT))
        for _ in range(30):
            time.sleep(1)
            try:
                api("/api/health")
                break
            except Exception:
                continue
        print(f"[1] 服务端启动 OK (端口 {PORT})")
        results.append(("服务端启动", True, ""))

        procs.append(subprocess.Popen(
            [sys.executable, "-m", "gpm", "--config", str(DATA / "agent.yaml"), "agent"],
            cwd=ROOT, env=env, stdout=log_a, stderr=subprocess.STDOUT))
        time.sleep(8)
        nodes = api("/api/nodes")
        assert any(n["status"] == "online" for n in nodes), "节点未上线"
        node_id = [n for n in nodes if n["status"] == "online"][0]["id"]
        print(f"[2] 节点注册并在线: {node_id}")
        results.append(("节点注册/心跳", True, node_id))

        # 真实任务
        t_ping = api("/api/tasks", "POST", {
            "name": "e2e-ping-ali", "type": "ping", "target": "223.5.5.5",
            "interval_seconds": 10, "params": {"count": 4, "timeout": 2}})
        t_lines = api("/api/tasks", "POST", {
            "name": "e2e-ping-lines", "type": "ping", "target": "www.baidu.com",
            "dns": ["223.5.5.5", "8.8.8.8"], "interval_seconds": 10,
            "params": {"count": 4, "timeout": 2}})
        t_curl = api("/api/tasks", "POST", {
            "name": "e2e-curl-multi", "type": "curl", "target": "www.baidu.com",
            "urls": ["https://www.baidu.com", "https://www.baidu.com/duty/"],
            "dns": ["223.5.5.5"], "interval_seconds": 10, "params": {"timeout": 10}})
        t_fail = api("/api/tasks", "POST", {
            "name": "e2e-fail-inject", "type": "curl", "target": "127.0.0.1",
            "urls": ["http://127.0.0.1:9/health"], "interval_seconds": 10,
            "params": {"timeout": 3}})
        t_mtr = api("/api/tasks", "POST", {
            "name": "e2e-mtr", "type": "mtr", "target": "8.8.8.8",
            "interval_seconds": 60, "params": {"cycles": 10}})
        print(f"[3] 任务创建: {t_ping['id'][:9]}… {t_lines['id'][:9]}… {t_curl['id'][:9]}… "
              f"{t_fail['id'][:9]}… {t_mtr['id'][:9]}…")

        print(f"[4] 真实探测运行 {duration}s …")
        time.sleep(duration)

        # 验收
        def streams(tid):
            return api(f"/api/query/streams?task_id={tid}")

        checks = []
        rows = [len(streams(t["id"])) for t in (t_ping, t_lines, t_curl, t_fail, t_mtr)]
        checks.append(("ping 目标流=1", rows[0] == 1, f"实际 {rows[0]}"))
        checks.append(("ping 多线路流=2", rows[1] == 2, f"实际 {rows[1]}"))
        checks.append(("curl 多URL流=2", rows[2] == 2, f"实际 {rows[2]}"))
        checks.append(("故障注入流=1", rows[3] == 1, f"实际 {rows[3]}"))
        checks.append(("mtr 流=1", rows[4] == 1, f"实际 {rows[4]}"))

        # 数据正确性：ping-ali 最近结果
        detail = api(f"/api/query/series?task_id={t_ping['id']}&node_id={node_id}"
                     f"&metric=rtt&granularity=raw")
        ok_pts = [p for p in detail["points"] if p["status"] == "ok"]
        checks.append(("ping 有成功样本", len(ok_pts) >= 3, f"{len(ok_pts)} 个"))
        if ok_pts:
            v = ok_pts[-1]["v"]
            checks.append(("RTT 数值合理(1~100ms)", 1 <= v <= 100, f"{v}ms"))

        # 多线路独立解析
        d2 = api(f"/api/query/series?task_id={t_lines['id']}&node_id={node_id}"
                 f"&dns=8.8.8.8&metric=rtt&granularity=raw")
        ok8 = [p for p in d2["points"] if p["status"] == "ok"]
        checks.append(("Google 线路有样本", len(ok8) >= 2, f"{len(ok8)} 个"))

        # curl 多 URL 独立
        st_curl = streams(t_curl["id"])
        urls = sorted({s["url"] for s in st_curl})
        checks.append(("curl URL 独立流", len(urls) == 2, str(urls)))

        # 故障注入 → 事件
        time.sleep(20)  # 事件阈值 3 次 + 上报
        incs = api("/api/query/incidents?open_only=true")
        fail_inc = [i for i in incs if i["task_id"] == t_fail["id"]]
        checks.append(("故障注入→事件开启", bool(fail_inc),
                       f"事件 {len(fail_inc)} 个, error={(fail_inc[0]['reason'].get('error_class') if fail_inc else '-')}"))

        # mtr skipped（Windows 无 mtr）
        mtr = api(f"/api/query/mtr?task_id={t_mtr['id']}&limit=1")
        checks.append(("mtr 如实 skipped(tool_missing)",
                       bool(mtr) and mtr[0]["status"] == "skipped",
                       mtr[0]["error_class"] if mtr else "无"))

        # 聚合与导出
        to = int(time.time())
        up = api(f"/api/query/uptime?task_id={t_curl['id']}&bucket=60&t_from={to - 600}&t_to={to}")
        checks.append(("通断条带有行", len(up["rows"]) >= 1, f"{len(up['rows'])} 行"))
        exp = api(f"/api/export?task_id={t_curl['id']}&fmt=json&t_from={to - 600}&t_to={to}")
        checks.append(("JSON 导出", len(exp) >= 2, f"{len(exp)} 条"))

        print("\n===== E2E 验收结果 =====")
        passed = 0
        for name, ok, detail in checks:
            print(f" [{'✓' if ok else '✗'}] {name:28s} {detail}")
            passed += bool(ok)
        print(f"===== {passed}/{len(checks)} 通过 =====")
        return 0 if passed == len(checks) else 1
    finally:
        for p in procs:
            p.terminate()
        for f in (log_s, log_a):
            f.close()


if __name__ == "__main__":
    dur = 120
    if "--duration" in sys.argv:
        dur = int(sys.argv[sys.argv.index("--duration") + 1])
    sys.exit(main(dur))
