"""Agent↔Server 协议模型（pydantic，仅服务端强校验；Agent 端为普通 dict）。"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .util import validate_dns_spec, validate_target, validate_url


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
    type: Literal["ping", "curl", "mtr"]
    dns: str = ""
    url: str = ""
    status: Literal["ok", "fail", "skipped"]
    error_class: str = ""
    error: str = ""
    dns_server: str = ""
    resolved_ip: str = ""
    dns_time_ms: Optional[float] = None
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


class TaskCreate(BaseModel):
    model_config = ConfigDict(validate_default=True)  # 默认值也走校验（urls 非空约束）

    name: str = Field(min_length=1, max_length=64)
    type: Literal["ping", "curl", "mtr"]
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


class TaskUpdate(BaseModel):
    """部分更新：仅校验出现的字段。"""
    model_config = ConfigDict(validate_default=False)

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    target: Optional[str] = None
    urls: Optional[list[str]] = Field(default=None, max_length=10)
    interval_seconds: Optional[int] = Field(default=None, ge=10, le=86400)
    dns: Optional[list[str]] = Field(default=None, max_length=6)
    nodes: Optional[list[str]] = Field(default=None, max_length=200)
    enabled: Optional[bool] = None
    params: Optional[dict] = None

    @field_validator("target")
    @classmethod
    def _target(cls, v):
        """空串合法（curl 任务以 urls 为准）；类型相关的强校验在路由层按任务类型做。"""
        if v is None or v == "":
            return v
        if not validate_target(v) and not validate_url(v):
            raise ValueError(f"非法目标: {v!r}")
        return v

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

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    tags: Optional[dict] = None
