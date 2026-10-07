#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gpm SQLite 热备份脚本（.docs/PROGRESS.md「数据备份/恢复」欠账项）。

用法:
    python scripts/backup_db.py                  # 备份到 backups/gpm-YYYYmmdd-HHMMSS.db
    python scripts/backup_db.py -o my.db         # 指定输出文件
    python scripts/backup_db.py --keep 7         # backups/ 下只保留最近 7 份（默认 30）
    python scripts/backup_db.py --verify X.db    # 校验一份备份（integrity_check + 行数摘要）

实现: sqlite3 在线 backup API（Connection.backup）——对 WAL 库是唯一正确姿势:
    - 不用文件复制（WAL/-shm 状态下拷出来的可能不一致）
    - 不用 VACUUM INTO 之外的事务读（会阻塞写入）
backup 在源库上短暂加共享锁，对运行中的服务端影响 ≈ 一次普通读。
恢复: 停服务端 → cp 备份文件到 data/gpm.db → 删掉 gpm.db-wal/-shm → 起服务端。
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None


def do_backup(src: Path, dst: Path) -> int:
    if not src.exists():
        print(f"[ERR] 源库不存在: {src}")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    with d:
        s.backup(d)          # 在线热备份：走 pager 层，不需要停服务
    s.close(); d.close()
    size = dst.stat().st_size
    print(f"[OK] {src} -> {dst} ({size / 1048576:.1f} MiB, {time.monotonic() - t0:.1f}s)")
    return 0


def do_prune(backup_dir: Path, keep: int) -> None:
    backups = sorted(backup_dir.glob("gpm-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"[prune] 删除过期备份 {old.name}")


def do_verify(path: Path) -> int:
    if not path.exists():
        print(f"[ERR] 备份不存在: {path}")
        return 1
    db = sqlite3.connect(str(path))
    ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"integrity_check: {ok}")
    # 关键表行数摘要（恢复演练时肉眼比对）
    for tbl in ("nodes", "tasks", "probe_results", "aggregates", "incidents", "alerts", "tokens"):
        try:
            n = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl:14} {n:>9} 行")
        except sqlite3.OperationalError:
            print(f"  {tbl:14} （表不存在）")
    db.close()
    return 0 if ok == "ok" else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="gpm SQLite 热备份")
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--db", default=str(here / "data" / "gpm.db"), help="源库路径（默认 data/gpm.db）")
    ap.add_argument("-o", "--out", default="", help="输出文件（默认 backups/gpm-时间戳.db）")
    ap.add_argument("--keep", type=int, default=30, help="保留最近 N 份（默认 30，0=不清理）")
    ap.add_argument("--verify", default="", help="只校验指定备份文件，不做备份")
    args = ap.parse_args()

    if args.verify:
        return do_verify(Path(args.verify))
    src = Path(args.db)
    dst = Path(args.out) if args.out else here / "backups" / time.strftime("gpm-%Y%m%d-%H%M%S.db")
    rc = do_backup(src, dst)
    if rc == 0 and args.keep > 0:
        do_prune(dst.parent if args.out else here / "backups", args.keep)
    return rc


if __name__ == "__main__":
    sys.exit(main())
