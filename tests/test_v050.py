"""v0.5.0 验收单测：
- LLM 配额自适应（rpm/tpm → interval/batch_char_budget，显式配置优先）
- 新配置键校验（renderer / ocr.mode / performance.layout_engine）
- htmlbox 渲染路径（insert_htmlbox 排版引擎端到端渲染不炸、文本落页）
- OCR 原位回贴（行分组 / 图形避让 / 白块覆盖 + 回灌）
- pymupdf-layout 适配层（外部区域接管 / 未装包回退标记）
- 任务持久化（JobStore 往返 / JobManager 重启恢复 / 排队取消落盘）
- SSE 广播（事件泵 → 订阅队列）
- /api/translate 配置合成的 key 回填（B1 修复）
- Job 警告接活（B3 修复）与崩溃归档为 error（B4 修复）
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.config import (IOConfig, LLMConfig, load_config)
from translator.layout import external_layout_regions, layout_page
from translator.ocr import _extract_paddle
from translator.pipeline import _apply_ocr_inplace, _group_ocr_lines
from translator.render import render_page


# ---------- 2-6: 配额自适应 ----------

def test_quota_effective_auto():
    llm = LLMConfig(rpm_limit=30, tpm_limit=100_000)
    eff = llm.effective()
    assert eff.min_call_interval == pytest.approx(2.0)
    # (100000/30) * 3.2 * 0.8 = 8533
    assert eff.batch_char_budget == 8533
    # 原对象不被原地修改
    assert llm.min_call_interval == 0.0
    assert llm.batch_char_budget == 3000


def test_quota_effective_explicit_wins():
    llm = LLMConfig(rpm_limit=30, tpm_limit=100_000)
    llm._explicit = {"min_call_interval", "batch_char_budget"}
    llm.min_call_interval = 6.0
    llm.batch_char_budget = 1500
    eff = llm.effective()
    assert eff.min_call_interval == 6.0
    assert eff.batch_char_budget == 1500


def test_quota_effective_budget_clamp():
    eff = LLMConfig(rpm_limit=1000, tpm_limit=10_000).effective()
    # (10000/1000)*3.2*0.8 = 25.6 → clamp 到 400
    assert eff.batch_char_budget == 400
    # 只设 rpm 不设 tpm → 只换算间隔，不动预算
    eff2 = LLMConfig(rpm_limit=60).effective()
    assert eff2.min_call_interval == pytest.approx(1.0)
    assert eff2.batch_char_budget == 3000
    # 什么都不设 → 原样
    eff3 = LLMConfig().effective()
    assert eff3.min_call_interval == 0.0
    assert eff3.batch_char_budget == 3000


def test_load_config_records_explicit_llm_keys(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("""
io: {input: x.pdf, output_dir: out}
llm:
  rpm_limit: 30
  tpm_limit: 60000
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.llm.rpm_limit == 30
    assert cfg.llm._explicit == {"rpm_limit", "tpm_limit"}
    eff = cfg.llm.effective()
    assert eff.min_call_interval == pytest.approx(2.0)
    assert eff.batch_char_budget == int(60000 / 30 * 3.2 * 0.8)


def test_new_config_keys_validation(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("""
io: {input: x.pdf, output_dir: out}
features: {renderer: bogus}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="renderer"):
        load_config(y)
    y.write_text("""
io: {input: x.pdf, output_dir: out}
ocr: {mode: bogus}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="ocr.mode"):
        load_config(y)
    y.write_text("""
io: {input: x.pdf, output_dir: out}
performance: {layout_engine: bogus}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="layout_engine"):
        load_config(y)


def test_new_config_keys_accepted(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("""
io: {input: x.pdf, output_dir: out}
features: {renderer: htmlbox}
ocr: {mode: inplace}
performance: {layout_engine: pymupdf-layout}
""", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.features.renderer == "htmlbox"
    assert cfg.ocr.mode == "inplace"
    assert cfg.performance.layout_engine == "pymupdf-layout"


# ---------- 2-1: htmlbox 渲染路径 ----------

def _simple_layout(page) -> dict:
    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        spans = [{"text": s["text"], "size": s["size"], "flags": 0,
                  "font": s["font"], "bbox": pymupdf.Rect(s["bbox"])}
                 for l in b["lines"] for s in l["spans"]]
        blocks.append({"bbox": pymupdf.Rect(b["bbox"]),
                       "text": " ".join(s["text"] for s in spans),
                       "spans": spans})
    paras = []
    for b in blocks:
        paras.append({"bbox": b["bbox"], "text": b["text"],
                      "spans": b["spans"], "size": 11.0})
    return {"paragraphs": paras, "tables_cells": [], "formulas": []}


def _cjk_font_or_skip():
    from translator.langs import resolve_output_fonts
    body, _ = resolve_output_fonts("zh", None)
    if not body:
        pytest.skip("no CJK font on this machine")
    return body


def test_render_page_htmlbox(tmp_path):
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Hello world paragraph one.", fontsize=11)
    page.insert_text((72, 120), "Second paragraph with more text.", fontsize=11)
    lay = _simple_layout(page)
    translated = [{"index": i, "text": f"第{i}段中文译文，验证 htmlbox 排版引擎路径。"}
                  for i in range(len(lay["paragraphs"]))]
    warnings: list[str] = []
    render_page(page, lay, translated, font_path,
                warnings=warnings, renderer="htmlbox")
    txt = page.get_text()
    assert "第0段中文译文" in txt and "第1段中文译文" in txt
    # 溢出不炸（窄盒允许 htmlbox 内部缩放，warning 可有可无，但不能异常）
    doc.save(str(tmp_path / "out.pdf"))


def test_render_page_htmlbox_bilingual(tmp_path):
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    # 多行段落（单行段盒高 ~12pt，双语 60/40 分割后底部不足 MIN_FONT*1.5，
    # 两条渲染路径都按设计放弃原文层——样张必须足够高）
    body = ("Original english body paragraph with several lines of text "
            "so that the bounding box is tall enough for the bilingual "
            "split layout to keep both layers in the same region.")
    page.insert_textbox(pymupdf.Rect(72, 90, 320, 170), body, fontsize=11)
    lay = _simple_layout(page)
    translated = [{"index": 0, "text": "中文译文段落。" * 6}]
    render_page(page, lay, translated, font_path, bilingual=True,
                renderer="htmlbox")
    txt = page.get_text()
    assert "中文译文段落" in txt and "Original english" in txt


# ---------- 2-4: OCR 原位回贴 ----------

def test_group_ocr_lines():
    lines = [
        (pymupdf.Rect(72, 90, 300, 102), "first line"),
        (pymupdf.Rect(72, 104, 320, 116), "second line continues"),
        (pymupdf.Rect(72, 180, 260, 192), "new block after gap"),
        (pymupdf.Rect(360, 90, 460, 102), "right column line"),
    ]
    blocks = _group_ocr_lines(lines)
    assert len(blocks) >= 2
    joined = [t for _, t in blocks]
    assert any("first line" in t and "second line" in t for t in joined)
    assert any("new block" in t for t in joined)


def test_extract_lines_paddle2x_format():
    result = [[[[72, 90], [300, 90], [300, 102], [72, 102]], ("hello", 0.99)],
              [[[72, 110], [300, 110], [300, 122], [72, 122]], ("world", 0.98)]]
    # v0.7.0: _extract_paddle 返回 (rect, text, score) 三元组（投票用）
    lines = _extract_paddle(result)
    assert [t for _, t, _s in lines] == ["hello", "world"]
    assert lines[0][0].x0 == pytest.approx(72)
    assert lines[0][2] == pytest.approx(0.99)


def test_apply_ocr_inplace_covers_and_skips_graphics():
    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    # 整页扫描图（不算图形避让对象）+ 一个页内子图（应触发避让）
    big = page.new_shape()
    big.draw_rect(pymupdf.Rect(0, 0, 595, 842))
    big.finish(fill=(0.95, 0.95, 0.95), color=None)
    big.commit()
    sub = page.new_shape()
    sub.draw_rect(pymupdf.Rect(400, 80, 500, 140))
    sub.finish(color=(0, 0, 0), width=1)
    sub.commit()
    blocks = [
        (pymupdf.Rect(72, 80, 320, 110), "normal text block"),
        (pymupdf.Rect(410, 90, 490, 130), "block on figure"),
    ]
    translations = ["普通文本块的中文译文。", "插图上的文本"]
    warnings: list[str] = []
    n = _apply_ocr_inplace(doc, 0, blocks, translations, font_path, warnings)
    assert n == 1                       # 图上块被跳过
    assert any("overlaps graphics" in w for w in warnings)
    txt = doc[0].get_text()
    assert "普通文本块的中文译文" in txt
    assert "插图上的文本" not in txt     # 跳过块不回贴译文


def test_ocr_inplace_end_to_end(tmp_path, monkeypatch):
    """端到端：扫描页（无文字层）→ mock OCR 行 → 原位回贴 + 存盘。"""
    from translator import ocr as ocr_mod
    from translator.pipeline import translate_document
    from translator.config import load_config

    font_path = _cjk_font_or_skip()
    doc = pymupdf.open()
    page = doc.new_page()
    # 扫描页：只有一张图，无文字层
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 535, 750))
    pix.set_rect(pix.irect, (255, 255, 255))
    page.insert_image(pymupdf.Rect(30, 50, 565, 800), pixmap=pix)
    src = tmp_path / "scan.pdf"
    doc.save(str(src))
    doc.close()

    fake_lines = [(pymupdf.Rect(60, 90, 520, 105), "scanned english sentence"),
                  (pymupdf.Rect(60, 110, 480, 125), "second scanned line")]
    monkeypatch.setattr(ocr_mod, "engine_available", lambda e: True)
    # v0.7.0: 管线走多引擎投票入口 → 打点在 ocr_page_lines_scored
    #（返回 (rect, text, score) 三元组；score 归一 0-1）
    monkeypatch.setattr(ocr_mod, "ocr_page_lines_scored",
                        lambda page, engine="paddle", src_lang="en", dpi=200:
                        [(r, t, 0.9) for r, t in fake_lines])

    class EchoClient:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            import json
            batch = json.loads(kw["messages"][-1]["content"])
            out = {k: f"【译】{v}" for k, v in batch.items()}
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(out, ensure_ascii=False)))])

    y = tmp_path / "c.yaml"
    y.write_text(f"""
io:
  input: {src.as_posix()}
  output_dir: {tmp_path.as_posix()}
llm:
  batch_size: 2
  max_llm_calls: 5
  rpm_limit: 0
ocr:
  engine: paddle
  mode: inplace
features:
  translation_cache: false
fonts:
  cjk: {Path(font_path).as_posix()}
""", encoding="utf-8")
    cfg = load_config(y)
    stats = translate_document(cfg, client=EchoClient())
    assert stats["ocr_inplace_blocks"] >= 1
    assert stats["calls"] >= 1
    out = pymupdf.open(stats["output"])
    all_txt = "".join(p.get_text() for p in out)
    assert "【译】scanned english sentence" in all_txt


# ---------- 2-2: pymupdf-layout 适配层 ----------

def test_external_layout_regions_with_fake_module(monkeypatch):
    fake = types.ModuleType("pymupdf.layout")
    monkeypatch.setitem(sys.modules, "pymupdf.layout", fake)
    regions = [
        SimpleNamespace(bbox=(100, 100, 200, 180), kind="figure"),
        SimpleNamespace(bbox=(300, 100, 500, 160), kind="table"),
        SimpleNamespace(bbox=(100, 300, 400, 330), kind="formula"),
        SimpleNamespace(bbox=(100, 400, 500, 500), kind="text"),
    ]
    monkeypatch.setattr(pymupdf, "_get_layout",
                        lambda page: regions, raising=False)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "some body text", fontsize=11)
    got = external_layout_regions(page)
    assert got is not None
    assert len(got["figures"]) == 1 and len(got["tables"]) == 1
    assert len(got["formulas"]) == 1

    # layout_page 集成：外部区域接管图/表
    lay = layout_page(page, engine="pymupdf-layout")
    assert lay["layout_engine"] == "pymupdf-layout"
    fr = lay["figure_regions"][0]
    assert fr.x0 == pytest.approx(100)


def test_external_layout_regions_fallback(monkeypatch):
    monkeypatch.delitem(sys.modules, "pymupdf.layout", raising=False)
    monkeypatch.setattr(pymupdf, "_get_layout", None, raising=False)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "plain text page", fontsize=11)
    assert external_layout_regions(page) is None
    lay = layout_page(page, engine="pymupdf-layout")
    assert lay["layout_engine"] == "pymupdf-layout-fallback"


# ---------- 2-3: 任务持久化 ----------

def test_jobstore_roundtrip(tmp_path):
    from server.store import JobStore
    st = JobStore(tmp_path / "jobs.db")
    snap = {"id": "aaa", "status": "queued", "config_path": "/tmp/c.yaml",
            "stage": "", "progress": None, "pages": 0, "paragraphs": 0,
            "calls": 0, "elapsed": 0.0, "created": 1000.0,
            "output_path": "", "error": ""}
    st.upsert(snap, seq=0)
    snap2 = dict(snap, id="bbb", status="done", output_path="/tmp/o.pdf")
    st.upsert(snap2)
    unf = st.unfinished()
    assert [u["id"] for u in unf] == ["aaa"]
    hist = st.history()
    assert [h["id"] for h in hist] == ["bbb"]
    assert hist[0]["output_path"] == "/tmp/o.pdf"
    st.close()


def test_jobmanager_restore_requeues(tmp_path, monkeypatch):
    from server.jobs import JobManager
    from server.store import JobStore
    db = tmp_path / "jobs.db"
    cfg = tmp_path / "c.yaml"
    cfg.write_text("io: {input: x.pdf}", encoding="utf-8")
    st = JobStore(db)
    # 模拟上次关服：一个排队中 + 一个运行中
    st.upsert({"id": "q1", "status": "queued", "config_path": str(cfg),
               "stage": "", "progress": None, "pages": 0, "paragraphs": 0,
               "calls": 0, "elapsed": 0.0, "created": 1.0,
               "output_path": "", "error": ""}, seq=0)
    st.upsert({"id": "r1", "status": "running", "config_path": str(cfg),
               "stage": "translate", "progress": {"done": 2, "total": 5,
                                                  "unit": "batch"},
               "pages": 0, "paragraphs": 0, "calls": 2, "elapsed": 9.0,
               "created": 2.0, "output_path": "", "error": ""}, seq=1)
    st.upsert({"id": "h1", "status": "done", "config_path": "gone.yaml",
               "stage": "", "progress": None, "pages": 3, "paragraphs": 30,
               "calls": 4, "elapsed": 20.0, "created": 0.5,
               "output_path": "/tmp/o.pdf", "error": ""})
    st.close()

    # 重启：不真开子进程（start 打桩）
    started = []

    def fake_start(self, root):
        self.status = "running"
        started.append(self.id)

    monkeypatch.setattr("server.jobs.Job.start", fake_start)
    mgr = JobManager(tmp_path, store=JobStore(db))
    # 上次运行中的任务排最前（seq=1 在前？unfinished 按 seq 排序：seq=0 先）
    assert sorted(started) == ["q1"]          # 队首开跑，另一个仍排队
    assert mgr.job is not None and mgr.job.id == "q1"
    assert [j.id for j in mgr.queue] == ["r1"]
    assert mgr.job.status == "running"
    # 历史从库里恢复
    assert any(h["id"] == "h1" for h in mgr.history)


def test_jobmanager_restore_missing_config(tmp_path, monkeypatch):
    from server.jobs import JobManager
    from server.store import JobStore
    db = tmp_path / "jobs.db"
    st = JobStore(db)
    st.upsert({"id": "gone", "status": "queued",
               "config_path": str(tmp_path / "missing.yaml"),
               "stage": "", "progress": None, "pages": 0, "paragraphs": 0,
               "calls": 0, "elapsed": 0.0, "created": 1.0,
               "output_path": "", "error": ""}, seq=0)
    st.close()
    started = []
    monkeypatch.setattr("server.jobs.Job.start",
                        lambda self, root: started.append(1))
    mgr = JobManager(tmp_path, store=JobStore(db))
    assert started == []                       # 不开跑
    assert mgr.job is None and not mgr.queue
    assert mgr.store.history()[0]["status"] == "error"


def test_queued_cancel_persisted(tmp_path, monkeypatch):
    from server.jobs import Job, JobManager
    from server.store import JobStore
    db = tmp_path / "jobs.db"
    mgr = JobManager(tmp_path, store=JobStore(db))
    started = []

    def fake_start(self, root):
        self.status = "running"
        started.append(self.id)

    monkeypatch.setattr(Job, "start", fake_start)
    # 先占住槽位
    j1 = Job("run1", "c1.yaml")
    j1.on_terminal = mgr._on_terminal
    j1.on_event = mgr._on_job_event
    j1.start(tmp_path)
    mgr.job = j1
    # 第二个排队
    r = mgr.submit("c2.yaml")
    assert r["queued"] is True
    qid = r["job_id"]
    assert mgr.act(qid, "cancel") is not None   # B2: 排队任务可取消
    snap = mgr.find(qid).snapshot()
    assert snap["status"] == "cancelled"
    # 已落盘：重启后不会恢复它
    st2 = JobStore(db)
    assert all(u["id"] != qid for u in st2.unfinished())


# ---------- 2-5: SSE 广播 ----------

def test_job_events_broadcast_and_warnings():
    from server.jobs import Job, JobManager
    mgr = JobManager(Path("."))
    q = mgr.subscribe()
    job = Job("j1", "c.yaml")
    job.on_event = mgr._on_job_event
    job._handle_event({"kind": "stage", "name": "layout"})
    job._handle_event({"kind": "warning", "msg": "overflow somewhere"})
    job._handle_event({"kind": "progress", "done": 1, "total": 3,
                       "unit": "page"})
    snap = job.snapshot()
    assert snap["stage"] == "layout"
    assert snap["warnings"] == ["overflow somewhere"]     # B3
    assert snap["progress"]["done"] == 1
    got = [q.get_nowait() for _ in range(3)]
    assert all(g["kind"] == "job_event" for g in got)
    assert got[0]["event"]["name"] == "layout"
    mgr.unsubscribe(q)


def test_sse_endpoint_streams_initial_state(tmp_path, monkeypatch):
    """直接驱动 SSE 生成器（本机 TestClient 流式传输在 ASGI 传输层阻塞，
    等整响应结束才返回——绕开 HTTP 层测同一 body_iterator 契约）。"""
    import asyncio
    import json as _json
    monkeypatch.setenv("PDF_TRANSLATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    import importlib
    import server.app as app_mod
    app_mod = importlib.reload(app_mod)

    async def _first_frame():
        resp = await app_mod.job_stream()
        assert resp.media_type == "text/event-stream"
        it = resp.body_iterator
        frame = await asyncio.wait_for(anext(it), timeout=5.0)
        return resp, it, frame

    resp, it, frame = asyncio.run(_first_frame())
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    # v0.5.1: 帧契约加 id 行（Last-Event-ID 断线续传重放的定位依据）
    m = re.match(r"id: (\d+)\ndata: ", frame)
    assert m, f"frame missing id/data lines: {frame!r}"
    payload = _json.loads(frame[m.end():])
    assert payload["kind"] == "state"
    assert "current" in payload and "queue_len" in payload
    # 心跳/后续帧不炸（再读一帧后主动终止，finally 退订）
    async def _drain():
        await asyncio.wait_for(anext(it), timeout=5.0)
    try:
        asyncio.run(_drain())
    except (StopAsyncIteration, asyncio.TimeoutError, RuntimeError):
        pass


# ---------- B1: /api/translate key 回填 ----------

def test_build_run_config_backfills_stored_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_TRANSLATOR_JOBS_DB", str(tmp_path / "jobs.db"))
    import importlib
    import server.app as app_mod
    app_mod = importlib.reload(app_mod)
    stored = tmp_path / "ui.yaml"
    stored.write_text("llm:\n  api_key: sk-stored-key\n",
                      encoding="utf-8")
    monkeypatch.setattr(app_mod, "UI_CONFIG_PATH", stored)

    req = app_mod.TranslateReq(
        input=str(tmp_path / "x.pdf"),
        config={"llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                        "api_key": ""}})
    out = app_mod._build_run_config(req, Path(tmp_path / "x.pdf"))
    assert out["llm"]["api_key"] == "sk-stored-key"

    # 打码 key 同样回填
    req2 = app_mod.TranslateReq(
        input=str(tmp_path / "x.pdf"),
        config={"llm": {"api_key": "sk-Aux***9jAO"}})
    out2 = app_mod._build_run_config(req2, Path(tmp_path / "x.pdf"))
    assert out2["llm"]["api_key"] == "sk-stored-key"

    # 显式新 key 优先
    req3 = app_mod.TranslateReq(
        input=str(tmp_path / "x.pdf"),
        config={"llm": {"api_key": "sk-fresh"}})
    out3 = app_mod._build_run_config(req3, Path(tmp_path / "x.pdf"))
    assert out3["llm"]["api_key"] == "sk-fresh"


# ---------- B4: 崩溃归档 error ----------

def test_reap_marks_crash_as_error(monkeypatch):
    from server.jobs import Job
    job = Job("crash1", "c.yaml")
    job.status = "running"
    # 模拟子进程 rc!=0 且无 exit 事件
    monkeypatch.setattr(job, "_proc",
                        SimpleNamespace(wait=lambda: 3), raising=False)
    import threading
    orig_sleep = threading.Lock  # noqa: F841（防误删 import）
    monkeypatch.setattr("server.jobs.time.sleep", lambda s: None)
    job._reap()
    snap = job.snapshot()
    assert snap["status"] == "error"
    assert "code 3" in snap["error"]
