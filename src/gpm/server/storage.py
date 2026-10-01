"""SQLite(WAL) 存储层：全部 SQL 集中于此（规模增长时单点替换为 TimescaleDB/VM）。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..common.util import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, token_hash TEXT, tags_json TEXT DEFAULT '{}',
  version TEXT DEFAULT '', status TEXT DEFAULT 'online', system_json TEXT DEFAULT '{}',
  local_ip TEXT DEFAULT '', egress_ip TEXT DEFAULT '', online_since INTEGER DEFAULT 0,
  token_id TEXT DEFAULT '',
  last_heartbeat INTEGER DEFAULT 0, created_at INTEGER, updated_at INTEGER);

CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, type TEXT, target TEXT DEFAULT '',
  urls_json TEXT, params_json TEXT DEFAULT '{}', dns_json TEXT DEFAULT '[]',
  nodes_json TEXT DEFAULT '[]',
  interval_seconds INTEGER, enabled INTEGER DEFAULT 1,
  config_version INTEGER DEFAULT 1, created_at INTEGER, updated_at INTEGER);

CREATE TABLE IF NOT EXISTS task_revisions(
  task_id TEXT, config_version INTEGER, snapshot_json TEXT, published_at INTEGER,
  PRIMARY KEY(task_id, config_version));

CREATE TABLE IF NOT EXISTS probe_results(
  ts INTEGER, task_id TEXT, node_id TEXT, type TEXT,
  dns TEXT DEFAULT '', url TEXT DEFAULT '',
  status TEXT, error_class TEXT DEFAULT '', error TEXT DEFAULT '',
  dns_server TEXT DEFAULT '', resolved_ip TEXT DEFAULT '', dns_time_ms REAL,
  metrics_json TEXT DEFAULT '{}', config_version INTEGER DEFAULT 0,
  ingested_at INTEGER,
  PRIMARY KEY(task_id, node_id, type, dns, url, ts));
CREATE INDEX IF NOT EXISTS ix_res_ts ON probe_results(ts);
CREATE INDEX IF NOT EXISTS ix_res_node_ts ON probe_results(node_id, ts);
CREATE INDEX IF NOT EXISTS ix_res_task_ts ON probe_results(task_id, ts);

CREATE TABLE IF NOT EXISTS aggregates(
  bucket TEXT, ts INTEGER, task_id TEXT, node_id TEXT, dns TEXT DEFAULT '', url TEXT DEFAULT '',
  count INTEGER, ok INTEGER, fail INTEGER,
  rtt_avg REAL, rtt_p50 REAL, rtt_p95 REAL, rtt_max REAL,
  loss_rate REAL, avail_rate REAL, http_code_json TEXT DEFAULT '{}',
  PRIMARY KEY(bucket, ts, task_id, node_id, dns, url));

CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, node_id TEXT, dns TEXT, url TEXT,
  started_at INTEGER, ended_at INTEGER, duration_ms INTEGER,
  kind TEXT DEFAULT 'probe',            -- probe=探测失败  node=节点离线/恢复
  reason_json TEXT DEFAULT '{}', note TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_inc_start ON incidents(started_at);

-- 节点分组（组级任务分配）
CREATE TABLE IF NOT EXISTS groups(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, note TEXT DEFAULT '', created_at INTEGER);
CREATE TABLE IF NOT EXISTS group_members(
  group_id TEXT, node_id TEXT, PRIMARY KEY(group_id, node_id));

-- 注册 Token 管理（多 Token/吊销；config 里的单 Token 退化为引导用）
CREATE TABLE IF NOT EXISTS tokens(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, token_hash TEXT, note TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1, created_at INTEGER, last_used_at INTEGER DEFAULT 0,
  revoked_at INTEGER DEFAULT 0);

-- GeoIP 结果缓存（避免频繁打外部接口）
CREATE TABLE IF NOT EXISTS geo_cache(
  ip TEXT PRIMARY KEY, data_json TEXT DEFAULT '{}', ts INTEGER);

-- 自定义「IP 段 → 位置」：IDC 内网段/固定出口段直接指定地理位置（优先于在线查询）
CREATE TABLE IF NOT EXISTS geo_networks(
  id TEXT PRIMARY KEY, cidr TEXT UNIQUE, place TEXT, lat REAL, lng REAL,
  note TEXT DEFAULT '', created_at INTEGER);

CREATE TABLE IF NOT EXISTS node_heartbeats(
  node_id TEXT, ts INTEGER, cpu REAL, mem REAL, tasks INTEGER,
  PRIMARY KEY(node_id, ts));

CREATE TABLE IF NOT EXISTS errors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, source TEXT, code TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

BUCKET_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            # 轻量迁移：老库补列
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(tasks)")]
            if "nodes_json" not in cols:
                self.db.execute("ALTER TABLE tasks ADD COLUMN nodes_json TEXT DEFAULT '[]'")
            icols = [r[1] for r in self.db.execute("PRAGMA table_info(incidents)")]
            if "kind" not in icols:
                self.db.execute("ALTER TABLE incidents ADD COLUMN kind TEXT DEFAULT 'probe'")
            ncols = [r[1] for r in self.db.execute("PRAGMA table_info(nodes)")]
            for col, ddl in (("local_ip", "TEXT DEFAULT ''"),
                             ("egress_ip", "TEXT DEFAULT ''"),
                             ("online_since", "INTEGER DEFAULT 0"),
                             ("token_id", "TEXT DEFAULT ''")):
                if col not in ncols:
                    self.db.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('config_version','1')")
            self.db.commit()

    # ---------- meta ----------
    def meta_get(self, key: str, default: str = "") -> str:
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def meta_set(self, key: str, value: str):
        with self.lock:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.commit()

    # ---------- nodes ----------
    def register_node(self, name: str, token_hash: str, tags: dict,
                      version: str, system: dict, ts: int,
                      local_ip: str = "", egress_ip: str = "",
                      token_id: str = "") -> tuple[str, bool]:
        """按名字幂等注册。返回 (node_id, created)。

        online_since：本次「在线时段」的起点。旧状态不是 online（离线后回归/首次）时重置为 ts。
        """
        with self.lock:
            r = self.db.execute("SELECT id FROM nodes WHERE name=?", (name,)).fetchone()
            if r:
                self.db.execute(
                    "UPDATE nodes SET token_hash=?, tags_json=?, version=?, status='online',"
                    " system_json=?, token_id=?,"
                    " local_ip=CASE WHEN ?<>'' THEN ? ELSE local_ip END,"
                    " egress_ip=CASE WHEN ?<>'' THEN ? ELSE egress_ip END,"
                    " online_since=CASE WHEN status<>'online' OR COALESCE(online_since,0)=0"
                    "                   THEN ? ELSE online_since END,"
                    " last_heartbeat=?, updated_at=? WHERE id=?",
                    (token_hash, json.dumps(tags, ensure_ascii=False), version,
                     json.dumps(system, ensure_ascii=False), token_id,
                     local_ip, local_ip, egress_ip, egress_ip, ts, ts, ts, r["id"]))
                self.db.commit()
                return r["id"], False
            nid = "n" + name.encode("utf-8").hex()[:12] + secrets4()
            self.db.execute(
                "INSERT INTO nodes(id,name,token_hash,tags_json,version,status,system_json,"
                "local_ip,egress_ip,online_since,token_id,last_heartbeat,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'online',?,?,?,?,?,?,?,?)",
                (nid, name, token_hash, json.dumps(tags, ensure_ascii=False), version,
                 json.dumps(system, ensure_ascii=False), local_ip, egress_ip, ts, token_id,
                 ts, ts, ts))
            self.db.commit()
            return nid, True

    def node_by_id(self, node_id: str):
        with self.lock:
            return self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()

    def node_touch(self, node_id: str, stats: dict, ts: int, egress_ip: str = ""):
        with self.lock:
            local_ip = str(stats.get("local_ip") or "")
            cur = self.db.execute("SELECT status FROM nodes WHERE id=?", (node_id,)).fetchone()
            if cur and cur["status"] != "online":
                # 离线 → 恢复：关闭节点侧未恢复事件（事件流里显示「已恢复 + 时长」）
                self.db.execute(
                    "UPDATE incidents SET ended_at=?, duration_ms=MAX(0,(?-started_at))*1000"
                    " WHERE kind='node' AND node_id=? AND ended_at IS NULL", (ts, ts, node_id))
            self.db.execute(
                "UPDATE nodes SET status='online', last_heartbeat=?, updated_at=?,"
                " version=CASE WHEN ?<>'' THEN ? ELSE version END,"
                " local_ip=CASE WHEN ?<>'' THEN ? ELSE local_ip END,"
                " egress_ip=CASE WHEN ?<>'' THEN ? ELSE egress_ip END,"
                " online_since=CASE WHEN status<>'online' OR COALESCE(online_since,0)=0"
                "                   THEN ? ELSE online_since END"
                " WHERE id=?",
                (ts, ts, str(stats.get("version") or ""), str(stats.get("version") or ""),
                 local_ip, local_ip, egress_ip, egress_ip, ts, node_id))
            self.db.execute(
                # cpu/mem 缺失时存 NULL（UI 显示「—」），不要把「采集不到」写成 0%
                "INSERT OR REPLACE INTO node_heartbeats(node_id,ts,cpu,mem,tasks) VALUES(?,?,?,?,?)",
                (node_id, ts, stats.get("cpu"), stats.get("mem"), stats.get("tasks", 0)))
            self.db.commit()

    def sweep_offline(self, timeout: int, ts: int) -> int:
        """心跳超时的节点标记离线，并写入「节点离线」事件（恢复时由 node_touch 关闭）。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, name, last_heartbeat FROM nodes WHERE status='online' AND last_heartbeat < ?",
                (ts - timeout,)).fetchall()
            for r in rows:
                self.db.execute("UPDATE nodes SET status='offline' WHERE id=?", (r["id"],))
                self.db.execute(
                    "INSERT INTO incidents(task_id,node_id,dns,url,started_at,kind,reason_json)"
                    " SELECT '','' || ?,'','',?,'node',? WHERE NOT EXISTS("
                    "  SELECT 1 FROM incidents WHERE kind='node' AND node_id=? AND ended_at IS NULL)",
                    (r["id"], ts, json.dumps(
                        {"event": "offline", "node": r["name"], "timeout": timeout,
                         "last_heartbeat": r["last_heartbeat"]}, ensure_ascii=False), r["id"]))
            self.db.commit()
            return len(rows)

    def update_node(self, node_id: str, fields: dict, ts: int) -> dict:
        """改名/标签。注意：改名后老 agent 的凭据(name:token)不匹配，会重新注册成新节点。"""
        with self.lock:
            cur = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not cur:
                raise KeyError(node_id)
            if "name" in fields and fields["name"] and fields["name"] != cur["name"]:
                dup = self.db.execute("SELECT id FROM nodes WHERE name=? AND id<>?",
                                      (fields["name"], node_id)).fetchone()
                if dup:
                    raise ValueError("节点名已存在")
            sets, vals = [], []
            for k in ("name", "tags_json"):
                if k in fields:
                    sets.append(f"{k}=?")
                    vals.append(fields[k])
            if sets:
                vals += [ts, node_id]
                self.db.execute(f"UPDATE nodes SET {', '.join(sets)}, updated_at=? WHERE id=?",
                                vals)
                self.db.commit()
            d = dict(self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())
            d["tags"] = json.loads(d.pop("tags_json") or "{}")
            return d

    def delete_node(self, node_id: str, ts: int) -> dict:
        """删除节点并级联清理其探测数据（不可逆）。同时从任务分配列表移除该节点。"""
        with self.lock:
            cur = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not cur:
                raise KeyError(node_id)
            info = dict(cur)
            self.db.execute("DELETE FROM probe_results WHERE node_id=?", (node_id,))
            self.db.execute("DELETE FROM aggregates WHERE node_id=?", (node_id,))
            self.db.execute("DELETE FROM node_heartbeats WHERE node_id=?", (node_id,))
            self.db.execute("DELETE FROM incidents WHERE node_id=?", (node_id,))
            self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            # 从任务分配中移除
            for t in self.db.execute("SELECT id, nodes_json FROM tasks WHERE nodes_json LIKE ?",
                                     (f'%"{node_id}"%',)).fetchall():
                lst = [n for n in json.loads(t["nodes_json"] or "[]") if n != node_id]
                self.db.execute("UPDATE tasks SET nodes_json=?, config_version=config_version+1,"
                                " updated_at=? WHERE id=?",
                                (json.dumps(lst), ts, t["id"]))
            self._bump_config_version()
            self.db.commit()
            return info

    def list_nodes(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM nodes ORDER BY name").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["tags"] = json.loads(d.pop("tags_json") or "{}")
                d["system"] = json.loads(d.pop("system_json") or "{}")
                hb = self.db.execute(
                    "SELECT cpu,mem,tasks FROM node_heartbeats WHERE node_id=? "
                    "ORDER BY ts DESC LIMIT 1", (r["id"],)).fetchone()
                d["cpu"] = hb["cpu"] if hb else None
                d["mem"] = hb["mem"] if hb else None
                d["hb_tasks"] = hb["tasks"] if hb else 0
                out.append(d)
            return out

    # ---------- tasks ----------
    def create_task(self, tid: str, name: str, ttype: str, target: str, urls: list[str],
                    params: dict, dns: list[str], interval: int, ts: int,
                    nodes: list[str] | None = None) -> dict:
        with self.lock:
            self.db.execute(
                "INSERT INTO tasks(id,name,type,target,urls_json,params_json,dns_json,"
                "nodes_json,interval_seconds,enabled,config_version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,1,1,?,?)",
                (tid, name, ttype, target, json.dumps(urls or []), json.dumps(params or {}),
                 json.dumps(dns or []), json.dumps(nodes or []), interval, ts, ts))
            self._snapshot_revision(tid, ts)
            self._bump_config_version()
            self.db.commit()
            return self.get_task(tid)

    def update_task(self, tid: str, fields: dict, ts: int):
        with self.lock:
            cur = self.get_task(tid)
            if not cur:
                raise KeyError(tid)
            allowed = {k: fields[k] for k in
                       ("name", "target", "urls", "params", "dns", "nodes",
                        "interval_seconds", "enabled")
                       if k in fields}
            if "urls" in allowed:
                allowed["urls_json"] = json.dumps(allowed.pop("urls") or [])
            if "params" in allowed:
                allowed["params_json"] = json.dumps(allowed.pop("params") or {})
            if "dns" in allowed:
                allowed["dns_json"] = json.dumps(allowed.pop("dns") or [])
            if "nodes" in allowed:
                allowed["nodes_json"] = json.dumps(allowed.pop("nodes") or [])
            sets = ", ".join(f"{k}=?" for k in allowed)
            vals = list(allowed.values())
            if sets:
                self.db.execute(f"UPDATE tasks SET {sets}, config_version=config_version+1,"
                                f" updated_at=? WHERE id=?", vals + [ts, tid])
            self._snapshot_revision(tid, ts)
            self._bump_config_version()
            self.db.commit()
            return self.get_task(tid)

    def delete_task(self, tid: str, ts: int):
        with self.lock:
            self.db.execute("DELETE FROM tasks WHERE id=?", (tid,))
            self._bump_config_version()
            self.db.commit()

    def _snapshot_revision(self, tid: str, ts: int):
        t = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if t:
            self.db.execute(
                "INSERT OR REPLACE INTO task_revisions(task_id,config_version,snapshot_json,published_at)"
                " VALUES(?,?,?,?)",
                (tid, t["config_version"], json.dumps(dict(t), ensure_ascii=False), ts))

    def _bump_config_version(self):
        v = int(self.meta_get("config_version", "1")) + 1
        self.meta_set("config_version", str(v))

    def config_version(self) -> int:
        return int(self.meta_get("config_version", "1"))

    def get_task(self, tid: str):
        with self.lock:
            r = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            return _task_row(r) if r else None

    def list_tasks(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
            return [_task_row(r) for r in rows]

    def enabled_tasks(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks WHERE enabled=1").fetchall()
            return [_task_row(r) for r in rows]

    # ---------- results ----------
    def insert_results(self, rows: list[dict], ts: int) -> tuple[int, int]:
        """INSERT OR IGNORE 去重。返回 (inserted, duplicates)。"""
        inserted = dup = 0
        with self.lock:
            for r in rows:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO probe_results(ts,task_id,node_id,type,dns,url,status,"
                    "error_class,error,dns_server,resolved_ip,dns_time_ms,metrics_json,"
                    "config_version,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["ts"], r["task_id"], r["node_id"], r["type"], r.get("dns", ""),
                     r.get("url", ""), r["status"], r.get("error_class", ""), r.get("error", ""),
                     r.get("dns_server", ""), r.get("resolved_ip", ""), r.get("dns_time_ms"),
                     json.dumps(r.get("metrics") or {}, ensure_ascii=False),
                     r.get("config_version", 0), ts))
                if cur.rowcount:
                    inserted += 1
                else:
                    dup += 1
            self.db.commit()
        if dup:
            self.log_error(ts, "ingest", "duplicate", f"{dup} 条重复结果被丢弃")
        return inserted, dup

    def log_error(self, ts: int, source: str, code: str, detail: str):
        try:
            with self.lock:
                self.db.execute("INSERT INTO errors(ts,source,code,detail) VALUES(?,?,?,?)",
                                (ts, source, code, detail[:500]))
                self.db.commit()
        except sqlite3.Error:
            pass

    def raw_series(self, task_id: str, node_id: str, dns: str, url: str,
                   metric: str, t_from: int, t_to: int) -> list[dict]:
        """原始结果序列（ping: rtt_avg/loss_rate; curl: total_time）。"""
        col = {"rtt": "json_extract(metrics_json,'$.rtt_avg')",
               "loss": "json_extract(metrics_json,'$.loss_rate')",
               "total": "json_extract(metrics_json,'$.total_time')",
               "code": "json_extract(metrics_json,'$.http_code')"}.get(metric)
        with self.lock:
            if col:
                sql = (f"SELECT ts, status, error_class, resolved_ip, {col} AS v FROM probe_results "
                       "WHERE task_id=? AND node_id=? AND dns=? AND url=? AND ts BETWEEN ? AND ? "
                       "ORDER BY ts")
                rows = self.db.execute(sql, (task_id, node_id, dns, url, t_from, t_to)).fetchall()
                return [{"ts": r["ts"], "status": r["status"], "error_class": r["error_class"],
                         "resolved_ip": r["resolved_ip"], "v": r["v"]} for r in rows]
            rows = self.db.execute(
                "SELECT ts, status, error_class, metrics_json FROM probe_results "
                "WHERE task_id=? AND node_id=? AND dns=? AND url=? AND ts BETWEEN ? AND ? ORDER BY ts",
                (task_id, node_id, dns, url, t_from, t_to)).fetchall()
            return [{"ts": r["ts"], "status": r["status"], "error_class": r["error_class"],
                     "metrics": json.loads(r["metrics_json"])} for r in rows]

    def result_at(self, task_id: str, node_id: str, dns: str, url: str, ts: int):
        with self.lock:
            r = self.db.execute(
                "SELECT * FROM probe_results WHERE task_id=? AND node_id=? AND dns=? AND url=? AND ts=?",
                (task_id, node_id, dns, url, ts)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            return d

    def latest_per_stream(self, task_id: str, node_id: str, dns: str, url: str, n: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, status, error_class, metrics_json FROM probe_results "
                "WHERE task_id=? AND node_id=? AND dns=? AND url=? ORDER BY ts DESC LIMIT ?",
                (task_id, node_id, dns, url, n)).fetchall()
            return [{"ts": r["ts"], "status": r["status"], "error_class": r["error_class"],
                     "metrics": json.loads(r["metrics_json"])} for r in rows]

    def result_streams(self, task_id: str) -> list[dict]:
        """某任务当前存在的 (node,dns,url) 流，附最近一次解析 IP 与状态。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT node_id, dns, url FROM probe_results WHERE task_id=?",
                (task_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                last = self.db.execute(
                    "SELECT resolved_ip, status, error_class, error, ts FROM probe_results "
                    "WHERE task_id=? AND node_id=? AND dns=? AND url=? ORDER BY ts DESC LIMIT 1",
                    (task_id, r["node_id"], r["dns"] or "", r["url"] or "")).fetchone()
                d["latest_ip"] = last["resolved_ip"] if last else ""
                d["latest_status"] = last["status"] if last else None
                d["latest_error_class"] = last["error_class"] if last else ""
                d["latest_error"] = last["error"] if last else ""
                d["latest_ts"] = last["ts"] if last else 0
                out.append(d)
            return out

    # ---------- aggregates ----------
    def agg_recompute(self, bucket: str, b_from: int, b_to: int):
        """按桶区间重算聚合（幂等：先删后插）。bucket 已校验。"""
        step = BUCKET_SECONDS[bucket]
        with self.lock:
            self.db.execute("DELETE FROM aggregates WHERE bucket=? AND ts>=? AND ts<?",
                            (bucket, b_from, b_to))
            ins = []
            if bucket == "1m":
                sql = (
                    "SELECT ts/60*60 AS b, task_id, node_id, dns, url, status,"
                    " json_extract(metrics_json,'$.rtt_avg') AS rtt,"
                    " json_extract(metrics_json,'$.loss_rate') AS loss,"
                    " json_extract(metrics_json,'$.http_code') AS code"
                    " FROM probe_results WHERE ts>=? AND ts<? AND status!='skipped'")
                rows = self.db.execute(sql, (b_from, b_to)).fetchall()
                groups: dict = {}
                for r in rows:
                    key = (r["b"], r["task_id"], r["node_id"], r["dns"] or "", r["url"] or "")
                    groups.setdefault(key, []).append(r)
                for (b, tid, nid, dns, url), items in groups.items():
                    n = len(items)
                    oks = sum(1 for r in items if r["status"] == "ok")
                    rtts = sorted(float(r["rtt"]) for r in items if r["rtt"] is not None)
                    def pct(p):
                        if not rtts:
                            return None
                        k = max(0, min(len(rtts) - 1, int(round(p / 100 * (len(rtts) - 1)))))
                        return rtts[k]
                    losses = [float(r["loss"]) for r in items if r["loss"] is not None]
                    codes: dict = {}
                    for r in items:
                        c = r["code"]
                        if c:
                            codes[str(int(c))] = codes.get(str(int(c)), 0) + 1
                    ins.append((bucket, b, tid, nid, dns, url, n, oks, n - oks,
                                round(sum(rtts) / len(rtts), 2) if rtts else None,
                                pct(50), pct(95), rtts[-1] if rtts else None,
                                round(sum(losses) / len(losses), 4) if losses else None,
                                round(oks / n, 4), json.dumps(codes)))
            else:
                prev = {"5m": "1m", "1h": "5m", "1d": "1h"}[bucket]
                sql = ("SELECT ts/?*? AS b, task_id, node_id, dns, url, count, ok, rtt_avg,"
                       " rtt_p95, rtt_max, loss_rate, http_code_json FROM aggregates"
                       " WHERE bucket=? AND ts>=? AND ts<?")
                rows = self.db.execute(sql, (step, step, prev, b_from, b_to)).fetchall()
                groups: dict = {}
                for r in rows:
                    key = (r["b"], r["task_id"], r["node_id"], r["dns"] or "", r["url"] or "")
                    groups.setdefault(key, []).append(r)
                for (b, tid, nid, dns, url), items in groups.items():
                    count = sum(int(r["count"]) for r in items)
                    oks = sum(int(r["ok"]) for r in items)
                    rtts = [float(r["rtt_avg"]) for r in items if r["rtt_avg"] is not None]
                    weights = [max(1, int(r["count"])) for r in items if r["rtt_avg"] is not None]
                    wsum = sum(weights) or 1
                    p95s = [float(r["rtt_p95"]) for r in items if r["rtt_p95"] is not None]
                    maxs = [float(r["rtt_max"]) for r in items if r["rtt_max"] is not None]
                    losses = [float(r["loss_rate"]) for r in items if r["loss_rate"] is not None]
                    codes: dict = {}
                    for r in items:
                        for c, v in json.loads(r["http_code_json"] or "{}").items():
                            codes[c] = codes.get(c, 0) + v
                    ins.append((bucket, b, tid, nid, dns, url, count, oks, count - oks,
                                round(sum(a * w for a, w in zip(rtts, weights)) / wsum, 2) if rtts else None,
                                sorted(rtts)[len(rtts) // 2] if rtts else None,
                                max(p95s) if p95s else None,
                                max(maxs) if maxs else None,
                                round(sum(losses) / len(losses), 4) if losses else None,
                                round(oks / count, 4) if count else None,
                                json.dumps(codes)))
            self.db.executemany(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,count,ok,fail,"
                "rtt_avg,rtt_p50,rtt_p95,rtt_max,loss_rate,avail_rate,http_code_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ins)
            self.db.commit()

    def agg_read(self, bucket: str, task_id: str, node_id: str, dns: str, url: str,
                 t_from: int, t_to: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM aggregates WHERE bucket=? AND task_id=? AND node_id=? AND dns=? "
                "AND url=? AND ts>=? AND ts<=? ORDER BY ts",
                (bucket, task_id, node_id, dns, url, t_from, t_to)).fetchall()
            return [dict(r) for r in rows]

    def agg_buckets_existing(self, bucket: str, task_id: str, t_from: int, t_to: int) -> list[dict]:
        """任务级汇总（跨节点/线路聚合后的桶序列，供总览与对比）。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, SUM(count) AS count, SUM(ok) AS ok, SUM(fail) AS fail,"
                " AVG(rtt_avg) AS rtt_avg, MAX(rtt_p95) AS rtt_p95,"
                # 可用率按「成功/总数」重算（不用各流 avail_rate 的平均，避免流数不等时偏差）；
                # 丢包率只能按流平均（各流探测次数不同，属近似，与 p95 同一约定）
                " CASE WHEN SUM(count) > 0 THEN CAST(SUM(ok) AS REAL) / SUM(count)"
                "      ELSE NULL END AS avail_rate,"
                " AVG(loss_rate) AS loss_rate"
                " FROM aggregates WHERE bucket=? AND task_id=? AND ts>=? AND ts<=?"
                " GROUP BY ts ORDER BY ts", (bucket, task_id, t_from, t_to)).fetchall()
            return [dict(r) for r in rows]

    def node_avail(self, node_id: str, t_from: int,
                   task_ids: list[str] | None = None) -> dict:
        """节点可用率：跨该节点的全部线路/URL 流聚合（避免逐流读取的 N+1）。"""
        with self.lock:
            sql = ("SELECT SUM(count) AS c, SUM(ok) AS o FROM aggregates"
                   " WHERE bucket='1m' AND node_id=? AND ts>=?")
            args: list = [node_id, t_from]
            if task_ids is not None:
                if not task_ids:
                    return {"count": 0, "ok": 0, "avail": None}
                sql += f" AND task_id IN ({','.join('?' * len(task_ids))})"
                args += list(task_ids)
            r = self.db.execute(sql, args).fetchone()
            c, o = (r["c"] or 0), (r["o"] or 0)
            return {"count": c, "ok": o, "avail": round(o / c, 4) if c else None}

    # ---------- incidents ----------
    def incident_open(self, task_id: str, node_id: str, dns: str, url: str,
                      started_at: int, reason: dict) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO incidents(task_id,node_id,dns,url,started_at,reason_json)"
                " VALUES(?,?,?,?,?,?)",
                (task_id, node_id, dns, url, started_at, json.dumps(reason, ensure_ascii=False)))
            self.db.commit()
            return cur.lastrowid

    def incident_close(self, inc_id: int, ended_at: int):
        with self.lock:
            self.db.execute(
                "UPDATE incidents SET ended_at=?,"
                " duration_ms=MAX(0,(?-started_at))*1000 WHERE id=?",   # 时钟回拨时不产生负时长
                (ended_at, ended_at, inc_id))
            self.db.commit()

    def open_incident_for(self, task_id: str, node_id: str, dns: str, url: str):
        with self.lock:
            return self.db.execute(
                "SELECT * FROM incidents WHERE task_id=? AND node_id=? AND dns=? AND url=? "
                "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (task_id, node_id, dns, url)).fetchone()

    def list_incidents(self, limit: int = 30, open_only: bool = False) -> list[dict]:
        with self.lock:
            sql = "SELECT * FROM incidents"
            if open_only:
                sql += " WHERE ended_at IS NULL"
            sql += " ORDER BY started_at DESC LIMIT ?"
            rows = self.db.execute(sql, (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["reason"] = json.loads(d.pop("reason_json") or "{}")
                out.append(d)
            return out

    # ---------- 节点分组（组级任务分配）----------
    def list_groups(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM groups ORDER BY name").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["members"] = [x["node_id"] for x in self.db.execute(
                    "SELECT node_id FROM group_members WHERE group_id=?", (r["id"],)).fetchall()]
                out.append(d)
            return out

    def create_group(self, gid: str, name: str, note: str, ts: int) -> dict:
        with self.lock:
            try:
                self.db.execute("INSERT INTO groups(id,name,note,created_at) VALUES(?,?,?,?)",
                                (gid, name, note, ts))
            except sqlite3.IntegrityError:
                raise ValueError(f"分组名已存在: {name}")
            self.db.commit()
            return next(g for g in self.list_groups() if g["id"] == gid)

    def update_group(self, gid: str, fields: dict, ts: int) -> dict:
        with self.lock:
            cur = self.db.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
            if not cur:
                raise KeyError(gid)
            if fields.get("name") and fields["name"] != cur["name"]:
                dup = self.db.execute("SELECT id FROM groups WHERE name=? AND id<>?",
                                      (fields["name"], gid)).fetchone()
                if dup:
                    raise ValueError("分组名已存在")
            sets = [f"{k}=?" for k in ("name", "note") if k in fields]
            if sets:
                self.db.execute(f"UPDATE groups SET {', '.join(sets)} WHERE id=?",
                                [fields[k] for k in ("name", "note") if k in fields] + [gid])
                self.db.commit()
            return next(g for g in self.list_groups() if g["id"] == gid)

    def delete_group(self, gid: str, ts: int) -> dict:
        """删组：清成员，并从任务分配里摘掉 g:<id> / g:<名称>（config_version 递增）。"""
        with self.lock:
            cur = self.db.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
            if not cur:
                raise KeyError(gid)
            keys = {f"g:{gid}", f"g:{cur['name']}"}
            self.db.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
            self.db.execute("DELETE FROM groups WHERE id=?", (gid,))
            for t in self.db.execute("SELECT id, nodes_json FROM tasks").fetchall():
                lst = json.loads(t["nodes_json"] or "[]")
                new = [x for x in lst if x not in keys]
                if new != lst:
                    self.db.execute(
                        "UPDATE tasks SET nodes_json=?, config_version=config_version+1,"
                        " updated_at=? WHERE id=?", (json.dumps(new), ts, t["id"]))
            self._bump_config_version()
            self.db.commit()
            return dict(cur)

    def set_group_members(self, gid: str, node_ids: list[str], ts: int) -> dict:
        with self.lock:
            if not self.db.execute("SELECT id FROM groups WHERE id=?", (gid,)).fetchone():
                raise KeyError(gid)
            self.db.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
            for nid in node_ids:
                if self.db.execute("SELECT id FROM nodes WHERE id=?", (nid,)).fetchone():
                    self.db.execute("INSERT OR IGNORE INTO group_members(group_id,node_id)"
                                    " VALUES(?,?)", (gid, nid))
            self.db.commit()
            return next(g for g in self.list_groups() if g["id"] == gid)

    def group_ids_for_node(self, node_id: str) -> list[str]:
        with self.lock:
            return [r["group_id"] for r in self.db.execute(
                "SELECT group_id FROM group_members WHERE node_id=?", (node_id,)).fetchall()]

    def tasks_for_node(self, node_id: str, node_name: str) -> list[dict]:
        """该节点应执行的任务：nodes 为空=全部节点；否则匹配 节点id / 节点名 / 组（g:组id 或 g:组名）。"""
        groups = self.list_groups()
        gids = {g["id"] for g in groups if node_id in g["members"]}
        gkeys = {f"g:{g['id']}" for g in groups if g["id"] in gids} | \
                {f"g:{g['name']}" for g in groups if g["id"] in gids}
        out = []
        for t in self.enabled_tasks():
            sel = t.get("nodes") or []
            if not sel or node_id in sel or node_name in sel or any(x in gkeys for x in sel):
                out.append(t)
        return out

    # ---------- 注册 Token 管理 ----------
    def list_tokens(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,name,note,enabled,created_at,last_used_at,revoked_at FROM tokens"
                " ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def create_token(self, tid: str, name: str, token_hash: str, note: str, ts: int) -> dict:
        with self.lock:
            try:
                self.db.execute("INSERT INTO tokens(id,name,token_hash,note,created_at)"
                                " VALUES(?,?,?,?,?)", (tid, name, token_hash, note, ts))
            except sqlite3.IntegrityError:
                raise ValueError(f"Token 名已存在: {name}")
            self.db.commit()
            return next(t for t in self.list_tokens() if t["id"] == tid)

    def update_token(self, tid: str, fields: dict, ts: int) -> dict:
        with self.lock:
            if not self.db.execute("SELECT id FROM tokens WHERE id=?", (tid,)).fetchone():
                raise KeyError(tid)
            sets, vals = [], []
            if "name" in fields:
                sets.append("name=?"); vals.append(fields["name"])
            if "note" in fields:
                sets.append("note=?"); vals.append(fields["note"])
            if "enabled" in fields:
                sets.append("enabled=?"); vals.append(1 if fields["enabled"] else 0)
                sets.append("revoked_at=?"); vals.append(0 if fields["enabled"] else ts)
            if sets:
                self.db.execute(f"UPDATE tokens SET {', '.join(sets)} WHERE id=?", vals + [tid])
                self.db.commit()
            return next(t for t in self.list_tokens() if t["id"] == tid)

    def delete_token(self, tid: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM tokens WHERE id=?", (tid,))
            self.db.commit()
            return cur.rowcount > 0

    def token_by_hash(self, token_hash: str):
        with self.lock:
            return self.db.execute(
                "SELECT * FROM tokens WHERE token_hash=? AND enabled=1 AND COALESCE(revoked_at,0)=0",
                (token_hash,)).fetchone()

    def token_active(self, tid: str) -> bool:
        """节点绑定的 Token 是否仍有效（吊销/停用后该节点的同步会被拒）。"""
        if not tid:
            return True
        with self.lock:
            r = self.db.execute("SELECT enabled, revoked_at FROM tokens WHERE id=?", (tid,)).fetchone()
            return bool(r and r["enabled"] and not r["revoked_at"])

    def token_touch(self, tid: str, ts: int):
        with self.lock:
            self.db.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (ts, tid))
            self.db.commit()

    # ---------- 节点侧事件（离线/恢复）----------
    def node_incident_open(self, node_id: str, ts: int, reason: dict) -> int:
        with self.lock:
            opened = self.db.execute(
                "SELECT id FROM incidents WHERE kind='node' AND node_id=? AND ended_at IS NULL",
                (node_id,)).fetchone()
            if opened:
                return opened["id"]
            cur = self.db.execute(
                "INSERT INTO incidents(task_id,node_id,dns,url,started_at,kind,reason_json)"
                " VALUES('',?,'','',?,'node',?)",
                (node_id, ts, json.dumps(reason, ensure_ascii=False)))
            self.db.commit()
            return cur.lastrowid

    def node_incident_close(self, node_id: str, ts: int) -> int:
        with self.lock:
            cur = self.db.execute(
                "UPDATE incidents SET ended_at=?, duration_ms=MAX(0,(?-started_at))*1000"
                " WHERE kind='node' AND node_id=? AND ended_at IS NULL", (ts, ts, node_id))
            self.db.commit()
            return cur.rowcount

    # ---------- 节点资源时序（心跳表）----------
    def node_metrics(self, node_id: str, t_from: int, t_to: int, bucket: int = 300) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT (ts/?)*? AS ts, AVG(cpu) cpu, MAX(cpu) cpu_max, AVG(mem) mem,"
                " MAX(mem) mem_max, COUNT(*) n FROM node_heartbeats"
                " WHERE node_id=? AND ts>=? AND ts<=? AND cpu IS NOT NULL"
                " GROUP BY ts/? ORDER BY ts", (bucket, bucket, node_id, t_from, t_to, bucket)
            ).fetchall()
            return [{**dict(r), "cpu": round(r["cpu"], 1) if r["cpu"] is not None else None,
                     "mem": round(r["mem"], 1) if r["mem"] is not None else None} for r in rows]

    # ---------- GeoIP 缓存 ----------
    def geo_cache_get(self, ip: str, ttl: int, ts: int):
        with self.lock:
            r = self.db.execute("SELECT * FROM geo_cache WHERE ip=?", (ip,)).fetchone()
            if r and ts - r["ts"] <= ttl:
                return json.loads(r["data_json"] or "{}")
            return None

    def geo_cache_put(self, ip: str, data: dict, ts: int):
        with self.lock:
            self.db.execute("INSERT INTO geo_cache(ip,data_json,ts) VALUES(?,?,?)"
                            " ON CONFLICT(ip) DO UPDATE SET data_json=excluded.data_json,"
                            " ts=excluded.ts", (ip, json.dumps(data, ensure_ascii=False), ts))
            self.db.commit()


    # ---------- 自定义「IP 段 → 位置」----------
    def list_geo_networks(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM geo_networks ORDER BY cidr").fetchall()
            return [dict(r) for r in rows]

    def add_geo_network(self, gid: str, cidr: str, place: str, lat: float, lng: float,
                        note: str, ts: int) -> dict:
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO geo_networks(id,cidr,place,lat,lng,note,created_at)"
                    " VALUES(?,?,?,?,?,?,?)", (gid, cidr, place, lat, lng, note, ts))
            except sqlite3.IntegrityError:
                raise ValueError(f"该网段已存在: {cidr}")
            self.db.commit()
            return next(g for g in self.list_geo_networks() if g["id"] == gid)

    def delete_geo_network(self, gid: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM geo_networks WHERE id=?", (gid,))
            self.db.commit()
            return cur.rowcount > 0

    def match_geo_network(self, ip: str) -> dict | None:
        """按最长前缀匹配：10.10.10.5 命中 10.10.10.0/24 优先于 10.0.0.0/8。"""
        if not ip:
            return None
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        best, best_len = None, -1
        for g in self.list_geo_networks():
            try:
                net = ipaddress.ip_network(g["cidr"], strict=False)
            except ValueError:
                continue
            if addr in net and net.prefixlen > best_len:
                best, best_len = g, net.prefixlen
        return best

    # ---------- 保留策略 ----------
    def retention(self, raw_days: int, a1m: int, a5m: int, a1h: int, hb_days: int, ts: int):
        with self.lock:
            day = 86400
            self.db.execute("DELETE FROM probe_results WHERE ts < ?", (ts - raw_days * day,))
            self.db.execute("DELETE FROM aggregates WHERE bucket='1m' AND ts < ?", (ts - a1m * day,))
            self.db.execute("DELETE FROM aggregates WHERE bucket='5m' AND ts < ?", (ts - a5m * day,))
            self.db.execute("DELETE FROM aggregates WHERE bucket='1h' AND ts < ?", (ts - a1h * day,))
            self.db.execute("DELETE FROM node_heartbeats WHERE ts < ?", (ts - hb_days * day,))
            self.db.commit()

    def stats_counts(self) -> dict:
        with self.lock:
            return {
                "results": self.db.execute("SELECT COUNT(*) c FROM probe_results").fetchone()["c"],
                "nodes": self.db.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"],
                "tasks": self.db.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
                "incidents_open": self.db.execute(
                    "SELECT COUNT(*) c FROM incidents WHERE ended_at IS NULL").fetchone()["c"],
            }


def secrets4() -> str:
    import secrets
    return secrets.token_hex(2)


def _task_row(r) -> dict:
    d = dict(r)
    d["urls"] = json.loads(d.pop("urls_json") or "[]")
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["dns"] = json.loads(d.pop("dns_json") or "[]")
    d["nodes"] = json.loads(d.pop("nodes_json") or "[]")
    return d
