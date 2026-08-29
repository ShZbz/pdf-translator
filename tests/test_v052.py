"""v0.5.2 验收单测：
- 术语表加载状态接口（GET /api/glossary/status）——UI 行内「已加载 · N 条术语」
- 批量导入来源清单（glossary_sources）随 /api/config 持久化往返——
  翻译加载的是合并文件，UI 需回显「哪几个文件被合并加载」
零网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _client():
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from server.app import app
    return fastapi_testclient.TestClient(app)


def test_status_ok_file(tmp_path):
    gl = tmp_path / "g.yaml"
    gl.write_text("Chern number: 陈数\nBerry curvature: 贝里曲率\n",
                  encoding="utf-8")
    with _client() as c:
        r = c.get("/api/glossary/status", params={"path": str(gl)})
        assert r.status_code == 200
        d = r.json()
        assert d == {"exists": True, "ok": True, "term_count": 2}


def test_status_missing_file(tmp_path):
    with _client() as c:
        d = c.get("/api/glossary/status",
                  params={"path": str(tmp_path / "nope.yaml")}).json()
        assert d["exists"] is False and d["ok"] is False


def test_status_empty_path():
    with _client() as c:
        d = c.get("/api/glossary/status").json()
        assert d["exists"] is False and d["term_count"] == 0


def test_status_invalid_yaml(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("good: 好\n  badIndent: x\n", encoding="utf-8")
    with _client() as c:
        d = c.get("/api/glossary/status", params={"path": str(bad)}).json()
        assert d["exists"] is True and d["ok"] is False and d["term_count"] == 0


def test_status_non_string_value(tmp_path):
    """数字/布尔译名是 error——ok=false 且不计入条目数（与保存校验一致）。"""
    f = tmp_path / "t.yaml"
    f.write_text("num_val: 123\nok: 正常\n", encoding="utf-8")
    with _client() as c:
        d = c.get("/api/glossary/status", params={"path": str(f)}).json()
        assert d["exists"] is True and d["ok"] is False
        assert d["term_count"] == 1   # 仅合法条目计入


def test_status_empty_file_is_ok(tmp_path):
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    with _client() as c:
        d = c.get("/api/glossary/status", params={"path": str(empty)}).json()
        assert d == {"exists": True, "ok": True, "term_count": 0}


def test_config_roundtrip_keeps_glossary_sources(tmp_path, monkeypatch):
    """glossary_sources（合并来源清单）PUT 后写盘、GET 原样读回。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import server.app as server_app
    cfg_path = tmp_path / "ui_config.yaml"
    monkeypatch.setattr(server_app, "UI_CONFIG_PATH", cfg_path)
    sources = [r"L:\gl\a.yaml", r"L:\gl\sub\b.yml"]
    payload = {"config": {
        "glossary_file": r"L:\gl\.ui_glossary_merged.yaml",
        "glossary_sources": sources,
        "io": {"source_lang": "en", "target_lang": "zh"},
        "llm": {"provider": "deepseek"},
    }}
    with _client() as c:
        assert c.put("/api/config", json=payload).status_code == 200
        assert cfg_path.exists()
        got = c.get("/api/config").json()["config"]
        assert got["glossary_sources"] == sources
        assert got["glossary_file"].endswith(".ui_glossary_merged.yaml")


def test_run_config_tolerates_glossary_sources(tmp_path):
    """translator Config 加载器忽略未知键——run config 带 glossary_sources 不炸。"""
    from translator.config import load_config
    import yaml
    run_cfg = tmp_path / "run.yaml"
    run_cfg.write_text(yaml.safe_dump({
        "io": {"input": "x.pdf", "output_dir": "out"},
        "glossary_file": "g.yaml",
        "glossary_sources": ["a.yaml", "b.yaml"],
    }, allow_unicode=True), encoding="utf-8")
    cfg = load_config(str(run_cfg))
    assert cfg.glossary_file == "g.yaml"
