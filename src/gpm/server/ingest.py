"""结果接入：校验、去重（主键冲突）、时钟偏差、事件状态机、聚合触发标记。"""
from __future__ import annotations

from ..common.models import ResultsIn
from ..common.util import now
from .incidents import IncidentMachine


class Ingest:
    def __init__(self, storage, machine: IncidentMachine, cfg_server: dict):
        self.s = storage
        self.m = machine
        self.late_window = cfg_server.get("late_window_seconds", 300)
        self.clock_skew = cfg_server.get("clock_skew_seconds", 120)
        self.batch_max = cfg_server.get("ingest_batch_max", 500)
        self.dirty_since = 0  # 聚合 sweep 参考：最新结果 ts

    def accept(self, data: ResultsIn) -> dict:
        truncated = 0
        if len(data.results) > self.batch_max:
            truncated = len(data.results) - self.batch_max
            data.results = data.results[:self.batch_max]
        ts_now = now()
        rows, rejected = [], []
        for r in data.results:
            skew = abs(r.ts - ts_now)
            if skew > self.clock_skew:
                self.s.log_error(ts_now, "ingest", "clock_skew",
                                 f"task={r.task_id} node={data.node_id} 偏差{skew}s，仅入原始表")
            rows.append({"ts": r.ts, "task_id": r.task_id, "node_id": data.node_id,
                         "type": r.type, "dns": r.dns or "", "url": r.url or "",
                         "status": r.status, "error_class": r.error_class, "error": r.error[:200],
                         "dns_server": r.dns_server or "", "resolved_ip": r.resolved_ip or "",
                         "dns_time_ms": r.dns_time_ms, "metrics": r.metrics,
                         "config_version": r.config_version})
        inserted, dups = self.s.insert_results(rows, ts_now)
        # 事件判定（跳过 skipped 与被去重的行）
        seen = set()
        for row, r in zip(rows, data.results):
            k = (row["task_id"], row["node_id"], row["dns"], row["url"], row["ts"])
            if r.status == "skipped":
                continue
            if k in seen:
                continue
            seen.add(k)
            self.m.on_result(row["task_id"], data.node_id, row["dns"], row["url"],
                             r.status, r.ts, r.error_class)
        if rows:
            self.dirty_since = max(self.dirty_since, max(r["ts"] for r in rows))
        return {"accepted": inserted, "duplicates": dups,
                "truncated": truncated, "rejected": rejected, "server_time": ts_now}
