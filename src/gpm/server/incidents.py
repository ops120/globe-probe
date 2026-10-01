"""通断事件状态机（粒度 task×node×dns×url）。"""
from __future__ import annotations

from ..common.util import now


class IncidentMachine:
    def __init__(self, storage, fail_threshold: int = 3, recover_threshold: int = 2):
        self.s = storage
        self.fail_threshold = fail_threshold
        self.recover_threshold = recover_threshold
        # key -> {"fail_streak":int,"ok_streak":int,"incident_id":int|None}
        self.state: dict[tuple, dict] = {}

    @staticmethod
    def key(task_id: str, node_id: str, dns: str, url: str) -> tuple:
        return (task_id, node_id, dns or "", url or "")

    def rebuild_stream(self, task_id: str, node_id: str, dns: str, url: str):
        """服务重启后从最近结果重建状态（重放 2×threshold 条）。"""
        k = self.key(task_id, node_id, dns, url)
        recent = self.s.latest_per_stream(task_id, node_id, dns, url,
                                          self.fail_threshold + self.recover_threshold)
        st = {"fail_streak": 0, "ok_streak": 0, "incident_id": None}
        for r in reversed(recent):  # 时间正序重放
            self._apply(k, r["status"], r["ts"], r["error_class"], rebuild=True)
        self.state[k] = st

    def _apply(self, k: tuple, status: str, ts: int, error_class: str, rebuild: bool = False):
        st = self.state.setdefault(k, {"fail_streak": 0, "ok_streak": 0, "incident_id": None})
        if status == "skipped":
            return  # 工具缺失等不参与判定
        if status == "fail":
            st["fail_streak"] += 1
            st["ok_streak"] = 0
            if st["fail_streak"] >= self.fail_threshold and st["incident_id"] is None:
                open_inc = self.s.open_incident_for(*k)
                if open_inc:  # 重启前事件未关闭
                    st["incident_id"] = open_inc["id"]
                else:
                    reason = {"error_class": error_class, "fail_streak": st["fail_streak"]}
                    st["incident_id"] = self.s.incident_open(*k, ts, reason)
        elif status == "ok":
            st["ok_streak"] += 1
            st["fail_streak"] = 0
            if st["ok_streak"] >= self.recover_threshold and st["incident_id"] is not None:
                self.s.incident_close(st["incident_id"], ts)
                st["incident_id"] = None

    def on_result(self, task_id: str, node_id: str, dns: str, url: str,
                  status: str, ts: int, error_class: str):
        self._apply(self.key(task_id, node_id, dns, url), status, ts, error_class)

    def counts(self) -> dict:
        open_n = sum(1 for st in self.state.values() if st["incident_id"])
        return {"tracked_streams": len(self.state), "open": open_n}
