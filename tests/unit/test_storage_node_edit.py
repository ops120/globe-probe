"""storage 节点编辑单测（update_node / delete_node）：

- update_node：改名成功（updated_at 更新、DB 行与返回值同步变化）；
  改名为另一已存在节点名 → ValueError("节点名已存在")，且不产生部分更新；
  不存在的 node_id → KeyError；只改 tags（tags_json 更新、返回行 tags 解析为 dict）；
  传入 name 与现名相同 → 不触发重名检查，正常返回；空 fields dict → 不执行
  UPDATE（updated_at 不变）但仍返回完整行；返回值是 dict 且已把 tags_json
  替换为 tags（不含 tags_json 键）。
- delete_node：不存在的 node_id → KeyError；级联清理（probe_results /
  aggregates / node_heartbeats / incidents / nodes 各表中该节点数据全没了，
  其他节点数据不受影响）；从任务 nodes 分配列表移除该节点且任务与全局
  config_version 增加；返回值是被删节点的原始信息（dict）；删除后
  node_by_id 返回 None；重复删除第二次 → KeyError。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server.storage import Storage

T0 = 1_700_000_000


def make_storage(tmp_path):
    s = Storage(str(tmp_path / "node_edit.db"))
    return s, T0


def _register(s, name, ts=T0, tags=None):
    nid, _ = s.register_node(name, "h", tags or {"region": "cn"}, "v1", {}, ts)
    return nid


def _count(s, table, node_id):
    return s.db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE node_id=?", (node_id,)).fetchone()[0]


# ---------------- update_node：改名 ----------------

def test_update_node_rename_success(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    d = s.update_node(nid, {"name": "华南-1"}, t0 + 60)
    assert d["name"] == "华南-1"
    assert d["updated_at"] == t0 + 60                      # updated_at 随更新刷新
    row = s.node_by_id(nid)
    assert row["name"] == "华南-1" and row["updated_at"] == t0 + 60


def test_update_node_rename_to_existing_raises(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    _register(s, "上海")
    with pytest.raises(ValueError, match="节点名已存在"):
        s.update_node(nid, {"name": "上海"}, t0 + 60)
    assert s.node_by_id(nid)["name"] == "北京"              # 失败时不产生部分更新


def test_update_node_missing_id_raises(tmp_path):
    s, t0 = make_storage(tmp_path)
    with pytest.raises(KeyError):
        s.update_node("n-no-such-node", {"name": "x"}, t0)


# ---------------- update_node：tags / 边界 ----------------

def test_update_node_tags_only(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京", tags={"region": "cn"})
    d = s.update_node(nid, {"tags_json": json.dumps({"region": "eu", "rack": "r7"})}, t0 + 60)
    assert d["tags"] == {"region": "eu", "rack": "r7"}     # 返回行 tags 已解析为 dict
    assert d["name"] == "北京"                              # name 不受影响
    row = s.node_by_id(nid)
    assert json.loads(row["tags_json"]) == {"region": "eu", "rack": "r7"}


def test_update_node_same_name_ok(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    other = _register(s, "上海")
    # name 与现名相同 → 不触发重名检查，正常返回
    d = s.update_node(nid, {"name": "北京"}, t0 + 60)
    assert d["name"] == "北京"
    assert s.node_by_id(nid)["name"] == "北京"
    assert s.node_by_id(other) is not None


def test_update_node_empty_fields_returns_full_row(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京", tags={"region": "cn"})
    d = s.update_node(nid, {}, t0 + 60)
    assert d["id"] == nid
    assert d["name"] == "北京"
    assert d["tags"] == {"region": "cn"}
    assert d["updated_at"] == t0                           # 无字段可更新 → 未执行 UPDATE
    assert "tags_json" not in d


def test_update_node_returns_dict_without_tags_json(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    d = s.update_node(nid, {"name": "华东-1", "tags_json": json.dumps({"a": 1})}, t0 + 60)
    assert isinstance(d, dict)
    assert "tags_json" not in d                            # 已替换为 tags
    assert isinstance(d["tags"], dict) and d["tags"] == {"a": 1}
    assert d["id"] == nid and d["name"] == "华东-1"


# ---------------- delete_node ----------------

def test_delete_node_missing_id_raises(tmp_path):
    s, t0 = make_storage(tmp_path)
    with pytest.raises(KeyError):
        s.delete_node("n-no-such-node", t0)


def test_delete_node_cascade_cleanup(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    other = _register(s, "上海")
    s.create_task("t1", "curl-目标", "curl", "https://example.com", ["https://example.com"],
                  {}, [], 30, t0, nodes=[nid])
    # 造数据：probe_results（insert_results）、aggregates（直插）、
    # node_heartbeats（node_touch）、incidents（incident_open）
    s.insert_results([
        {"ts": t0 + 10, "task_id": "t1", "node_id": nid, "type": "curl", "status": "ok",
         "metrics": {}},
        {"ts": t0 + 10, "task_id": "t1", "node_id": other, "type": "curl", "status": "ok",
         "metrics": {}}], t0 + 10)
    s.db.execute("INSERT INTO aggregates(bucket,ts,task_id,node_id,count,ok,fail)"
                 " VALUES('1m',?,'t1',?,1,1,0)", (t0 + 60, nid))
    s.db.commit()
    s.node_touch(nid, {"cpu": 12.5, "mem": 40.0, "tasks": 1}, t0 + 20)
    s.node_touch(other, {"cpu": 30.0, "mem": 50.0, "tasks": 1}, t0 + 20)
    s.incident_open("t1", nid, "", "https://example.com", t0 + 30, {"event": "fail"})
    # 前置：各表确实各有 1 行该节点数据（防空验证）
    assert _count(s, "probe_results", nid) == 1
    assert _count(s, "aggregates", nid) == 1
    assert _count(s, "node_heartbeats", nid) == 1
    assert _count(s, "incidents", nid) == 1
    info = s.delete_node(nid, t0 + 100)
    assert info["id"] == nid and info["name"] == "北京"
    # 级联清理：该节点在 5 张表中的数据全没了（nodes 表主键列是 id）
    for table, col in (("probe_results", "node_id"), ("aggregates", "node_id"),
                       ("node_heartbeats", "node_id"), ("incidents", "node_id"),
                       ("nodes", "id")):
        n = s.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (nid,)).fetchone()[0]
        assert n == 0, table
    # 其他节点的数据不受影响
    assert _count(s, "probe_results", other) == 1
    assert _count(s, "node_heartbeats", other) == 1
    assert s.node_by_id(other) is not None


def test_delete_node_removes_node_from_task_and_bumps_config_version(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    s.create_task("t1", "curl-目标", "curl", "https://example.com", ["https://example.com"],
                  {}, [], 30, t0, nodes=[nid])
    t_before = s.get_task("t1")
    assert t_before["nodes"] == [nid]
    cv_before = s.config_version()
    s.delete_node(nid, t0 + 100)
    t_after = s.get_task("t1")
    assert t_after["nodes"] == []                          # 已从任务分配列表移除
    assert t_after["config_version"] == t_before["config_version"] + 1
    assert s.config_version() > cv_before                  # 全局 config_version 同步增加


def test_delete_node_returns_original_info(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京", tags={"region": "cn"})
    info = s.delete_node(nid, t0 + 100)
    assert isinstance(info, dict)
    assert info["id"] == nid
    assert info["name"] == "北京"
    assert info["created_at"] == t0


def test_delete_node_after_delete_gone_and_repeat_raises(tmp_path):
    s, t0 = make_storage(tmp_path)
    nid = _register(s, "北京")
    s.delete_node(nid, t0 + 10)
    assert s.node_by_id(nid) is None
    with pytest.raises(KeyError):
        s.delete_node(nid, t0 + 20)                        # 重复删除
