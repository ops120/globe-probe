"""配置加载：YAML 文件 + 环境变量覆盖。全部阈值集中于此，禁止散落魔法数字。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "server": {
        "listen": "127.0.0.1:8620",
        "database": "data/gpm.db",
        "webui_dir": "",              # 默认取包内 webui/static
        "admin_token": "",            # 设置后写接口要求 X-Admin-Token
        "ingest_batch_max": 500,      # 单次上报条数上限
        "late_window_seconds": 300,   # 迟到窗口
        "clock_skew_seconds": 120,
        "heartbeat_timeout": 60,      # 节点离线判定
        "retention_raw_days": 30,
        "retention_1m_days": 90,
        "retention_5m_days": 180,
        "retention_1h_days": 730,
        "retention_hb_days": 7,
        "retention_alerts_days": 30,   # 告警历史保留
        "retention_audit_days": 30,    # 操作审计保留
        "retention_outbox_days": 7,    # 通知重投记录（done/failed）保留
        "retention_incidents_days": 180,  # 已关闭事件保留
        "retention_external_days": 30,   # 第三方告警保留（按最后活动时间；提示型数据不必留久）
        "thread_pool_tokens": 40,     # AnyIO 线程池上限（防线程爆发 MemoryError）
        "tasks_cache_seconds": 15,    # /api/tasks 列表缓存秒数（0=禁用）；config_version 变更立即失效，?fresh=1 绕过
        # 通知里「点击查看」深链的前缀（如 https://gpm.example.com）。留空则通知不带链接——
        # 界面会显著提示未配置，不再让运维以为「链接坏了」。
        "public_url": "",
        "alert_eval_interval": 30,    # 告警规则评估周期（秒）
        "notify_retry_interval": 60,  # 通知失败重投的扫描周期（秒）
        "digest_check_interval": 300, # 巡检报告定时推送的检查周期（秒）
    },
    # —— 第三方告警 webhook 接收（第六期）——
    "hook": {
        # 单次 payload 上限；超出返回 413
        "max_body_bytes": 262144,        # 256 KB
        # 每来源每分钟接收上限；超出返回 429
        "rate_limit_per_min": 120,
        # 关联到本地事件的时间窗：±多少秒 = window * 2
        "link_window_seconds": 1800,     # 30 分钟
        # 名字参与匹配的最短长度（节点 n1=1 字符不应匹配会命中 n10）
        "link_min_name_len": 2,
    },
    # —— 第三方告警拉取（第六期，26）——
    "pull": {
        "default_interval_seconds": 300,   # 默认轮询周期
        "backoff_base_seconds": 300,       # 失败退避基数
        "backoff_max_seconds": 3600,       # 退避上限
    },
    # —— JEV 故障判断阈值（第七期）——
    "jev": {
        "min_support": 0.5,                # 单假设被判为「可能」的下限
        "weak_support": 0.5,               # 最高支持度低于此 → 依据薄弱
        "disagree_margin": 0.15,            # 最高与次高差距小于此 → 存在分歧
        "min_confidence": 0.3,              # 置信度下限
    },
    # —— 事件详情/聚合页（第二期/第四期）——
    "view": {
        "changes_pad_seconds": 1800,         # 事件窗口 ±X 分钟的同期变更
        "changes_per_card": 3,               # 卡片上只留 N 条
        "changes_limit_detail": 8,           # 弹窗里最多展示 N 条
        "changes_scan_limit": 64,            # 查变更的扫描上限
        "node_suspect_min_tasks": 3,         # 「节点上 X 个任务同时失败」横切提示阈值
        "dns_changes_window_seconds": 86400,
        "dns_changes_limit": 5,
        "matrix_max_cells": 60,              # 范围矩阵上限
        "matrix_max_nodes": 32,
        "blast_scan_limit": 200,
        "dying_window_seconds": 1800,
        "dying_max_points": 60,
        "eventview_pad_seconds": 600,
        "bucket_1m_max_seconds": 10800,     # 3 小时
        "bucket_5m_max_seconds": 259200,    # 3 天
    },
    # —— 通知/告警评估（第三期）——
    "alert": {
        "scope_window_seconds": 60,
        "escalate_max_minutes": 1440,        # 升级冷却
        "retry_backoff_seconds": [60, 300, 900],
        "notify_default_timeout_seconds": 8.0,
        "notify_default_smtp_port": 587,
    },
    # —— 报表（第三期/第六期）——
    "report": {
        "flap_min_count": 3,
        "flap_window_seconds": 1800,
        "metrics_max_output_bytes": 204800,  # 200 KB
        "prober_curl_scan_limit_bytes": 524288,  # 关键字/正则只扫前 512 KB
        "agent_dns_memory_max": 500,
    },
    "agent": {
        "server_url": "http://127.0.0.1:8620",
        "register_token": "gpm-dev-register",
        "name": "",
        "tags": {},
        "data_dir": "data/agent",
        "heartbeat_interval": 15,
        "poll_interval": 10,
        "report_batch_size": 50,
        "report_interval": 5,
        "offline_buffer_max": 5000,   # 本地缓冲条数上限
    },
    "probe": {
        "min_interval_seconds": 10,   # ping/curl/tcp 间隔下限
        "min_mtr_interval_seconds": 60,
        "min_dns_interval_seconds": 30,  # dns 监控间隔下限（多线路对比开销更大）
        "ping_count": 4,
        "ping_timeout": 2.0,
        "curl_timeout": 10.0,
        "curl_expected_status": [[200, 300]],
        "mtr_cycles": 10,
        "mtr_max_hops": 30,
        "mtr_timeout": 45.0,
        "dns_timeout": 2.0,
        "dns_cache_ttl": 60,
        "dns_cache_max": 300,
        "fail_threshold": 3,
        "recover_threshold": 2,
        "flap_window_seconds": 600,   # 抖动合并窗口：关闭后多久内再次失败算同一次事件
        "flap_max_seconds": 21600,    # 单次事件上限（超过则不再合并，防止无限累积）
        # 陈旧事件自动收口：某流超过该秒数既无新样本也无恢复样本 → 收口（0=关闭该机制）。
        # 「沉默 ≠ 故障」：任务停用/节点移除后不再产生结果，否则事件会永远挂着
        "stale_after_seconds": 21600,
        "jitter_ratio": 0.1,
    },
    "logging": {"level": "INFO", "file": ""},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        self.raw = _deep_merge(DEFAULTS, self.raw or {})
        # 环境变量覆盖（敏感值不落盘）
        if os.environ.get("GPM_ADMIN_TOKEN"):
            self.raw["server"]["admin_token"] = os.environ["GPM_ADMIN_TOKEN"]
        if os.environ.get("GPM_REGISTER_TOKEN"):
            self.raw["agent"]["register_token"] = os.environ["GPM_REGISTER_TOKEN"]

    @property
    def server(self) -> dict:
        return self.raw["server"]

    @property
    def agent(self) -> dict:
        return self.raw["agent"]

    @property
    def probe(self) -> dict:
        return self.raw["probe"]

    @property
    def logging(self) -> dict:
        return self.raw["logging"]

    # —— 按模块拆出来的配置块；调用方读 cfg.hook.xxx / cfg.jev.xxx 等 ——
    @property
    def hook(self) -> dict:
        return self.raw["hook"]

    @property
    def pull(self) -> dict:
        return self.raw["pull"]

    @property
    def jev(self) -> dict:
        return self.raw["jev"]

    @property
    def view(self) -> dict:
        return self.raw["view"]

    @property
    def alert(self) -> dict:
        return self.raw["alert"]

    @property
    def report(self) -> dict:
        return self.raw["report"]

    # —— 以下是按模块拆出来的配置块；调用方读 cfg.hook.xxx / cfg.jev.xxx 等 ——
    @property
    def hook(self) -> dict:
        return self.raw["hook"]

    @property
    def pull(self) -> dict:
        return self.raw["pull"]

    @property
    def jev(self) -> dict:
        return self.raw["jev"]

    @property
    def view(self) -> dict:
        return self.raw["view"]

    @property
    def alert(self) -> dict:
        return self.raw["alert"]

    @property
    def report(self) -> dict:
        return self.raw["report"]


def load_config(path: str | None) -> Config:
    if not path:
        return Config()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml  # 可选依赖
        data = yaml.safe_load(text) or {}
    else:
        import json
        data = json.loads(text or "{}")
    return Config(data)
