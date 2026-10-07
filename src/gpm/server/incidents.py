"""通断事件状态机（粒度 task×node×dns×url）。"""
from __future__ import annotations

import threading


class IncidentMachine:
    def __init__(self, storage, fail_threshold: int = 3, recover_threshold: int = 2,
                 flap_window: int = 600, flap_max: int = 6 * 3600):
        self.s = storage
        self.fail_threshold = fail_threshold
        self.recover_threshold = recover_threshold
        # 抖动合并（业界「有界合并窗口」）：关闭后 flap_window 秒内再次失败，
        # 且该事件自首次开始不超过 flap_max 秒 → 视为同一次事件，重新打开而不是新建
        self.flap_window = max(0, int(flap_window))
        self.flap_max = max(60, int(flap_max))
        # key -> {"fail_streak":int,"ok_streak":int,"incident_id":int|None,"no_open":bool}
        # no_open：已向库里确认过「该流当前没有未恢复事件」，避免每条 ok 结果都回查一次库
        self.state: dict[tuple, dict] = {}
        # /api/agent/results 是线程池里的 sync 端点，同一流的两批结果可并发到达
        # （agent 超时重试即可触发）。_apply 是跨多次持锁事务的 check-then-act
        # （查库确认无事件 → 开单），无锁时会丢失连续计数、同一流双开事件——
        # 恢复时只关内存里记的那条，另一条变成僵尸事件。单机内存锁足够串行化。
        self._lock = threading.Lock()

    @staticmethod
    def key(task_id: str, node_id: str, dns: str, url: str) -> tuple:
        return (task_id, node_id, dns or "", url or "")

    @staticmethod
    def _new_state() -> dict:
        return {"fail_streak": 0, "ok_streak": 0, "incident_id": None, "no_open": False}

    def rebuild_stream(self, task_id: str, node_id: str, dns: str, url: str):
        """服务重启后从最近结果重建内存状态（**只重建状态，不写库**）。

        历史缺陷（.docs/ONCALL_OPTIMIZATION_2.md 根因 1.1）：原实现把重放结果累积在
        局部字典 st 里，随后用 self.state[k] = st 把刚重放出来的状态覆盖成空 ——
        等于没重建；而且该函数从未被任何地方调用。现在把 state 显式交给 _apply 累积，
        并由 rebuild_all() 在服务启动时接上。
        """
        k = self.key(task_id, node_id, dns, url)
        recent = self.s.latest_per_stream(task_id, node_id, dns or "", url or "",
                                          self.fail_threshold + self.recover_threshold)
        st = self._new_state()
        for r in reversed(recent):          # 时间正序重放
            self._apply(k, r["status"], r["ts"], r["error_class"], rebuild=True, st=st)
        self.state[k] = st
        return st

    def rebuild_all(self) -> int:
        """启动时重建所有「库里仍有未恢复事件」的流的状态，返回重建条数。

        没有这一步，重启后内存里 incident_id 全是 None，而恢复收口原先只看内存，
        于是「重启后恢复」的流会留下永远关不掉的僵尸事件。
        """
        n = 0
        for row in self.s.streams_with_open_incidents():
            try:
                self.rebuild_stream(row["task_id"], row["node_id"], row["dns"], row["url"])
                n += 1
            except Exception:
                continue
        return n

    def _close_if_open(self, k: tuple, st: dict, ts: int, rebuild: bool = False):
        """恢复收口。**以库为准**：内存里的 incident_id 可能在服务重启后丢失，
        只认内存会把「重启后恢复」的流永远留成僵尸事件（根因 1.1）。

        no_open 把「库中确实没有未恢复事件」缓存下来，保证每个流每个事件生命周期
        最多回查一次库，不会让每条 ok 结果都产生一次查询。
        """
        iid = st.get("incident_id")
        if iid is None:
            if st.get("no_open"):
                return
            row = self.s.open_incident_for(*k)
            if not row:
                st["no_open"] = True
                return
            iid = int(row["id"])
        if rebuild:
            st["incident_id"] = iid          # 重放只重建内存状态，收口交给随后的实时结果
            st["no_open"] = False
            return
        self.s.incident_close(iid, ts)
        st["incident_id"] = None
        st["no_open"] = True

    def _apply(self, k: tuple, status: str, ts: int, error_class: str,
               rebuild: bool = False, st: dict | None = None):
        if st is None:
            st = self.state.setdefault(k, self._new_state())
        if status == "skipped":
            return  # 工具缺失等不参与判定
        if status == "fail":
            st["fail_streak"] += 1
            st["ok_streak"] = 0
            if st["fail_streak"] >= self.fail_threshold and st["incident_id"] is None:
                open_inc = self.s.open_incident_for(*k)
                if open_inc:  # 重启前事件未关闭
                    st["incident_id"] = open_inc["id"]
                    st["no_open"] = False
                    return
                if rebuild:
                    return   # 重放历史不新建事件：该事件早该在首次失败时就已落库
                reason = {"error_class": error_class, "fail_streak": st["fail_streak"]}
                merged = False
                if self.flap_window > 0:
                    prev = self.s.last_closed_incident(*k)
                    if prev:
                        gap = ts - int(prev.get("ended_at") or 0)
                        span = ts - int(prev.get("started_at") or ts)
                        if 0 <= gap <= self.flap_window and span <= self.flap_max:
                            merged = self.s.incident_reopen(int(prev["id"]), ts, reason)
                            if merged:
                                st["incident_id"] = int(prev["id"])
                if not merged:
                    st["incident_id"] = self.s.incident_open(*k, ts, reason)
                st["no_open"] = False
        elif status == "ok":
            st["ok_streak"] += 1
            st["fail_streak"] = 0
            if st["ok_streak"] >= self.recover_threshold:
                self._close_if_open(k, st, ts, rebuild=rebuild)

    def on_result(self, task_id: str, node_id: str, dns: str, url: str,
                  status: str, ts: int, error_class: str):
        with self._lock:
            self._apply(self.key(task_id, node_id, dns, url), status, ts, error_class)

    def counts(self) -> dict:
        with self._lock:
            open_n = sum(1 for st in self.state.values() if st["incident_id"])
            return {"tracked_streams": len(self.state), "open": open_n}
