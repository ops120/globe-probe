"""CI 专用：一键搭起「有数据的」一次性实例并跑浏览器验收（headless）。

单独一个空的 gpm 实例喂不饱 ui_acceptance.py 的数据断言（事件详情弹窗、
重投队列、历史对比双曲线、渠道表都要求有真实数据）。本脚本负责：

  1. 起一次性 server（独立端口 8622 / 独立库）+ 两个 agent（双节点形态）；
  2. 通过真实 API 建任务（含一个必失败任务 → 产出事件）与通知渠道；
  3. 等实时数据与事件出现后停服，用 SQLite 直接补种 ~6h 历史
     （对比页「已自动切到前一时段」要求 history<24h 且前窗有数据）；
  4. 重启后跑 scripts/ui_acceptance.py（GPM_UI_CHANNEL 置空 → Playwright 自带
     chromium，CI 无 Chrome 也能跑）；
  5. 无论成败都清理进程与临时目录。

用法：python scripts/ui_ci.py [--port 8622] [--keep]
退出码 = 浏览器验收退出码。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def api(base, path, method="GET", body=None, timeout=20):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:200]}") from e


def wait_for(fn, what, timeout, interval=2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001 - 未就绪时接口可能尚未监听
            pass
        time.sleep(interval)
    raise TimeoutError(f"等待超时：{what}（{timeout}s）")


def spawn(cmd, cwd, env, log):
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)


def stop(procs):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def seed_history(db_path: str, hours: float = 6.0):
    """停服状态下补种历史：1m 聚合（全任务全流）+ 一条已关闭事件 + 告警与重投记录。

    对比页断言要求 history_hours < 24 且「前一时段」两条曲线都有数据点，
    因此种 6h 而不是 ≥24h；today/other 两个窗口都落在种子区间内。
    """
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    now = int(time.time())
    step = 60
    t_from = now - int(hours * 3600)
    t_to = now - 120          # 与实时数据留 2 分钟空隙，PK 冲突交给 OR REPLACE
    tasks = con.execute("SELECT id, type, urls_json FROM tasks").fetchall()
    node_ids = [r[0] for r in con.execute("SELECT id FROM nodes").fetchall()]

    # 定位种子：节点 local_ip=127.0.0.1 走自定义网段、ping 目标 223.5.5.5 走缓存命中，
    # 让「全球地图链路」断言离线可复现（不依赖 ip-api 在线查询）。
    con.execute(
        "INSERT OR REPLACE INTO geo_networks(id,cidr,place,lat,lng,note,created_at) "
        "VALUES('gn-ci-seed','127.0.0.0/8','中国 · 上海',31.2304,121.4737,'ui_ci seed',?)", (now,))
    con.execute(
        "INSERT OR REPLACE INTO geo_cache(ip,data_json,ts) VALUES('223.5.5.5',?,?)",
        (json.dumps({"ok": True, "lat": 30.29, "lng": 120.16, "place": "中国 · 浙江 · 杭州",
                     "isp": "seed", "ip": "223.5.5.5"}, ensure_ascii=False), now))

    n_rows = 0
    for tid, ttype, urls_json in tasks:
        urls = json.loads(urls_json or "[]") if ttype == "curl" else [""]
        for url in urls:
            for nid in node_ids:
                # 总览可用率与对比页读 1h 桶；1m 供条带/明细；5m 是 1h 重算的原料
                #（sweep 的 agg_recompute 先删后插且向前多盖一个桶，重启后的首轮
                #  会拿 5m 重建「上个完整小时」的 1h —— 没有 5m 种子该桶就会变空）
                for bucket, step in (("1m", 60), ("5m", 300), ("1h", 3600)):
                    rows = []
                    ts = t_from - (t_from % step)
                    while ts <= t_to:
                        rows.append((bucket, ts, tid, nid, "", url, 1, 1, 0,
                                     12.5, 12.0, 13.0, 14.0, 0.0, 1.0,
                                     '{"200": 1}' if ttype == "curl" else "{}"))
                        ts += step
                    con.executemany(
                        "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                        "count,ok,fail,rtt_avg,rtt_p50,rtt_p95,rtt_max,loss_rate,avail_rate,"
                        "http_code_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    n_rows += len(rows)

    # 把聚合游标钉到「当前进行中的桶」：重启后首轮 sweep 只会重算当前桶 + 上一个
    # 完整桶（迟到窗口），而上一个完整桶的重建原料（5m/1m 种子）已就位 —— 历史种子不再被删空
    for bucket, step in (("1m", 60), ("5m", 300), ("1h", 3600), ("1d", 86400)):
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    (f"agg_cursor_{bucket}", str((now // step) * step)))

    fail_tid = con.execute("SELECT id FROM tasks WHERE name='curl-fail'").fetchone()
    fail_tid = fail_tid[0] if fail_tid else (tasks[0][0] if tasks else "")
    ch_id = con.execute("SELECT id FROM notify_channels LIMIT 1").fetchone()
    ch_id = ch_id[0] if ch_id else ""
    nid1 = node_ids[0] if node_ids else ""
    url9 = "http://127.0.0.1:9/"
    con.execute(
        "INSERT INTO incidents(task_id,node_id,dns,url,started_at,ended_at,duration_ms,"
        "kind,reason_json,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (fail_tid, nid1, "", url9, now - 7200, now - 5400, 1_800_000, "probe",
         json.dumps({"error_class": "connect_error", "fail_streak": 3}, ensure_ascii=False),
         "CI 种子事件"))
    cur = con.execute(
        "INSERT INTO alerts(ts,rule_id,rule_name,metric,key,status,severity,title,text,"
        "target_json,delivered,n_channels,n_ok,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now - 7200, "seed-rule", "种子规则", "avail", f"task:{fail_tid}", "resolved",
         "warning", "可用率低于阈值告警", "CI 种子告警：可用率 0%（已恢复）",
         json.dumps({"task": fail_tid}, ensure_ascii=False), 1, 1, 0, "seeded"))
    alert_id = cur.lastrowid
    con.execute(
        "INSERT INTO notify_outbox(ts,alert_id,channel_id,title,text,attempts,"
        "next_retry_at,status,last_error,done_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now - 7200, alert_id, ch_id, "可用率低于阈值告警", "CI 种子通知", 1, 0,
         "done", "", now - 7100))
    con.commit()
    con.close()
    print(f"[ui_ci] 已补种历史：{n_rows} 条 1m 聚合 + 1 事件 + 1 告警 + 1 重投记录")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8622)
    ap.add_argument("--keep", action="store_true", help="调试：保留临时目录")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    tmp = Path(tempfile.mkdtemp(prefix="gpm-ui-ci-"))
    (tmp / "ag1").mkdir()
    (tmp / "ag2").mkdir()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    procs: list = []
    rc = 1
    try:
        (tmp / "server.yaml").write_text(
            'server:\n'
            f'  listen: "127.0.0.1:{args.port}"\n'
            f'  database: "{(tmp / "ui-ci.db").as_posix()}"\n'
            'agent:\n'
            '  register_token: "ui-ci-token"\n'
            'logging:\n  level: "INFO"\n', encoding="utf-8")
        for i in (1, 2):
            (tmp / f"agent{i}.yaml").write_text(
                'agent:\n'
                f'  server_url: "{base}"\n'
                '  register_token: "ui-ci-token"\n'
                f'  name: "ci-node-{i}"\n'
                f'  data_dir: "{(tmp / f"ag{i}").as_posix()}"\n'
                '  heartbeat_interval: 10\n'
                '  report_interval: 3\n', encoding="utf-8")

        log_s = open(tmp / "server.log", "w", encoding="utf-8")
        procs.append(spawn([sys.executable, "-m", "gpm", "--config",
                            str(tmp / "server.yaml"), "server"], ROOT, env, log_s))
        wait_for(lambda: api(base, "/api/health")["ok"] is True,
                 "server 就绪", 60)
        print("[ui_ci] server 已就绪")

        # 双节点：还原本地实例的「每任务多流」形态（mtr 条带 ≥2 行等断言依赖它）
        for i in (1, 2):
            procs.append(spawn([sys.executable, "-m", "gpm", "--config",
                                str(tmp / f"agent{i}.yaml"), "agent"], ROOT, env,
                               open(tmp / f"agent{i}.log", "w", encoding="utf-8")))
        wait_for(lambda: len([n for n in api(base, "/api/nodes")
                              if n["status"] == "online"]) >= 2,
                 "两个 agent 注册并在线", 120)
        print("[ui_ci] 两个节点已在线")

        api(base, "/api/alerts/channels", "POST", {
            "name": "本地演练", "type": "webhook",
            "config": {"url": "http://127.0.0.1:9/hook"}, "enabled": True})
        api(base, "/api/tasks", "POST", {
            "name": "curl-baidu-multi", "type": "curl", "interval_seconds": 10,
            "urls": [f"{base}/api/health", f"{base}/"], "nodes": []})
        api(base, "/api/tasks", "POST", {
            "name": "ping-example", "type": "ping", "target": "223.5.5.5",
            "interval_seconds": 10, "nodes": []})
        api(base, "/api/tasks", "POST", {
            "name": "mtr-ci", "type": "mtr", "target": "127.0.0.1",
            "interval_seconds": 60, "params": {"cycles": 10, "max_hops": 30, "timeout": 45},
            "nodes": []})
        api(base, "/api/tasks", "POST", {
            "name": "curl-fail", "type": "curl", "interval_seconds": 10,
            "urls": ["http://127.0.0.1:9/"], "nodes": []})
        # P2 新类型/新参数的真实数据任务：tcp 建连（目标是本 server 自身端口，必可达）、
        # dns 双线路逐线路表、curl 关键字命中、curl 证书余量（真实 https 出口）
        api(base, "/api/tasks", "POST", {
            "name": "tcp-ci", "type": "tcp", "target": f"127.0.0.1:{args.port}",
            "interval_seconds": 10, "nodes": []})
        api(base, "/api/tasks", "POST", {
            "name": "dns-ci", "type": "dns", "target": "example.com",
            "dns": ["udp:223.5.5.5", "udp:119.29.29.29"],
            "interval_seconds": 30, "nodes": []})       # dns 间隔下限 30s
        api(base, "/api/tasks", "POST", {
            "name": "curl-keyword-ci", "type": "curl", "target": "127.0.0.1",
            "interval_seconds": 10,
            "urls": [f"{base}/api/health"], "params": {"keyword": "ok"}, "nodes": []})
        api(base, "/api/tasks", "POST", {
            "name": "curl-cert-ci", "type": "curl", "interval_seconds": 30,
            "urls": ["https://www.baidu.com"], "params": {"cert_check": True}, "nodes": []})
        # 停用任务：仅供「停用展示 + 启停审计」断言（curl-fail 不能停——事件全靠它）
        dis = api(base, "/api/tasks", "POST", {
            "name": "tcp-disabled-ci", "type": "tcp", "target": "127.0.0.1:9",
            "interval_seconds": 10, "nodes": []})
        api(base, f"/api/tasks/{dis['id']}", "PUT", {"enabled": False})
        print("[ui_ci] 任务与渠道已创建（9 任务（含 1 停用）+ 1 不可达 webhook 渠道）")

        # 事件由 curl-fail 的连续失败自动开启（fail_streak 阈值 + 上报周期）
        wait_for(lambda: len(api(base, "/api/query/incidents?open_only=true")) >= 1,
                 "故障任务产生事件", 240, interval=5)
        # dns 间隔下限 30s：至少跑出 2 轮（tcp/curl 10s 间隔顺带攒足），故比旧四任务时代多等一会
        print("[ui_ci] 事件已出现，再等 120s 让实时数据积累（dns ≥2 轮、tcp ≥10 轮）")
        time.sleep(120)

        stop(procs)
        procs = []
        seed_history((tmp / "ui-ci.db").as_posix())

        log_s = open(tmp / "server2.log", "w", encoding="utf-8")
        procs.append(spawn([sys.executable, "-m", "gpm", "--config",
                            str(tmp / "server.yaml"), "server"], ROOT, env, log_s))
        wait_for(lambda: api(base, "/api/health")["ok"] is True, "server 重启", 60)
        for i in (1, 2):
            procs.append(spawn([sys.executable, "-m", "gpm", "--config",
                                str(tmp / f"agent{i}.yaml"), "agent"], ROOT, env,
                               open(tmp / f"agent{i}.2.log", "w", encoding="utf-8")))
        wait_for(lambda: len([n for n in api(base, "/api/nodes")
                              if n["status"] == "online"]) >= 2, "节点重连", 120)
        # 自检：验收前直接看对比端点的关键字段（历史窗口是否触发「自动切前一时段」）
        t0 = api(base, "/api/tasks")[0]
        cdbg = api(base, f"/api/compare?task_id={t0['id']}&mode=yesterday&metric=avail")
        cdbg2 = api(base, f"/api/compare?task_id={t0['id']}&mode=prev&metric=avail")
        print(f"[ui_ci] compare 自检 task={t0['name']}: "
              f"yesterday(has_today={cdbg['has_today']}, has_other={cdbg['has_other']}, "
              f"history_hours={cdbg['history_hours']}) | "
              f"prev(has_other={cdbg2['has_other']}, window={cdbg2['window_hours']}h)")
        print("[ui_ci] 实例带历史数据重启完成，开始浏览器验收")

        acc_env = dict(env, GPM_UI_CHANNEL="")   # CI 用 Playwright 自带 chromium
        acc = subprocess.run(
            [sys.executable, "scripts/ui_acceptance.py", "--base", base,
             "--out", "artifacts/ui-ci"], cwd=ROOT, env=acc_env)
        rc = acc.returncode
        print(f"[ui_ci] 浏览器验收退出码 {rc}")
        return rc
    except Exception as e:  # noqa: BLE001 - CI 脚本要给出可定位的失败原因
        print(f"[ui_ci] 失败：{e!r}")
        return 2
    finally:
        stop(procs)
        for p in procs:
            if p.poll() is None:
                print(f"[ui_ci] 警告：进程 {p.pid} 未按期退出")
        if args.keep:
            print(f"[ui_ci] 临时目录保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
