"""告警诊断助手：error_class 分层初判 + 探测范围判定（通知模板与事件详情共用）。

设计约定（ONCALL_OPTIMIZATION.md 第一期）：
- classify() 把探测器已有的 error_class 映射到「层面 + 建议动作」，纯函数无 IO；
- verdict() 把「该任务各节点最近一轮探测状态」归纳为三档范围结论，同样纯函数；
- 涉及存储的查询（取各节点最近一轮、取审计/心跳窗口）由调用方（alerting/eventview/api_web）完成。
"""
from __future__ import annotations

# error_class → (层面, 建议动作)。未知类别归「待定位」。
LAYER_MAP: dict[str, tuple[str, str]] = {
    "dns_error": ("DNS 层", "域名解析失败：看 dns 任务逐线路表，核对解析商状态"),
    "expect_mismatch": ("DNS 层", "解析成功但与期望不符：核对答案是否被变更"),
    "refused": ("网络/端口层", "连接被拒绝：目标端口未监听或防火墙拦截"),
    "timeout": ("网络层", "连接/响应超时：看 mtr 逐跳定位丢包位置"),
    "network_unreachable": ("网络层", "网络不可达：检查节点出口/路由/IPv6"),
    "tls_error": ("TLS 层", "握手失败：证书链、SNI 或中间设备劫持"),
    "cert_expired": ("TLS 层", "证书余量不足：续期证书"),
    "http_5xx": ("服务端层", "目标返回 5xx：查应用/网关日志"),
    "http_4xx": ("应用层", "目标返回 4xx：核对鉴权、路径是否变更"),
    "keyword_miss": ("应用层", "内容关键字未命中：页面/接口内容是否被改动"),
    "regex_miss": ("应用层", "内容正则未命中：同关键字未命中"),
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


def classify(error_class: str) -> tuple[str, str]:
    """error_class → (层面, 建议动作)。空/未知返回「待定位」。"""
    return LAYER_MAP.get((error_class or "").strip(), ("待定位", "按原始错误与阶段耗时人工判读"))


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
