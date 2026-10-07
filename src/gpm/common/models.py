"""Agent↔Server 协议模型（pydantic，仅服务端强校验；Agent 端为普通 dict）。"""
from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .util import (
    is_ip,
    validate_dns_spec,
    validate_domain,
    validate_host_port,
    validate_target,
    validate_url,
)

TASK_TYPES = ("ping", "curl", "mtr", "tcp", "dns")
_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS")


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    register_token: str
    tags: dict = Field(default_factory=dict)
    version: str = "unknown"
    system: dict = Field(default_factory=dict)
    local_ip: str = Field(default="", max_length=64)   # 节点自报的本机 IP（到服务端那一跳的源地址）


class HeartbeatIn(BaseModel):
    node_id: str
    token: str
    config_version: int = 0
    stats: dict = Field(default_factory=dict)


class ProbeResultIn(BaseModel):
    ts: int
    task_id: str
    type: Literal["ping", "curl", "mtr", "tcp", "dns"]
    dns: str = ""
    url: str = ""
    status: Literal["ok", "fail", "skipped"]
    error_class: str = ""
    error: str = ""
    dns_server: str = ""
    resolved_ip: str = ""
    dns_time_ms: float | None = None
    metrics: dict = Field(default_factory=dict)
    config_version: int = 0

    @field_validator("task_id")
    @classmethod
    def _tid(cls, v):
        if not v or len(v) > 40 or any(c in v for c in ";|&`$><\\'\""):
            raise ValueError("非法 task_id")
        return v


class ResultsIn(BaseModel):
    node_id: str
    token: str
    results: list[ProbeResultIn] = Field(max_length=1000)


def validate_params(ptype: str, params: dict) -> None:
    """任务 params 形状校验（TaskCreate 校验器与 update_task 路由共用）。

    非法直接 raise ValueError：pydantic 路径自动 422；路由层包成 HTTPException(422)。
    只挡「形状非法」（枚举/类型/可编译性），业务数值区间交给探测器如实处理。
    """
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")

    def _bool(key: str):
        v = params.get(key)
        if v is not None and not isinstance(v, bool):
            raise ValueError(f"params.{key} 必须是布尔值")

    ip_ver = params.get("ip_version")
    if ip_ver is not None and ip_ver not in ("auto", "4", "6"):
        raise ValueError("params.ip_version 只能是 auto / 4 / 6")

    if ptype == "curl":
        m = params.get("method")
        if m is not None and str(m).upper() not in _HTTP_METHODS:
            raise ValueError(f"params.method 只支持 {'/'.join(_HTTP_METHODS)}")
        headers = params.get("headers")
        if headers is not None:
            if not isinstance(headers, dict) or len(headers) > 10 or any(
                    not isinstance(k, str) or not k
                    or not isinstance(v, (str, int, float, bool))
                    for k, v in headers.items()):
                raise ValueError("params.headers 必须是 ≤10 条的「字符串→标量」扁平映射")
        body = params.get("body")
        if body is not None and (not isinstance(body, str)
                                 or len(body.encode("utf-8")) > 8192):
            raise ValueError("params.body 必须是 ≤8KB 的字符串")
        rx = params.get("regex")
        if rx is not None:
            if not isinstance(rx, str):
                raise ValueError("params.regex 必须是字符串")
            try:
                re.compile(rx)
            except re.error as e:
                raise ValueError(f"params.regex 无法编译: {e}")
        _bool("follow_redirects")
        _bool("cert_check")
    elif ptype == "tcp":
        port = params.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)
                                 or not 1 <= port <= 65535):
            raise ValueError("params.port 必须是 1~65535 的整数")
        _bool("tls")
    elif ptype == "dns":
        exp = params.get("expected_ips")
        if exp is not None:
            if (not isinstance(exp, list) or len(exp) > 32
                    or any(not isinstance(x, str) or not x.strip() for x in exp)):
                raise ValueError("params.expected_ips 必须是 ≤32 条的字符串数组（IP 或 CIDR）")
            for x in exp:
                xs = x.strip()
                if "/" in xs:
                    try:
                        ipaddress.ip_network(xs, strict=False)
                    except ValueError:
                        raise ValueError(f"params.expected_ips 网段非法: {x!r}")
                elif not is_ip(xs):
                    raise ValueError(f"params.expected_ips 必须是 IP/网段: {x!r}")
        rx = params.get("expected_regex")
        if rx is not None:
            if not isinstance(rx, str):
                raise ValueError("params.expected_regex 必须是字符串")
            try:
                re.compile(rx)
            except re.error as e:
                raise ValueError(f"params.expected_regex 无法编译: {e}")
    elif ptype == "mtr":
        pm = params.get("probe_mode")
        if pm is not None and pm not in ("icmp", "tcp", "udp"):
            raise ValueError("params.probe_mode 只能是 icmp / tcp / udp")
        _bool("show_asn")


class TaskCreate(BaseModel):
    model_config = ConfigDict(validate_default=True)  # 默认值也走校验（urls 非空约束）

    name: str = Field(min_length=1, max_length=64)
    type: Literal["ping", "curl", "mtr", "tcp", "dns"]
    target: str = ""
    urls: list[str] = Field(default_factory=list, max_length=10)
    interval_seconds: int = Field(default=10, ge=10, le=86400)
    dns: list[str] = Field(default_factory=list, max_length=6)
    nodes: list[str] = Field(default_factory=list, max_length=200)  # 空=全部分配节点
    params: dict = Field(default_factory=dict)

    @field_validator("target")
    @classmethod
    def _target(cls, v, info):
        t = info.data.get("type")
        if t == "curl":
            return v  # 校验在 urls
        if t == "tcp":
            if not validate_host_port(v or ""):
                raise ValueError(f"非法目标（需 host:port）: {v!r}")
            return v
        if t == "dns":
            if not validate_domain(v or ""):
                raise ValueError(f"非法域名目标: {v!r}")
            return v
        if not validate_target(v or ""):
            raise ValueError(f"非法目标: {v!r}")
        return v

    @field_validator("urls")
    @classmethod
    def _urls(cls, v, info):
        if info.data.get("type") == "curl":
            if not v:
                raise ValueError("curl 任务至少一个 URL")
            for u in v:
                if not validate_url(u):
                    raise ValueError(f"非法 URL: {u!r}")
        return v

    @field_validator("dns")
    @classmethod
    def _dns(cls, v):
        for d in v:
            if not validate_dns_spec(d):
                raise ValueError(f"非法 DNS 线路: {d!r}（支持 IP、doh:<URL>、dot:<ip>[:853]）")
        return v

    @field_validator("params")
    @classmethod
    def _params(cls, v, info):
        validate_params(info.data.get("type") or "ping", v)
        return v


class TaskUpdate(BaseModel):
    """部分更新：仅校验出现的字段。"""
    model_config = ConfigDict(validate_default=False)

    name: str | None = Field(default=None, min_length=1, max_length=64)
    target: str | None = None
    urls: list[str] | None = Field(default=None, max_length=10)
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    dns: list[str] | None = Field(default=None, max_length=6)
    nodes: list[str] | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    params: dict | None = None

    @field_validator("target")
    @classmethod
    def _target(cls, v):
        """空串合法（curl 任务以 urls 为准）；类型相关的强校验在路由层按任务类型做。"""
        if v is None or v == "":
            return v
        # 宽松放行各类型目标形态（host:port / 域名 / URL / IP），注入字符仍被挡；
        # 严格校验在 update_task 路由按任务类型执行
        if validate_target(v) or validate_url(v) or validate_host_port(v) or validate_domain(v):
            return v
        raise ValueError(f"非法目标: {v!r}")

    @field_validator("urls")
    @classmethod
    def _urls(cls, v):
        if v is None:
            return v
        for u in v:
            if not validate_url(u):
                raise ValueError(f"非法 URL: {u!r}")
        return v

    @field_validator("dns")
    @classmethod
    def _dns(cls, v):
        if v is None:
            return v
        for d in v:
            if not validate_dns_spec(d):
                raise ValueError(f"非法 DNS 线路: {d!r}（支持 IP、doh:<URL>、dot:<ip>[:853]）")
        return v


class NodeUpdate(BaseModel):
    model_config = ConfigDict(validate_default=False)

    name: str | None = Field(default=None, min_length=1, max_length=64)
    tags: dict | None = None
