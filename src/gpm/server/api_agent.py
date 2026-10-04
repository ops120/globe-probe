"""Agent API：注册（幂等）、心跳+配置同步、批量结果上报。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request

from ..common.models import HeartbeatIn, RegisterIn, ResultsIn
from ..common.util import now, sha256


def _token_eq(a: str, b: str) -> bool:
    """常量时间比较：节点凭据本身就是 sha256 哈希，逐字节计时恢复哈希=直接冒充节点。"""
    return secrets.compare_digest(a.encode(), b.encode())


def _match_token(app_state, raw: str) -> tuple[bool, str]:
    """注册 Token 校验：优先数据库里的多 Token（可吊销/停用），
    config 中的单 Token 退化为「引导 Token」（兼容旧部署）。返回 (是否通过, token_id)。"""
    if not raw:
        return False, ""
    cfg_token = app_state["register_token"]
    s = app_state["storage"]
    row = s.token_by_hash(sha256(raw))
    if row:
        s.token_touch(row["id"], now())
        return True, row["id"]
    if cfg_token and _token_eq(raw, cfg_token):
        return True, ""
    return False, ""


def _node_allowed(s, n) -> bool:
    """节点绑定的 Token 被吊销/停用后，其同步与上报一并被拒。"""
    return s.token_active((n["token_id"] if "token_id" in n.keys() else "") or "")


def _peer_ip(request: Request) -> str:
    """服务端观测到的源 IP = 该节点的出口 IP（NAT 后即公网出口；同网段则等于本机 IP）。

    反代场景优先取 X-Forwarded-For 的第一跳。
    """
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def setup_router(app_state) -> APIRouter:
    s = app_state["storage"]
    router = APIRouter(prefix="/api/agent")  # 每次调用独立 router

    @router.post("/register")
    def register(body: RegisterIn, request: Request):
        ok, token_id = _match_token(app_state, body.register_token)
        if not ok:
            raise HTTPException(403, "注册 Token 无效或已吊销")
        nid, created = s.register_node(body.name, sha256(body.name + ":" + body.register_token),
                                       body.tags, body.version, body.system, now(),
                                       local_ip=body.local_ip, egress_ip=_peer_ip(request),
                                       token_id=token_id)
        return {"node_id": nid, "created": created, "server_time": now()}

    @router.post("/sync")
    def sync(body: HeartbeatIn, request: Request):
        n = s.node_by_id(body.node_id)
        if not n or not _token_eq(n["token_hash"], body.token):
            raise HTTPException(401, "节点未注册或凭据失效")
        if not _node_allowed(s, n):
            raise HTTPException(401, "该节点使用的注册 Token 已被吊销，请用新 Token 重新注册")
        ts = now()
        s.node_touch(body.node_id, body.stats or {}, ts, egress_ip=_peer_ip(request))
        cur_version = s.config_version()
        resp = {"server_time": ts, "config_version": cur_version, "heartbeat_interval":
                app_state["cfg"].agent.get("heartbeat_interval", 15)}
        if body.config_version != cur_version:
            # 节点分配过滤：nodes 为空 = 全部节点；否则匹配 节点id / 节点名 / 组（g:组id 或 g:组名）
            resp["tasks"] = s.tasks_for_node(body.node_id, n["name"])
        return resp

    @router.post("/results")
    def results(body: ResultsIn):
        n = s.node_by_id(body.node_id)
        if not n or not _token_eq(n["token_hash"], body.token):
            raise HTTPException(401, "节点未注册或凭据失效")
        if not _node_allowed(s, n):
            raise HTTPException(401, "该节点使用的注册 Token 已被吊销")
        return app_state["ingest"].accept(body)

    return router