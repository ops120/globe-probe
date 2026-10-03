"""告警诊断助手：error_class 分层初判 + 探测范围判定（通知模板与事件详情共用）。

设计约定（ONCALL_OPTIMIZATION.md 第一期）：
- classify() 把探测器已有的 error_class 映射到「层面 + 建议动作」，纯函数无 IO；
- verdict() 把「该任务各节点最近一轮探测状态」归纳为三档范围结论，同样纯函数；
- 涉及存储的查询（取各节点最近一轮、取审计/心跳窗口）由调用方（alerting/eventview/api_web）完成。
"""
from __future__ import annotations

# error_class → (层面, 建议动作)。未知类别归「待定位」。
#
# 词表必须与探测器**实际产出的** error_class 对齐：早期版本只收了 timeout/tls_error 等少数
# 几类，线上 34334 条真实失败样本里有 83% 落到「待定位」，头牌的「分层初判」等于没生效
# （见 .docs/ONCALL_OPTIMIZATION_2.md 根因 1.3）。下列每一类都能在 probers/ 里找到出处，
# tests/unit/test_diagnose.py 会按 src 扫描做「词表一致性」回归。
LAYER_MAP: dict[str, tuple[str, str]] = {
    # ---- DNS 层（common/dnsres.py 的 DnsError kinds + dnsmon）----
    "dns_error": ("DNS 层", "域名解析失败：看 dns 任务逐线路表，核对解析商状态"),
    "dns_timeout": ("DNS 层", "解析超时/线路不可达：看 dns 任务逐线路表，核对解析线路与解析商"),
    "dns_servfail": ("DNS 层", "解析服务器返回 SERVFAIL：解析商故障或域名配置异常"),
    "dns_refused": ("DNS 层", "解析请求被拒绝：核对解析线路是否放行该客户端"),
    "dns_formerr": ("DNS 层", "解析请求格式错误：核对域名写法与线路配置"),
    "nx_domain": ("DNS 层", "域名不存在（NXDOMAIN）：域名过期或线路/拼写错误"),
    "expect_mismatch": ("DNS 层", "解析成功但与期望不符：核对答案是否被变更"),
    # ---- 网络 / 端口层 ----
    "timeout": ("网络层", "连接/响应超时：看 mtr 逐跳定位丢包位置"),
    "connect_timeout": ("网络/端口层", "TCP 连接超时：端口未监听或被中间设备丢包；先 nc -vz 探端口再看 mtr"),
    "response_timeout": ("网络/服务端层", "已连接但响应超时：目标应用处理不过来或被限流；看阶段耗时与服务端日志"),
    "recv_error": ("网络层", "连接被中途重置/接收失败：查中间设备与目标负载"),
    "network_unreachable": ("网络层", "网络不可达：检查节点出口/路由/IPv6"),
    "unreachable": ("网络层", "目标不可达：检查路由/出口/目标是否存活"),
    "refused": ("网络/端口层", "连接被拒绝：目标端口未监听或防火墙拦截"),
    "path_fail": ("网络层", "路径末跳 100% 丢包：目标不可达；看 mtr 逐跳定位丢包位置"),
    "path_unreachable": ("网络层", "路径不可达：改用 URL/TCP 类任务验证端口与应用层"),
    # ---- TLS ----
    "tls_error": ("TLS 层", "握手失败：证书链、SNI 或中间设备劫持"),
    "cert_expired": ("TLS 层", "证书余量不足：续期证书"),
    # ---- 应用 / 服务端（http_<码> 为动态拼出，见 classify 的码段判定）----
    "http_5xx": ("服务端层", "目标返回 5xx：查应用/网关日志"),
    "http_4xx": ("应用层", "目标返回 4xx：核对鉴权、路径是否变更"),
    "keyword_miss": ("应用层", "内容关键字未命中：页面/接口内容是否被改动"),
    "regex_miss": ("应用层", "内容正则未命中：同关键字未命中"),
    "other": ("待定位", "未归类的探测器退出码：保留原始错误人工判读"),
    # ---- 节点侧 ----
    "tool_missing": ("节点侧", "探测器工具缺失：安装工具或换节点"),
    "unsupported": ("节点侧", "节点不支持该探测模式：换节点或改参数"),
    "fake_ip": ("节点侧(DNS)", "解析被代理劫持为 fake-ip：该任务配置 DoH 线路"),
    "parse_error": ("节点侧", "探测输出解析失败：反馈原始输出现象"),
    "mtr_parse_error": ("节点侧", "路径输出解析失败：反馈原始输出现象"),
}

# 范围三档 → (结论短句, 处置提示)
_SCOPE_VERDICT = {
    "all_nodes": ("全节点失败 → 疑似目标侧故障", "优先查目标本身与公共链路"),
    "partial": ("部分节点失败 → 疑似局部链路/线路问题", "对比失败与正常节点的出口与线路"),
    "single_node": ("仅单节点失败 → 疑似该节点侧问题", "优先查该节点出口、资源与工具"),
}


_UNKNOWN: tuple[str, str] = ("待定位", "按原始错误与阶段耗时人工判读")


def _classify_http(ec: str) -> tuple[str, str] | None:
    """http_<状态码> 的码段判定。

    curl 探测器的状态码类是**动态拼的**（probers/curl.py: f"http_{http_code}"），
    所以词表里的 http_4xx/http_5xx 这两个「形状键」永远匹配不到真实数据——线上实测
    http_0/http_403/... 全都落进「待定位」。这里按码段兜住，词表键仅作兼容保留。
    """
    code = ec[len("http_"):]
    if code.endswith("xx") and code[:-2].isdigit():     # http_5xx / http_4xx 形状
        code = code[:-2] + "00"
    if not code.isdigit():
        return None
    n = int(code)
    if n == 0:
        return ("网络层", "未拿到 HTTP 响应：连接未能完成或被中间设备中断；看 mtr 逐跳")
    if 400 <= n < 500:
        return ("应用层", f"目标返回 {n}：核对鉴权、路径或期望状态码配置")
    if 500 <= n < 600:
        return ("服务端层", f"目标返回 {n}：查应用/网关日志")
    return ("应用层", f"目标返回 {n}：核对期望状态码配置")


# 层面 → **可直接粘贴运行**的第一条排查命令（.docs/ONCALL_OPTIMIZATION_2.md 第三期 13）。
# 卡片上的「建议」是散文，值班的人还得自己想命令；这里把「第一步跑什么」直接给出来。
# 占位符：<域名> <解析线路> <目标IP> <主机> <端口> <URL>
RUNBOOK: dict[str, str] = {
    "DNS 层": "dig @<解析线路> <域名> +short   # 或 nslookup <域名> <解析线路>",
    "网络层": "mtr -r -c 10 <目标IP>            # Windows: tracert -d <目标IP>",
    "网络/端口层": "nc -vz <主机> <端口>          # 无 nc 时: curl -v --connect-timeout 3 telnet://<主机>:<端口>",
    "网络/服务端层": "curl -o /dev/null -s -w 'dns=%{time_namelookup} connect=%{time_connect} ttfb=%{time_starttransfer} total=%{time_total}\\n' <URL>",
    "TLS 层": "openssl s_client -connect <主机>:443 -servername <主机> </dev/null 2>/dev/null | openssl x509 -noout -dates",
    "服务端层": "curl -sI <URL>",
    "应用层": "curl -s <URL> | head -c 500",
    "节点侧": "在节点上查进程与资源：ps -ef | grep gpm-agent；top -b -n1 | head -5",
    "节点侧(DNS)": "确认该节点 DNS 是否被代理劫持为 fake-ip；给该任务配 DoH 线路",
}


def runbook_for(layer: str) -> str:
    """层面 → 可粘贴命令；未知层面返回空串（不编造命令）。"""
    return RUNBOOK.get((layer or "").strip(), "")


def classify(error_class: str) -> tuple[str, str]:
    """error_class → (层面, 建议动作)。空/未知返回「待定位」。"""
    ec = (error_class or "").strip()
    if not ec:
        return _UNKNOWN
    hit = LAYER_MAP.get(ec)
    if hit:
        return hit
    if ec.startswith("http_"):
        got = _classify_http(ec)
        if got:
            return got
    return _UNKNOWN


def verdict(states: list[dict]) -> dict:
    """范围判定：states = 该任务各节点最近一轮探测状态 [{node_name, status}]。

    status ∈ ok|fail|skipped（skipped 不计入分母，如实呈现）。返回：
    {mode, failed, total, failed_names, verdict, advice}
    """
    real = [s for s in states if s.get("status") in ("ok", "fail")]
    total = len(real)
    failed_names = [s.get("node_name") or str(s.get("node_id") or "") for s in real
                    if s.get("status") == "fail"]
    failed = len(failed_names)
    if total == 0 or failed == 0:
        mode = ""                      # 没有失败样本，不做范围结论
    elif failed == total and total > 1:
        mode = "all_nodes"
    elif failed == 1 and total > 1:
        mode = "single_node"
    else:
        mode = "partial"
    text, advice = _SCOPE_VERDICT.get(mode, ("", ""))
    return {"mode": mode, "failed": failed, "total": total, "failed_names": failed_names,
            "verdict": text, "advice": advice}
