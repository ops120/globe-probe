# -*- coding: utf-8 -*-
"""AI 分析 API 集成：降级路径（未配置/解析失败）+ mock 网关成功路径 + 锚定降级。

不做真实外部请求——LLM 网关用 monkeypatch 打桩（项目纪律：不依赖外部凭据）。
"""
from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server.app import create_app  # noqa: E402
from gpm.server import aiqa  # noqa: E402


def _client(tmp_path, ai_cfg=None):
    cfg = Config({"server": {"database": str(tmp_path / "t.db")}})
    cfg.raw["ai"] = ai_cfg or {"enabled": False, "url": "", "api_key": "", "model": ""}
    return TestClient(create_app(cfg))


def test_analyze_requires_question_or_range(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/ai/analyze", json={})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False and d["reason"] == "empty"


def test_analyze_time_unparsed_says_so(tmp_path):
    """解析不出时间必须明说（不猜默认窗口冒充用户意图）。"""
    with _client(tmp_path) as c:
        r = c.post("/api/ai/analyze", json={"question": "系统怎么样"})
        d = r.json()
        assert d["ok"] is False and d["reason"] == "time_unparsed"
        assert "时间" in d["hint"]


def test_analyze_degraded_when_disabled(tmp_path):
    """未配置网关 → degraded=disabled，结构化事实照给（不装 AI）。"""
    with _client(tmp_path) as c:
        r = c.post("/api/ai/analyze", json={"question": "最近 1 小时", "t_from": 1, "t_to": 2})
        d = r.json()
        assert d["ok"] is True and d["degraded"] == "disabled"
        assert "facts" in d and d["answer"] == ""


def test_analyze_llm_success_path(tmp_path, monkeypatch):
    """mock 网关：正常回答（全部锚定）→ degraded=None + answer 透出。"""
    cfg_ai = {"enabled": True, "url": "https://gw.example/v1", "api_key": "k", "model": "m"}
    c = _client(tmp_path, cfg_ai)

    def fake_llm(question, facts, cfg):
        # 用事实里真实存在的任务名组一段回答（锚定必过）。
        # 注意 facts.get("total", 0) 在值为 None 时不取默认——空库实测踩坑：
        # %d 格式化 None 抛 TypeError，会被 API 层当「网关错误」降级。
        total = facts.get("total") or 0
        names = [i.get("title") or "" for i in facts.get("incidents") or []]
        return "该时段共 %d 起故障：%s。未发现可证明的关联。" % (total, "、".join(names[:2]))

    monkeypatch.setattr(aiqa, "call_llm", fake_llm)
    r = c.post("/api/ai/analyze", json={"question": "最近 1 小时", "t_from": 1, "t_to": 2})
    d = r.json()
    assert d["ok"] is True and d["degraded"] is None
    assert d["answer"]


def test_analyze_unanchored_degrades(tmp_path, monkeypatch):
    """mock 网关返回幻觉实体 → degraded=unanchored + 提示含未锚定词。"""
    cfg_ai = {"enabled": True, "url": "https://gw.example/v1", "api_key": "k", "model": "m"}
    c = _client(tmp_path, cfg_ai)

    def fake_llm(question, facts, cfg):
        return "淘宝网 www.taobao.com 也在该时段故障。"      # 事实里没有

    monkeypatch.setattr(aiqa, "call_llm", fake_llm)
    r = c.post("/api/ai/analyze", json={"question": "最近 1 小时", "t_from": 1, "t_to": 2})
    d = r.json()
    assert d["degraded"] == "unanchored"
    assert d.get("unanchored")


def test_analyze_llm_error_degrades(tmp_path, monkeypatch):
    """网关抛错 → degraded=error + 原因透出，事实照给。"""
    cfg_ai = {"enabled": True, "url": "https://gw.example/v1", "api_key": "k", "model": "m"}
    c = _client(tmp_path, cfg_ai)

    def fake_llm(question, facts, cfg):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(aiqa, "call_llm", fake_llm)
    r = c.post("/api/ai/analyze", json={"question": "最近 1 小时", "t_from": 1, "t_to": 2})
    d = r.json()
    assert d["degraded"] == "error" and "timeout" in d["hint"]
    assert d["facts"]
