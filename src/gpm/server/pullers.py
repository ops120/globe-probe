"""第三方告警「拉取」适配器（.docs/ONCALL_OPTIMIZATION_2.md 第六期 26）。

webhook 推不到（内网隔离 / 源侧不支持推送）时改用 API 拉。几条刻意的约束：

- **请求形状逐源写死**，不做「万能 JSON 映射」：那只会让人配错了还以为接通了。
- **只实现能确认形状的两家**：Grafana（Alertmanager v2 `/api/v2/alerts`，返回的正是
  webhook 里那种 alert 对象，可直接复用 hooks 的解析器）与 Zabbix（JSON-RPC
  `trigger.get`，`value` 0=OK / 1=PROBLEM）。腾讯云的 TC3-HMAC 签名、GCP 的 OAuth
  令牌属于另一类工作量，这里**明确抛 NotSupported**，让调用方如实报告「未实现」——
  绝不发一个假的成功。
- **游标与退避存 meta**：失败按 300s×2^n 退避（上限 1 小时），成功后清零。
- **只读**：只发拉取请求，不回写第三方，也不改本地的关联关系（关联仍由 ingest 侧统一做）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..config import Config
from . import hooks


class NotSupported(Exception):
    """该来源的 API 拉取未实现（需要官方签名 / OAuth，本期不做）。"""


# —— 以下阈值由 cfg.pull.* 提供；保留模块级同名常量供外部（包括 tests/ 与
# api_web.py）按属性名直接读取。默认值与 src/gpm/config.py DEFAULTS["pull"] 对齐；
# 运行期 init(cfg) 会覆盖。
DEFAULT_INTERVAL = 300          # 默认轮询周期（秒）
BACKOFF_BASE = 300              # 失败退避基数
BACKOFF_MAX = 3600              # 退避上限

_cfg: Config | None = None


def init(cfg) -> None:
    """由 app 在启动时注入 cfg；之后 DEFAULT_INTERVAL / BACKOFF_BASE / BACKOFF_MAX
    同步到 cfg.pull.*。"""
    global _cfg, DEFAULT_INTERVAL, BACKOFF_BASE, BACKOFF_MAX
    _cfg = cfg
    try:
        DEFAULT_INTERVAL = int(cfg.pull.get("default_interval_seconds", DEFAULT_INTERVAL) or DEFAULT_INTERVAL)
    except Exception:
        pass
    try:
        BACKOFF_BASE = int(cfg.pull.get("backoff_base_seconds", BACKOFF_BASE) or BACKOFF_BASE)
    except Exception:
        pass
    try:
        BACKOFF_MAX = int(cfg.pull.get("backoff_max_seconds", BACKOFF_MAX) or BACKOFF_MAX)
    except Exception:
        pass


# ---------------------------------------------------------------- 适配器

def build_grafana(settings: dict) -> dict:
    """Grafana / Alertmanager：`GET {base}/api/v2/alerts`。

    返回的就是 webhook 里那种 alert 对象数组，解析直接复用 hooks.parse("grafana")。
    """
    base = str(settings.get("url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("未配置拉取地址")
    headers = {"Accept": "application/json"}
    if settings.get("token"):
        headers["Authorization"] = "Bearer " + str(settings["token"])
    return {"url": base + "/api/v2/alerts", "method": "GET", "headers": headers, "body": None}


def parse_grafana(resp) -> list[dict]:
    if isinstance(resp, dict):
        resp = resp.get("alerts") or []
    return hooks.parse("grafana", {"alerts": resp if isinstance(resp, list) else []})


def build_zabbix(settings: dict) -> dict:
    """Zabbix：JSON-RPC `trigger.get`（5.4+ 用 API Token 作 auth）。"""
    base = str(settings.get("url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("未配置拉取地址")
    params = {"output": ["triggerid", "description", "priority", "lastchange", "value"],
              "selectHosts": ["host"], "monitored": True,
              "sortfield": "lastchange", "sortorder": "DESC", "limit": 100}
    body = {"jsonrpc": "2.0", "method": "trigger.get", "params": params, "id": 1}
    if settings.get("token"):
        body["auth"] = str(settings["token"])
    return {"url": base + "/api_jsonrpc.php", "method": "POST",
            "headers": {"Content-Type": "application/json-rpc"},
            "body": json.dumps(body, ensure_ascii=False).encode("utf-8")}


def parse_zabbix(resp) -> list[dict]:
    if not isinstance(resp, dict) or resp.get("error"):
        return []
    out = []
    for t in (resp.get("result") or []):
        if not isinstance(t, dict) or not t.get("triggerid"):
            continue
        hosts = t.get("hosts") or []
        host = ""
        if hosts and isinstance(hosts[0], dict):
            host = str(hosts[0].get("host") or "")
        out.append({
            "source_id": str(t["triggerid"]),
            "title": str(t.get("description") or ""),
            "severity": str(t.get("priority") or ""),
            # Zabbix: value 0=OK(恢复) / 1=PROBLEM(告警)
            "status": "resolved" if str(t.get("value")) == "0" else "firing",
            "started_at": int(t.get("lastchange") or 0),
            "ended_at": 0,
            "labels": {"host": host} if host else {},
            "url": "",
            "raw": t,
        })
    return out


def build_tencent(settings: dict) -> dict:
    raise NotSupported("腾讯云 DescribeAlarmHistory 需要 TC3-HMAC 签名，本期未实现")


def build_gcp(settings: dict) -> dict:
    raise NotSupported("GCP Cloud Monitoring 需要 OAuth 令牌，本期未实现")


ADAPTERS = {
    "grafana": (build_grafana, parse_grafana),
    "zabbix": (build_zabbix, parse_zabbix),
    "tencent": (build_tencent, None),
    "gcp": (build_gcp, None),
}


def supported(source: str) -> bool:
    """该来源是否支持 API 拉取。UI 与报告据此如实显示「未实现」。"""
    a = ADAPTERS.get(source)
    if not a:
        return False
    try:
        a[0]({"url": "http://x"})       # 试探是否抛 NotSupported（不真的发请求）
        return True
    except NotSupported:
        return False
    except ValueError:
        return True                     # 只是没配地址，能力本身是有的


# ---------------------------------------------------------------- 拉取一次

def _settings_for(storage, source: str) -> dict:
    get = storage.setting_get
    return {"url": get("pull_%s_url" % source, ""),
            "token": get("pull_%s_token" % source, "")}


def _default_fetch(url: str, method: str, headers: dict, body, timeout: int = 15):
    """默认抓取（可注入，测试用桩）。只读：只会发这一个请求。"""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def poll_source(storage, source: str, ts: int | None = None, fetch=None,
                limit: int = 200) -> dict:
    """拉一次并落库。返回结果摘要（含 `supported` / `error`），调用方据此如实展示。

    幂等与关联都复用 ingest 侧：这里只负责「取回来 + 落库 + 关联」，
    不新增一套告警模型。
    """
    now_s = int(ts or time.time())
    adapter = ADAPTERS.get(source)
    if not adapter:
        return {"source": source, "supported": False, "fetched": 0, "created": 0,
                "updated": 0, "error": "未知来源"}
    build, parse = adapter
    if parse is None or not supported(source):
        return {"source": source, "supported": False, "fetched": 0, "created": 0,
                "updated": 0, "error": "该来源的 API 拉取未实现（需要官方签名 / OAuth）"}
    settings = _settings_for(storage, source)
    try:
        req = build(settings)
    except ValueError as e:
        return {"source": source, "supported": True, "fetched": 0, "created": 0,
                "updated": 0, "error": str(e)}
    call = fetch or _default_fetch
    try:
        resp = call(req["url"], req["method"], req["headers"], req["body"])
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        storage.meta_set("pull_%s_fail" % source,
                         str(int(storage.meta_get("pull_%s_fail" % source, "0") or 0) + 1))
        _schedule_backoff(storage, source, now_s)
        storage.meta_set("pull_%s_last_error" % source, ("%s: %s" % (type(e).__name__, e))[:200])
        return {"source": source, "supported": True, "fetched": 0, "created": 0,
                "updated": 0, "error": "%s: %s" % (type(e).__name__, e)}
    alerts = parse(resp)[:limit]
    created = updated = 0
    for a in alerts:
        aid, is_new = storage.external_alert_upsert(
            source, str(a["source_id"]), dict(a, raw=hooks.redact(a.get("raw") or {})), now_s)
        created += 1 if is_new else 0
        updated += 0 if is_new else 1
        rec = storage.external_alert_get(aid)
        if rec:
            from .api_web import _correlate_external  # 延迟导入，避免模块级循环依赖
            _correlate_external(storage, rec, now_s)
    storage.meta_set("pull_%s_fail" % source, "0")
    storage.meta_set("pull_%s_last_ok" % source, str(now_s))
    storage.meta_set("pull_%s_last_error" % source, "")
    storage.meta_set("pull_%s_next_at" % source, "0")
    return {"source": source, "supported": True, "fetched": len(alerts),
            "created": created, "updated": updated, "error": ""}


def _schedule_backoff(storage, source: str, now_s: int) -> None:
    n = int(storage.meta_get("pull_%s_fail" % source, "0") or 0)
    delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** max(0, n - 1)))
    storage.meta_set("pull_%s_next_at" % source, str(now_s + delay))


def state(storage, source: str) -> dict:
    """给 UI/报告的当前状态（如实区分「支持但没配」「已配好」「未实现」）。"""
    settings = _settings_for(storage, source)
    return {
        "source": source,
        "supported": supported(source),
        "enabled": storage.setting_get("pull_%s_enabled" % source, "0") == "1",
        "configured": bool(str(settings.get("url") or "").strip()),
        # 地址回显（便于运维看清配了什么），但 **Token 绝不回显**
        "url": str(settings.get("url") or ""),
        "interval_seconds": int(storage.setting_get("pull_interval_seconds", "300") or 300),
        "last_ok": int(storage.meta_get("pull_%s_last_ok" % source, "0") or 0),
        "last_error": storage.meta_get("pull_%s_last_error" % source, ""),
        "fail_count": int(storage.meta_get("pull_%s_fail" % source, "0") or 0),
        "next_at": int(storage.meta_get("pull_%s_next_at" % source, "0") or 0),
    }


def due_sources(storage, now_s: int) -> list[str]:
    """到期该拉的来源（enabled + 支持 + 已配地址 + 过了退避）。"""
    out = []
    for src in hooks.SOURCES:
        st = state(storage, src)
        if st["enabled"] and st["supported"] and st["configured"] and now_s >= st["next_at"]:
            out.append(src)
    return out
