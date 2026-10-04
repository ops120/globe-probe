"""gpm 命令行入口：server / agent / doctor / task / report / migrate / cache。"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .common.util import now, run_cmd


def setup_logging(cfg_logging: dict, verbose: bool):
    level = logging.DEBUG if verbose else getattr(logging, cfg_logging.get("level", "INFO"))
    handlers: list = [logging.StreamHandler(sys.stdout)]
    if cfg_logging.get("file"):
        Path(cfg_logging["file"]).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(cfg_logging["file"], encoding="utf-8"))
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def cmd_server(args, cfg):
    import uvicorn
    from .server.app import create_app
    app = create_app(cfg)
    host, _, port = cfg.server["listen"].rpartition(":")
    uvicorn.run(app, host=host, port=int(port),
                log_level="debug" if args.verbose else "info")


def cmd_agent(args, cfg):
    import asyncio
    from .agent.agent import Agent
    if args.server:
        cfg.agent["server_url"] = args.server
    if args.name:
        cfg.agent["name"] = args.name
    if args.token:
        cfg.agent["register_token"] = args.token
    asyncio.run(Agent(cfg).run())


def cmd_doctor(args, cfg):
    """环境自检：Python、外部工具、解析、数据库、服务端连通性。"""
    checks: list[tuple[str, bool, str]] = []

    v = sys.version_info
    checks.append(("Python >= 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"))

    for tool in (["ping"], ["curl", "--version"], ["mtr", "--version"]):
        try:
            code, out, err = run_cmd(tool, 8)
            first = (out or err).strip().splitlines()[0][:60] if (out or err) else "ok"
            checks.append((f"工具 {tool[0]}", code == 0 or tool[0] != "mtr", first))
        except Exception as e:  # noqa
            ok = tool[0] == "mtr"  # mtr 缺失可容忍（Windows/未装）
            checks.append((f"工具 {tool[0]}", ok, f"不可用: {e}"))

    # ping 实测（本机回环）
    try:
        from .probers.ping import run_ping
        r = run_ping({"target": "127.0.0.1", "params": {"count": 2, "timeout": 1}},
                     "", None, now())
        checks.append(("ping 探测(127.0.0.1)", r["status"] == "ok",
                       f"status={r['status']} rtt_avg={r['metrics'].get('rtt_avg')}"))
    except Exception as e:  # noqa
        checks.append(("ping 探测(127.0.0.1)", False, str(e)))

    # curl 实测
    try:
        from .probers.curl import run_curl
        r = run_curl({"params": {"timeout": 8}}, "https://www.baidu.com", "", "", None, now())
        checks.append(("curl 探测(baidu)", r["status"] == "ok",
                       f"http={r['metrics'].get('http_code')} total={r['metrics'].get('total_time')}ms"))
    except Exception as e:  # noqa
        checks.append(("curl 探测(baidu)", False, str(e)))

    # DNS 指定服务器解析（走 DoH 兜底链）
    try:
        from .common.dnsres import resolve_a
        ips, ms, tr = resolve_a("www.baidu.com", "223.5.5.5", timeout=3)
        checks.append(("DNS 解析(223.5.5.5)", True, f"{ips[0]} via {tr} {ms:.0f}ms"))
    except Exception as e:  # noqa
        checks.append(("DNS 解析(223.5.5.5)", False, str(e)))

    # 数据库可写
    try:
        db = cfg.server["database"]
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        from .server.storage import Storage
        s = Storage(db)
        s.meta_set("doctor_last", str(now()))
        checks.append(("数据库可写", True, db))
    except Exception as e:  # noqa
        checks.append(("数据库可写", False, str(e)))

    # 服务端连通
    try:
        import urllib.request
        with urllib.request.urlopen(cfg.agent["server_url"] + "/api/health", timeout=5) as r:
            checks.append(("服务端连通", r.status == 200, cfg.agent["server_url"]))
    except Exception as e:  # noqa
        checks.append(("服务端连通", False, f"{cfg.agent['server_url']}: {e}"))

    print("\n=== gpm doctor 环境自检 ===")
    fail = 0
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        if not ok:
            fail += 1
        print(f" [{mark}] {name:26s} {detail}")
    print(f"=== {len(checks) - fail}/{len(checks)} 通过 ===")
    sys.exit(0 if fail == 0 else 1)


def cmd_task(args, cfg):
    from .server.storage import Storage
    s = Storage(cfg.server["database"])
    if args.action == "add":
        from .common.util import new_id
        if args.type == "curl":
            urls = [u.strip() for u in args.target.split(",") if u.strip()]
            target = ""
        else:
            urls, target = [], args.target
        t = s.create_task(new_id("t"), args.name, args.type, target, urls,
                          {"count": args.count, "timeout": args.timeout} if args.type == "ping" else
                          {"timeout": args.timeout},
                          [d for d in (args.dns or "").split(",") if d.strip()],
                          args.interval, now())
        print(json.dumps(t, ensure_ascii=False, indent=1))
    elif args.action == "list":
        for t in s.list_tasks():
            print(f"{t['id']}  {t['name']:24s} {t['type']:5s} {t['target'][:40]:40s} "
                  f"{t['interval_seconds']}s dns={','.join(t['dns']) or '-'} "
                  f"enabled={t['enabled']} v{t['config_version']}")
    elif args.action == "enable" or args.action == "disable":
        t = s.update_task(args.task_id, {"enabled": 1 if args.action == "enable" else 0}, now())
        print(json.dumps(t, ensure_ascii=False, indent=1))


def cmd_report(args, cfg):
    from .server.storage import Storage
    s = Storage(cfg.server["database"])
    t_to = args.to or now()
    t_from = args.from_ or (t_to - 86400)
    with s.lock:
        rows = s.db.execute(
            "SELECT * FROM probe_results WHERE task_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (args.task_id, t_from, t_to)).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
        data.append(d)
    out = json.dumps(data, ensure_ascii=False, indent=1) if args.format == "json" else \
        "\n".join(f"{d['ts']}\t{d['node_id']}\t{d['dns']}\t{d['status']}\t{d['error_class']}"
                  for d in data)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"已导出 {len(data)} 条 → {args.output}")
    else:
        print(out[:5000])


def cmd_migrate(args, cfg):
    from .server.storage import Storage
    Storage(cfg.server["database"])
    print("schema 已就绪（启动时自动迁移）。")


def cmd_cache(args, cfg):
    from .agent.agent import Agent
    if args.yes or input("确认清空本地缓冲与解析缓存? [y/N] ").lower() == "y":
        Agent(cfg).clear_cache()
        print("已清空。")
    else:
        print("已取消。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpm", description="gpm · 全球拨测监控平台")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("server", help="启动服务端")
    sp.set_defaults(fn=cmd_server)

    ap = sub.add_parser("agent", help="启动节点 Agent")
    ap.add_argument("--server", default="", help="服务端地址（覆盖配置）")
    ap.add_argument("--name", default="", help="节点名（覆盖配置）")
    ap.add_argument("--token", default="", help="注册 Token（覆盖配置）")
    ap.set_defaults(fn=cmd_agent)

    sub.add_parser("doctor", help="环境自检").set_defaults(fn=cmd_doctor)

    tp = sub.add_parser("task", help="任务管理")
    tp.add_argument("action", choices=["add", "list", "enable", "disable"])
    tp.add_argument("--name", default="task")
    tp.add_argument("--type", default="ping", choices=["ping", "curl", "mtr"])
    tp.add_argument("--target", default="", help="目标；curl 可逗号分隔多个 URL")
    tp.add_argument("--interval", type=int, default=10)
    tp.add_argument("--dns", default="", help="逗号分隔 DNS 服务器")
    tp.add_argument("--count", type=int, default=4)
    tp.add_argument("--timeout", type=float, default=2.0)
    tp.add_argument("--task-id", default="")
    tp.set_defaults(fn=cmd_task)

    rp = sub.add_parser("report", help="结果导出")
    rp.add_argument("--task-id", required=True)
    rp.add_argument("--from", dest="from_", type=int, default=0)
    rp.add_argument("--to", dest="to", type=int, default=0)
    rp.add_argument("--format", choices=["json", "text"], default="json")
    rp.add_argument("--output", default="")
    rp.set_defaults(fn=cmd_report)

    sub.add_parser("migrate", help="数据库迁移").set_defaults(fn=cmd_migrate)

    cp = sub.add_parser("cache", help="清理本地缓存/缓冲（需确认）")
    cp.add_argument("--yes", action="store_true")
    cp.set_defaults(fn=cmd_cache)
    return p


def main():
    args = build_parser().parse_args()
    from .config import load_config
    cfg = load_config(args.config if Path(args.config).exists() else None)
    setup_logging(cfg.logging, args.verbose)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
