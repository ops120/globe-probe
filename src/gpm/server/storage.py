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

-- 告警通知渠道（webhook / 企业微信 / 钉钉 / 飞书 / SMTP）
CREATE TABLE IF NOT EXISTS notify_channels(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, type TEXT, config_json TEXT DEFAULT '{}',
  enabled INTEGER DEFAULT 1, created_at INTEGER,
  last_ok_at INTEGER DEFAULT 0, last_error TEXT DEFAULT '');

-- 告警规则（可用率/延迟/丢包/节点离线；支持静默期与维护窗口豁免）
CREATE TABLE IF NOT EXISTS alert_rules(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, metric TEXT, op TEXT, threshold REAL,
  window_seconds INTEGER DEFAULT 300, task_id TEXT DEFAULT '', node_id TEXT DEFAULT '',
  group_id TEXT DEFAULT '', severity TEXT DEFAULT 'warning',
  channel_ids_json TEXT DEFAULT '[]', silence_seconds INTEGER DEFAULT 1800,
  enabled INTEGER DEFAULT 1, created_at INTEGER);

-- 维护窗口：窗口内不评估规则，也不计入可用率（SLA 侧按事件剔除）
CREATE TABLE IF NOT EXISTS maintenance_windows(
  id TEXT PRIMARY KEY, name TEXT, starts_at INTEGER, ends_at INTEGER,
  task_id TEXT DEFAULT '', node_id TEXT DEFAULT '', note TEXT DEFAULT '', created_at INTEGER);

-- 告警历史（firing / resolved）
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, rule_id TEXT, rule_name TEXT,
  metric TEXT, key TEXT, status TEXT, severity TEXT, title TEXT, text TEXT,
  target_json TEXT DEFAULT '{}', delivered INTEGER DEFAULT 0,
  n_channels INTEGER DEFAULT 0, n_ok INTEGER DEFAULT 0, detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts(rule_id, key, status, ts);

-- 键值设置（巡检推送配置、UI 偏好等服务端可持久化项）
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);

-- 操作审计：所有写接口都会留痕（谁 / 何时 / 改了什么 / 结果）
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, who TEXT, action TEXT, target TEXT,
  target_id TEXT DEFAULT '', status INTEGER DEFAULT 0, ip TEXT DEFAULT '', detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

-- 通知重投队列：派发失败的渠道进这里，按退避重试
CREATE TABLE IF NOT EXISTS notify_outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, alert_id INTEGER DEFAULT 0,
  channel_id TEXT, title TEXT, text TEXT, attempts INTEGER DEFAULT 0,
  next_retry_at INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
  last_error TEXT DEFAULT '', done_at INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON notify_outbox(status, next_retry_at);

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
            for col, ddl in (("acked_at", "INTEGER DEFAULT 0"),
                             ("acked_by", "TEXT DEFAULT ''"),
                             ("note", "TEXT DEFAULT ''"),
                             ("reopen_count", "INTEGER DEFAULT 0")):
                if col not in icols:
                    self.db.execute(f"ALTER TABLE incidents ADD COLUMN {col} {ddl}")
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
                   task_ids: list[str] | None = None, t_to: int = 0) -> dict:
        """节点可用率：跨该节点的全部线路/URL 流聚合（避免逐流读取的 N+1）。

        t_to>0 时限定窗口上界（历史报表必须传，否则统计区间会一直延伸到 now）。
        """
        with self.lock:
            sql = ("SELECT SUM(count) AS c, SUM(ok) AS o FROM aggregates"
                   " WHERE bucket='1m' AND node_id=? AND ts>=?")
            args: list = [node_id, t_from]
            if t_to:
                sql += " AND ts<=?"
                args.append(t_to)
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

    def last_closed_incident(self, task_id: str, node_id: str, dns: str, url: str) -> dict | None:
        """同一流最近一次已关闭的事件（用于「抖动合并」）。"""
        with self.lock:
            r = self.db.execute(
                "SELECT * FROM incidents WHERE task_id=? AND node_id=? AND dns=? AND url=?"
                " AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
                (task_id, node_id, dns or "", url or "")).fetchone()
            return dict(r) if r else None

    def incident_reopen(self, iid: int, ts: int, reason: dict) -> bool:
        """抖动合并：把刚关闭的同一流事件重新打开（不新增一行，reopen_count+1）。

        对应业界「有界合并窗口」做法：短时间内同目标反复失败仍属同一事件，
        避免一次抖动被拆成十几条事件（也就不需要在 UI 里假装它们不是一回事）。
        """
        with self.lock:
            cur = self.db.execute(
                "UPDATE incidents SET ended_at=NULL, reopen_count=reopen_count+1, reason_json=?"
                " WHERE id=? AND ended_at IS NOT NULL",
                (json.dumps(reason, ensure_ascii=False), iid))
            self.db.commit()
            return cur.rowcount > 0

    def list_incidents(self, limit: int = 30, open_only: bool = False,
                       t_from: int = 0, t_to: int = 0) -> list[dict]:
        """事件列表。t_from/t_to 非 0 时按「与窗口有交集」过滤（在 SQL 里做，
        避免只扫最近 N 条再内存裁剪导致超长窗口漏掉最旧事件）。"""
        with self.lock:
            sql = "SELECT * FROM incidents"
            where, args = [], []
            if open_only:
                where.append("ended_at IS NULL")
            if t_from:
                where.append("(ended_at IS NULL OR ended_at >= ?)")
                args.append(t_from)
            if t_to:
                where.append("started_at <= ?")
                args.append(t_to)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY started_at DESC LIMIT ?"
            args.append(limit)
            rows = self.db.execute(sql, args).fetchall()
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

    # ---------- 告警渠道 ----------
    def list_channels(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM notify_channels ORDER BY created_at").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["config"] = json.loads(d.pop("config_json") or "{}")
                out.append(d)
            return out

    def create_channel(self, cid: str, name: str, ctype: str, config: dict, ts: int) -> dict:
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO notify_channels(id,name,type,config_json,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (cid, name, ctype, json.dumps(config, ensure_ascii=False), ts))
            except sqlite3.IntegrityError:
                raise ValueError(f"渠道名已存在: {name}")
            self.db.commit()
            return next(c for c in self.list_channels() if c["id"] == cid)

    def update_channel(self, cid: str, fields: dict, ts: int) -> dict:
        with self.lock:
            if not self.db.execute("SELECT id FROM notify_channels WHERE id=?", (cid,)).fetchone():
                raise KeyError(cid)
            sets, vals = [], []
            for k, col in (("name", "name"), ("type", "type")):
                if k in fields:
                    sets.append(f"{col}=?"); vals.append(fields[k])
            if "config" in fields:
                sets.append("config_json=?")
                vals.append(json.dumps(fields["config"], ensure_ascii=False))
            if "enabled" in fields:
                sets.append("enabled=?"); vals.append(1 if fields["enabled"] else 0)
            if sets:
                self.db.execute(f"UPDATE notify_channels SET {', '.join(sets)} WHERE id=?",
                                vals + [cid])
                self.db.commit()
            return next(c for c in self.list_channels() if c["id"] == cid)

    def delete_channel(self, cid: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM notify_channels WHERE id=?", (cid,))
            self.db.commit()
            return cur.rowcount > 0

    def channel_touch(self, cid: str, ok: bool, err: str, ts: int):
        with self.lock:
            if ok:
                self.db.execute("UPDATE notify_channels SET last_ok_at=?, last_error='' WHERE id=?",
                                (ts, cid))
            else:
                self.db.execute("UPDATE notify_channels SET last_error=? WHERE id=?", (err[:200], cid))
            self.db.commit()

    # ---------- 告警规则 ----------
    def list_rules(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM alert_rules ORDER BY created_at").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["channel_ids"] = json.loads(d.pop("channel_ids_json") or "[]")
                out.append(d)
            return out

    def create_rule(self, rid: str, fields: dict, ts: int) -> dict:
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO alert_rules(id,name,metric,op,threshold,window_seconds,task_id,"
                    "node_id,group_id,severity,channel_ids_json,silence_seconds,enabled,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, fields["name"], fields["metric"], fields["op"], float(fields["threshold"]),
                     int(fields.get("window_seconds") or 300), fields.get("task_id") or "",
                     fields.get("node_id") or "", fields.get("group_id") or "",
                     fields.get("severity") or "warning",
                     json.dumps(fields.get("channel_ids") or []),
                     int(fields.get("silence_seconds") or 1800),
                     1 if fields.get("enabled", True) else 0, ts))
            except sqlite3.IntegrityError:
                raise ValueError(f"规则名已存在: {fields.get('name')}")
            self.db.commit()
            return next(r for r in self.list_rules() if r["id"] == rid)

    def update_rule(self, rid: str, fields: dict, ts: int) -> dict:
        with self.lock:
            if not self.db.execute("SELECT id FROM alert_rules WHERE id=?", (rid,)).fetchone():
                raise KeyError(rid)
            cols = {"name": "name", "metric": "metric", "op": "op", "threshold": "threshold",
                    "window_seconds": "window_seconds", "task_id": "task_id", "node_id": "node_id",
                    "group_id": "group_id", "severity": "severity",
                    "silence_seconds": "silence_seconds"}
            sets, vals = [], []
            for k, col in cols.items():
                if k in fields and fields[k] is not None:
                    sets.append(f"{col}=?"); vals.append(fields[k])
            if "channel_ids" in fields:
                sets.append("channel_ids_json=?")
                vals.append(json.dumps(fields["channel_ids"] or []))
            if "enabled" in fields:
                sets.append("enabled=?"); vals.append(1 if fields["enabled"] else 0)
            if sets:
                self.db.execute(f"UPDATE alert_rules SET {', '.join(sets)} WHERE id=?", vals + [rid])
                self.db.commit()
            return next(r for r in self.list_rules() if r["id"] == rid)

    def delete_rule(self, rid: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
            self.db.commit()
            return cur.rowcount > 0

    # ---------- 维护窗口 ----------
    def list_windows(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM maintenance_windows ORDER BY starts_at DESC").fetchall()]

    def create_window(self, wid: str, fields: dict, ts: int) -> dict:
        with self.lock:
            self.db.execute(
                "INSERT INTO maintenance_windows(id,name,starts_at,ends_at,task_id,node_id,note,"
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (wid, fields.get("name") or "维护窗口", int(fields["starts_at"]),
                 int(fields["ends_at"]), fields.get("task_id") or "", fields.get("node_id") or "",
                 fields.get("note") or "", ts))
            self.db.commit()
            return next(w for w in self.list_windows() if w["id"] == wid)

    def delete_window(self, wid: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM maintenance_windows WHERE id=?", (wid,))
            self.db.commit()
            return cur.rowcount > 0

    def in_maintenance(self, ts: int, task_id: str = "", node_id: str = "") -> dict | None:
        """命中维护窗口则返回该窗口（未指定 task/node 的窗口对全部生效）。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM maintenance_windows WHERE starts_at<=? AND ends_at>=?",
                (ts, ts)).fetchall()
            for w in rows:
                if w["task_id"] and task_id and w["task_id"] != task_id:
                    continue
                if w["node_id"] and node_id and w["node_id"] != node_id:
                    continue
                if w["task_id"] and not task_id:
                    continue
                if w["node_id"] and not node_id:
                    continue
                return dict(w)
            return None

    # ---------- 告警历史 ----------
    def alert_add(self, ts: int, rule: dict, key: str, status: str, title: str, text: str,
                  target: dict, delivered: bool, n_channels: int, n_ok: int,
                  detail: str = "") -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO alerts(ts,rule_id,rule_name,metric,key,status,severity,title,text,"
                "target_json,delivered,n_channels,n_ok,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, rule["id"], rule["name"], rule["metric"], key, status,
                 rule.get("severity") or "warning", title, text,
                 json.dumps(target, ensure_ascii=False), 1 if delivered else 0,
                 n_channels, n_ok, detail[:300]))
            self.db.commit()
            return cur.lastrowid

    def alert_last(self, rule_id: str, key: str) -> dict | None:
        with self.lock:
            r = self.db.execute(
                "SELECT * FROM alerts WHERE rule_id=? AND key=? ORDER BY ts DESC LIMIT 1",
                (rule_id, key)).fetchone()
            return dict(r) if r else None

    def alert_open(self, rule_id: str, key: str) -> dict | None:
        """该规则+目标当前是否处于未恢复状态。

        注意：不能直接查 status='firing' —— 恢复是**追加**一条 resolved 记录，
        旧的 firing 行仍在表里；必须看「最新一条」的状态，否则恢复后会一直认为未恢复，
        导致每个评估周期重复推送「已恢复」。
        """
        with self.lock:
            r = self.db.execute(
                "SELECT * FROM alerts WHERE rule_id=? AND key=?"
                " ORDER BY ts DESC, id DESC LIMIT 1", (rule_id, key)).fetchone()
            return dict(r) if r and r["status"] == "firing" else None

    def alert_recent(self, limit: int = 50, status: str = "") -> list[dict]:
        with self.lock:
            sql = "SELECT * FROM alerts"
            args: list = []
            if status:
                sql += " WHERE status=?"
                args.append(status)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(limit)
            out = []
            for r in self.db.execute(sql, args).fetchall():
                d = dict(r)
                d["target"] = json.loads(d.pop("target_json") or "{}")
                out.append(d)
            return out

    def alert_counts(self) -> dict:
        """firing = 当前仍未恢复的（规则+目标）组合数；total = 历史记录条数。"""
        with self.lock:
            firing = self.db.execute(
                "SELECT COUNT(*) c FROM alerts a WHERE a.id = ("
                "  SELECT a2.id FROM alerts a2 WHERE a2.rule_id=a.rule_id AND a2.key=a.key"
                "  ORDER BY a2.ts DESC, a2.id DESC LIMIT 1) AND a.status='firing'"
            ).fetchone()["c"]
            total = self.db.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            return {"firing": firing, "total": total}

    # ---------- 设置（键值）----------
    def setting_get(self, key: str, default: str = "") -> str:
        with self.lock:
            r = self.db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
            return r["v"] if r else default

    def setting_set(self, key: str, value: str):
        with self.lock:
            self.db.execute("INSERT INTO settings(k,v) VALUES(?,?)"
                            " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
            self.db.commit()

    def settings_all(self) -> dict:
        with self.lock:
            return {r["k"]: r["v"] for r in self.db.execute("SELECT k,v FROM settings")}

    # ---------- 操作审计 ----------
    def audit_add(self, ts: int, who: str, action: str, target: str, target_id: str = "",
                  status: int = 0, ip: str = "", detail: str = "") -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO audit_log(ts,who,action,target,target_id,status,ip,detail)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (ts, who, action, target, target_id, status, ip, detail[:400]))
            self.db.commit()
            return cur.lastrowid

    def audit_list(self, limit: int = 100, action: str = "", target: str = "",
                   since: int = 0) -> list[dict]:
        with self.lock:
            sql = "SELECT * FROM audit_log"
            where, args = [], []
            if action:
                where.append("action=?")
                args.append(action)
            if target:
                where.append("target LIKE ?")
                args.append(target + "%")
            if since:
                where.append("ts>=?")
                args.append(since)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY ts DESC, id DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def audit_counts(self) -> dict:
        with self.lock:
            return {
                "total": self.db.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"],
                "day": self.db.execute("SELECT COUNT(*) c FROM audit_log WHERE ts>=?",
                                       (now() - 86400,)).fetchone()["c"],
            }

    # ---------- 通知重投队列 ----------
    def outbox_add(self, ts: int, alert_id: int, channel_id: str, title: str, text: str,
                   next_retry_at: int = 0, err: str = "") -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO notify_outbox(ts,alert_id,channel_id,title,text,next_retry_at,last_error)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts, alert_id, channel_id, title, text, next_retry_at, err[:200]))
            self.db.commit()
            return cur.lastrowid

    def outbox_get(self, oid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM notify_outbox WHERE id=?", (oid,)).fetchone()
            return dict(r) if r else None

    def outbox_due(self, ts: int, limit: int = 20) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM notify_outbox WHERE status='pending' AND next_retry_at<=?"
                " ORDER BY next_retry_at LIMIT ?", (ts, limit)).fetchall()]

    def outbox_mark(self, oid: int, status: str, err: str = "", next_retry_at: int = 0):
        with self.lock:
            if status == "pending":
                self.db.execute(
                    "UPDATE notify_outbox SET attempts=attempts+1, next_retry_at=?,"
                    " last_error=? WHERE id=?", (next_retry_at, err[:200], oid))
            else:
                self.db.execute(
                    "UPDATE notify_outbox SET status=?, last_error=?, done_at=?,"
                    " attempts=attempts+1 WHERE id=?", (status, err[:200], now(), oid))
            self.db.commit()

    def outbox_list(self, limit: int = 50, status: str = "") -> list[dict]:
        with self.lock:
            sql = "SELECT * FROM notify_outbox"
            args: list = []
            if status:
                sql += " WHERE status=?"
                args.append(status)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def outbox_counts(self) -> dict:
        with self.lock:
            rows = self.db.execute(
                "SELECT status, COUNT(*) c FROM notify_outbox GROUP BY status").fetchall()
            out = {"pending": 0, "done": 0, "failed": 0}
            for r in rows:
                out[r["status"]] = r["c"]
            return out

    def outbox_delete(self, oid: int) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM notify_outbox WHERE id=?", (oid,))
            self.db.commit()
            return cur.rowcount > 0

    # ---------- 事件确认 / 备注 ----------
    def incident_get(self, iid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["reason"] = json.loads(d.pop("reason_json") or "{}")
            return d

    def incident_ack(self, iid: int, ts: int, who: str, note: str = "") -> bool:
        with self.lock:
            cur = self.db.execute(
                "UPDATE incidents SET acked_at=?, acked_by=?,"
                " note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
                (ts, who, note, note, iid))
            self.db.commit()
            return cur.rowcount > 0

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
