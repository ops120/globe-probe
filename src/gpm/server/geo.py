"""节点地理定位：标签优先 → 内置区表 → 在线 GeoIP（节点出口 IP / 服务端出口），结果落缓存。

诚实性约定：每个位置都带 source 字段说明来源；用「服务端出口」近似出来的位置标 approx=True，
前端会明确标注，不假装是节点精确位置。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

# 常用区域质心（离线可用，够把节点画到大致位置）：键做包含匹配（小写）
REGION_TABLE = {
    # 中国大陆
    "cn-north": (39.90, 116.40, "中国 · 华北(北京)"),
    "beijing": (39.90, 116.40, "中国 · 北京"),
    "bj": (39.90, 116.40, "中国 · 北京"),
    "cn-east": (31.23, 121.47, "中国 · 华东(上海)"),
    "shanghai": (31.23, 121.47, "中国 · 上海"),
    "sh": (31.23, 121.47, "中国 · 上海"),
    "hangzhou": (30.27, 120.16, "中国 · 杭州"),
    "cn-south": (23.13, 113.26, "中国 · 华南(广州)"),
    "guangzhou": (23.13, 113.26, "中国 · 广州"),
    "shenzhen": (22.54, 114.06, "中国 · 深圳"),
    "chengdu": (30.57, 104.07, "中国 · 成都"),
    "wuhan": (30.59, 114.31, "中国 · 武汉"),
    "xian": (34.34, 108.94, "中国 · 西安"),
    "hongkong": (22.32, 114.17, "中国 · 香港"),
    "hk": (22.32, 114.17, "中国 · 香港"),
    "taiwan": (25.03, 121.57, "中国 · 台北"),
    "singapore": (1.35, 103.82, "新加坡"),
    "sg": (1.35, 103.82, "新加坡"),
    # 亚太
    "japan": (35.68, 139.69, "日本 · 东京"),
    "tokyo": (35.68, 139.69, "日本 · 东京"),
    "osaka": (34.69, 135.50, "日本 · 大阪"),
    "korea": (37.57, 126.98, "韩国 · 首尔"),
    "seoul": (37.57, 126.98, "韩国 · 首尔"),
    "india": (19.08, 72.88, "印度 · 孟买"),
    "mumbai": (19.08, 72.88, "印度 · 孟买"),
    "australia": (-33.87, 151.21, "澳大利亚 · 悉尼"),
    "sydney": (-33.87, 151.21, "澳大利亚 · 悉尼"),
    "indonesia": (-6.21, 106.85, "印尼 · 雅加达"),
    "vietnam": (10.82, 106.63, "越南 · 胡志明"),
    "thailand": (13.76, 100.50, "泰国 · 曼谷"),
    "malaysia": (3.14, 101.69, "马来西亚 · 吉隆坡"),
    # 欧美
    "us-west": (37.77, -122.42, "美国 · 西海岸(旧金山)"),
    "us-east": (39.04, -77.49, "美国 · 东海岸(弗吉尼亚)"),
    "us-central": (41.88, -87.63, "美国 · 中部(芝加哥)"),
    "losangeles": (34.05, -118.24, "美国 · 洛杉矶"),
    "seattle": (47.61, -122.33, "美国 · 西雅图"),
    "siliconvalley": (37.35, -121.95, "美国 · 硅谷"),
    "canada": (43.65, -79.38, "加拿大 · 多伦多"),
    "brazil": (-23.55, -46.63, "巴西 · 圣保罗"),
    "uk": (51.51, -0.13, "英国 · 伦敦"),
    "london": (51.51, -0.13, "英国 · 伦敦"),
    "germany": (50.11, 8.68, "德国 · 法兰克福"),
    "frankfurt": (50.11, 8.68, "德国 · 法兰克福"),
    "netherlands": (52.37, 4.90, "荷兰 · 阿姆斯特丹"),
    "france": (48.86, 2.35, "法国 · 巴黎"),
    "russia": (55.75, 37.62, "俄罗斯 · 莫斯科"),
    "uae": (25.20, 55.27, "阿联酋 · 迪拜"),
    "dubai": (25.20, 55.27, "阿联酋 · 迪拜"),
}

GEO_API = "http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,lat,lon,isp,query"


def _is_private(ip: str) -> bool:
    if not ip:
        return True
    if ip.startswith(("10.", "127.", "169.254.", "192.168.", "::1", "0.")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return True
    return False


def _tag_latlng(tags: dict):
    try:
        lat = float(tags.get("lat"))
        lng = float(tags.get("lng") if tags.get("lng") is not None else tags.get("lon"))
    except (TypeError, ValueError):
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng, str(tags.get("place") or tags.get("city") or tags.get("region") or "标签坐标")
    return None


def resolve_place(text: str):
    """把「位置」文本解析成坐标：先按内置区表的键匹配，再按中文标签包含匹配。

    这样管理工作台上可以直接写「上海」「cn-east」「上海/华东」等，不必查经纬度。
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in REGION_TABLE:
        return REGION_TABLE[t]
    for k, val in REGION_TABLE.items():
        if len(k) >= 3 and k in t:
            return val
    for val in REGION_TABLE.values():          # 中文标签包含匹配：'上海' → '中国 · 上海'
        if t in val[2].lower():
            return val
    return None


def _tag_region(tags: dict):
    for key in ("region", "city", "province", "area", "country", "isp", "place"):
        v = str(tags.get(key) or "").strip().lower()
        if not v:
            continue
        if v in REGION_TABLE:
            return REGION_TABLE[v]
        for k, val in REGION_TABLE.items():
            if len(k) >= 3 and k in v:
                return val
    return None


def _fetch(ip: str, timeout: float = 6.0) -> dict:
    """在线 GeoIP（ip-api.com，免费、无需 key）。失败返回 {'ok': False, 'err': ...}。"""
    url = GEO_API.format(ip=urllib.parse.quote(ip, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "gpm-geo/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return {"ok": False, "err": str(e)[:120]}
    if d.get("status") != "success":
        return {"ok": False, "err": str(d.get("message") or d.get("status") or "unknown")}
    return {"ok": True, "lat": d.get("lat"), "lng": d.get("lon"),
            "place": " · ".join(x for x in (d.get("country"), d.get("regionName"), d.get("city")) if x),
            "isp": d.get("isp") or "", "ip": d.get("query") or ip}


def _cached_online(storage, key: str, ip: str, ts: int, ttl: int, neg_ttl: int) -> dict:
    hit = storage.geo_cache_get(key, ttl if True else neg_ttl, ts)
    if hit is not None:
        return hit
    data = _fetch(ip)
    # 成功的缓存久一点、失败的缓存短一点，避免反复慢查询
    storage.geo_cache_put(key, data if data.get("ok") else {"ok": False, "err": data.get("err", ""),
                                                            "ts_hint": "negative"}, ts)
    return data


def locate_nodes(storage, cfg, ts: int = 0) -> list[dict]:
    """给每个节点算出坐标：标签坐标 → 标签区表 → 出口 IP 在线查询 → 服务端出口近似。"""
    import time
    ts = ts or int(time.time())
    ttl = int(cfg.server.get("geo_cache_ttl", 86400)) if hasattr(cfg, "server") else 86400
    out, unknown = [], []
    for n in storage.list_nodes():
        tags = n.get("tags") or {}
        item = {"node_id": n["id"], "node_name": n["name"], "status": n["status"],
                "tags": tags, "local_ip": n.get("local_ip", ""), "egress_ip": n.get("egress_ip", ""),
                "avail_24h": storage.node_avail(n["id"], ts - 86400)["avail"]}
        loc = _tag_latlng(tags)
        src = "标签坐标"
        approx = False
        if loc:
            item.update(lat=loc[0], lng=loc[1], place=loc[2], source=src, approx=approx)
            out.append(item)
            continue
        loc = _tag_region(tags)
        if loc:
            item.update(lat=loc[0], lng=loc[1], place=loc[2], source="标签(内置区表)", approx=False)
            out.append(item)
            continue
        # 自定义「IP 段 → 位置」：IDC 内网段（如 10.10.10.0/24 在上海）优先于在线查询。
        # 先看节点自报的本机 IP，再看服务端观测到的出口 IP；命中即定案（用标志位而不是
        # for/else —— 内层 break 不会跳出外层节点循环，会掉进下面的 fallback 逻辑）
        hit = None
        for ip, label in ((n.get("local_ip") or "", "本机 IP"),
                          (n.get("egress_ip") or "", "出口 IP")):
            net = storage.match_geo_network(ip)
            if net:
                hit = (net, label, ip)
                break
        if hit:
            net, label, ip = hit
            item.update(lat=net["lat"], lng=net["lng"], place=net["place"],
                        source="自定义网段(" + net["cidr"] + " 匹配 " + label + " " + ip + ")",
                        approx=False)
            out.append(item)
            continue
        eip = n.get("egress_ip") or ""
        if eip and not _is_private(eip):
            g = _cached_online(storage, eip, eip, ts, ttl, ttl // 4)
            if g.get("ok"):
                item.update(lat=g["lat"], lng=g["lng"], place=g.get("place") or eip,
                            source=f"在线查询({eip})", approx=False, isp=g.get("isp", ""))
                out.append(item)
                continue
        # 出口是内网/回环：用「服务端自身出口」近似（明确标注 approx）
        g = _cached_online(storage, "__self__", "", ts, ttl, ttl // 4)
        if g.get("ok"):
            item.update(lat=g["lat"], lng=g["lng"], place=g.get("place") or "服务端出口",
                        source="在线查询(服务端出口近似)", approx=True, isp=g.get("isp", ""))
            out.append(item)
            continue
        item["reason"] = "无定位信息（可给节点加标签 region=… 或 lat/lng=…）"
        if not g.get("ok"):
            item["reason"] += f"；在线查询失败：{g.get('err', '')}"
        unknown.append(item)
    return out


def _task_label(t: dict) -> str:
    return t.get("target") or ((t.get("urls") or [""])[0])


def locate_flows(storage, cfg, ts: int = 0, budget: int = 8) -> dict:
    """探测链路：节点坐标 → 目标（最近一次解析到的 IP）坐标。

    预算控制：一次请求最多做 budget 次「未命中缓存」的在线查询，避免首屏把外部接口打爆；
    超预算的目标标记为 pending，前端提示「稍后自动补齐」（下一次请求命中缓存即出现）。
    """
    import time
    ts = ts or int(time.time())
    ttl = int(cfg.server.get("geo_cache_ttl", 86400)) if hasattr(cfg, "server") else 86400
    nodes = {n["node_id"]: n for n in locate_nodes(storage, cfg, ts)}
    tasks = {t["id"]: t for t in storage.list_tasks()}
    flows, pending, intra = [], 0, []
    ip_geo: dict = {}
    for t in tasks.values():
        if not t.get("enabled"):
            continue
        for st in storage.result_streams(t["id"]):
            ip = (st.get("latest_ip") or "").strip()
            node = nodes.get(st["node_id"])
            if not ip or not node:
                continue
            if _is_private(ip):
                intra.append({"task": t["name"], "target": _task_label(t), "ip": ip})
                continue
            key = "ip:" + ip
            g = ip_geo.get(ip)
            if g is None:
                cached = storage.geo_cache_get(key, ttl, ts)
                if cached is not None:
                    g = cached
                elif budget > 0:
                    budget -= 1
                    g = _cached_online(storage, key, ip, ts, ttl, ttl // 4)
                else:
                    pending += 1
                    ip_geo[ip] = {"ok": False, "budget_out": True}
                    continue
                ip_geo[ip] = g
            if not g.get("ok"):
                continue
            flows.append({
                "node_id": st["node_id"], "node_name": node["node_name"],
                "from": [node["lng"], node["lat"]],
                "to": [g["lng"], g["lat"]],
                "task_id": t["id"], "task_name": t["name"], "task_type": t["type"],
                "target": _task_label(t), "resolved_ip": ip, "to_place": g.get("place") or ip,
                "to_source": f"在线查询({ip})",
                "status": st.get("latest_status") or "", "error_class": st.get("latest_error_class") or "",
                "ts": st.get("latest_ts") or 0,
            })
    return {"flows": flows, "pending": pending, "intra": intra[:20]}


def unknown_nodes(storage, cfg) -> list[dict]:
    """单独返回无法定位的节点（前端在侧边列出，提示补标签）。"""
    located = {x["node_id"] for x in locate_nodes(storage, cfg)}
    return [{"node_id": n["id"], "node_name": n["name"], "status": n["status"],
             "reason": "无定位信息（加标签 region=cn-north 或 lat=39.9,lng=116.4 即可上图）"}
            for n in storage.list_nodes() if n["id"] not in located]
