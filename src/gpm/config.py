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
        "alert_eval_interval": 30,    # 告警规则评估周期（秒）
        "notify_retry_interval": 60,  # 通知失败重投的扫描周期（秒）
        "digest_check_interval": 300, # 巡检报告定时推送的检查周期（秒）
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
        "min_interval_seconds": 10,   # ping/curl 间隔下限
        "min_mtr_interval_seconds": 60,
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
