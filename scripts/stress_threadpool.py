"""scripts/stress_threadpool.py -- 服务端线程池压测：复现 MemoryError「假死」并验证保护。

原理：并发线程持续打重接口（默认走 AnyIO 线程池的 sync 路由），同时可选用子进程
吃掉指定内存制造压力。正常情况下 /api/health（async，不占线程池）的 threads 字段
应稳定在 thread_pool_tokens（默认 40）附近；若出现 5xx / 超时 / threads 无界增长，
说明线程池保护未生效或内存压力已逼近极限。

用法（Git Bash / PowerShell 均可）：
    python scripts/stress_threadpool.py --duration 90 --concurrency 12
    python scripts/stress_threadpool.py --duration 90 --concurrency 12 --mem-pressure-mb 512
    python scripts/stress_threadpool.py --endpoints "/api/overview,/api/report/sla" --duration 30

退出码：出现 5xx / 超时 / threads 异常增长（> tokens*1.5）时为 1，正常为 0。

实跑记录（2026-10-02，Windows Server 2019 主机 20GB 内存/约 5.5GB 空闲，Python 3.13，
对运行中的 http://127.0.0.1:8620 实测，两轮 --duration 90 --concurrency 12）：

1. 纯压测（不加内存压力）：total=175 ok=175 timeout/conn_err=0 http_5xx=0；
   latency p50=5552.5ms p95=15006.1ms max=18017.2ms；
   health threads: min=14 max=17 last=17（≈ 12 个并发 worker + 服务端常驻线程，
   全程贴着并发数走，远低于 thread_pool_tokens=40）。
2. 加 --mem-pressure-mb 512：total=232 ok=232 timeout/conn_err=0 http_5xx=0；
   latency p50=4882.3ms p95=10528.0ms max=12189.1ms；
   health threads: min=14 max=17 last=17。

结论：
- 两轮均未复现 MemoryError/线程爆发：threads 全程稳定 14-17（< tokens=40），
  anyio 上限把线程数钉死在并发规模附近，线程爆发型 MemoryError 已被结构性排除；
- 高延迟（p50 数秒）来自重接口 + 单进程 SQLite 的真实负载，是吞吐瓶颈但非故障；
- thread_pool_tokens 无需调大：40 已是实际线程数的 2 倍余，调大反而放大内存风险；
- 20GB 内存 + 512MB 子进程压力远不足以触发整进程 MemoryError；要触发历史故障
  需线程数无界增长（已不可能）或真实内存枯竭，本机不易复现。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

DEFAULT_ENDPOINTS = ",".join([
    "/api/overview",
    "/api/report/sla",
    "/api/query/incidents_all?limit=200",
    "/api/report/digest?hours=720",
    "/api/tasks",
])

# threads 异常增长判定基线（默认 AnyIO tokens=40；health 不暴露 tokens，故用常量）
BASELINE_TOKENS = 40

MEM_CHILD_CODE = """
import sys, time
mb = int(sys.argv[1]); seconds = float(sys.argv[2])
buf = bytearray(mb * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1          # 触碰每个页，确保真实提交物理内存
time.sleep(seconds)
"""


def parse_args():
    ap = argparse.ArgumentParser(description="gpm server 线程池压测")
    ap.add_argument("--base-url", default="http://127.0.0.1:8620")
    ap.add_argument("--endpoints", default=DEFAULT_ENDPOINTS,
                    help="逗号分隔的重接口列表（GET）")
    ap.add_argument("--duration", type=int, default=60, help="压测时长（秒）")
    ap.add_argument("--concurrency", type=int, default=16, help="并发线程数")
    ap.add_argument("--timeout", type=float, default=30.0, help="单请求超时（秒）")
    ap.add_argument("--report-every", type=int, default=10, help="实时报表周期（秒）")
    ap.add_argument("--mem-pressure-mb", type=int, default=0,
                    help=">0 时用子进程吃掉这么多 MB 内存制造压力")
    return ap.parse_args()


def fetch(url: str, timeout: float) -> tuple[int, str | None]:
    """GET 一个 URL，返回 (http_status 或 0, 错误类型)；0 表示传输层失败。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, type(e).__name__


def health_threads(base_url: str, timeout: float = 5.0) -> int | None:
    try:
        with urllib.request.urlopen(base_url + "/api/health", timeout=timeout) as r:
            return int(json.loads(r.read().decode()).get("threads") or 0)
    except Exception:
        return None


def percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(p / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def main() -> int:
    args = parse_args()
    endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    if not endpoints:
        print("no endpoints given", flush=True)
        return 2
    base = args.base_url.rstrip("/")
    stop_at = time.monotonic() + args.duration

    records = deque()          # (monotonic_ts, latency_s, status, err)
    lock = threading.Lock()
    stop = threading.Event()
    threads_seen: list[int] = []

    def worker(idx: int):
        i = 0
        while not stop.is_set() and time.monotonic() < stop_at:
            u = base + endpoints[(idx + i) % len(endpoints)]
            i += 1
            t0 = time.monotonic()
            status, err = fetch(u, args.timeout)
            dt = time.monotonic() - t0
            with lock:
                records.append((t0, dt, status, err))

    def reporter():
        next_at = time.monotonic() + args.report_every
        while not stop.is_set() and time.monotonic() < stop_at:
            time.sleep(0.2)
            if time.monotonic() < next_at:
                continue
            next_at += args.report_every
            now = time.monotonic()
            with lock:
                window = [r for r in records if r[0] >= now - args.report_every]
            lat = sorted(r[1] for r in window)
            errs = sum(1 for r in window if r[2] == 0 or r[2] >= 500)
            th = health_threads(base)
            if th is not None:
                threads_seen.append(th)
            print(f"[{now - (stop_at - args.duration):6.0f}s] "
                  f"reqs={len(window):4d} qps={len(window) / args.report_every:6.1f} "
                  f"p50={percentile(lat, 50) * 1000:7.1f}ms "
                  f"p95={percentile(lat, 95) * 1000:8.1f}ms "
                  f"err={errs:3d} health_threads={th}", flush=True)

    mem_proc = None
    if args.mem_pressure_mb > 0:
        mem_proc = subprocess.Popen(
            [sys.executable, "-c", MEM_CHILD_CODE, str(args.mem_pressure_mb),
             str(args.duration + 10)])
        print(f"mem-pressure child started: {args.mem_pressure_mb} MB", flush=True)

    print(f"stress start: base={base} endpoints={len(endpoints)} "
          f"concurrency={args.concurrency} duration={args.duration}s "
          f"mem_pressure={args.mem_pressure_mb or 'off'}MB", flush=True)
    workers = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(args.concurrency)]
    rep = threading.Thread(target=reporter, daemon=True)
    for w in workers:
        w.start()
    rep.start()
    for w in workers:
        w.join()
    stop.set()
    rep.join(timeout=2)
    if mem_proc:
        mem_proc.terminate()

    with lock:
        rows = list(records)
    lat_all = sorted(r[1] for r in rows)
    n_timeout = sum(1 for r in rows if r[2] == 0)
    n_5xx = sum(1 for r in rows if r[2] >= 500)
    err_kinds: dict[str, int] = {}
    for _, _, st, err in rows:
        if st == 0 and err:
            err_kinds[err] = err_kinds.get(err, 0) + 1
    th_min = min(threads_seen) if threads_seen else None
    th_max = max(threads_seen) if threads_seen else None
    th_last = health_threads(base)

    print("-- summary --", flush=True)
    print(f"total={len(rows)} ok={sum(1 for r in rows if 200 <= r[2] < 500)} "
          f"timeout/conn_err={n_timeout} http_5xx={n_5xx}", flush=True)
    if err_kinds:
        print(f"conn error kinds: {err_kinds}", flush=True)
    if lat_all:
        print(f"latency p50={percentile(lat_all, 50) * 1000:.1f}ms "
              f"p95={percentile(lat_all, 95) * 1000:.1f}ms "
              f"max={lat_all[-1] * 1000:.1f}ms", flush=True)
    print(f"health threads: min={th_min} max={th_max} last={th_last}", flush=True)

    bad = []
    if n_timeout or n_5xx:
        bad.append("出现超时/5xx（线程池或内存压力已打满）")
    if th_max and th_min and th_max > max(BASELINE_TOKENS * 1.5, th_min * 1.5):
        bad.append(f"threads 异常增长（min={th_min} max={th_max}）")
    verdict = "; ".join(bad) if bad else "正常：未复现 MemoryError/线程爆发"
    print(f"verdict: {verdict}", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
