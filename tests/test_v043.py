"""v0.4.3 验收单测：
- LLM 批字符预算组批（_pack_batches + 端到端调用切分）
- 翻译缓存容量上限淘汰
- 表格单元格译文缺失时原文回灌（dry-run 丢字回归）
- 扫描页惰性 OCR：引擎缺失警告 / 可用附录页（monkeypatch 注入）
- 布局多进程并行与串行结果一致性
- JobManager 任务队列 + history 归档
- 配置未知键容错
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.cache import TranslationCache
from translator.config import (Config, FeatureConfig, IOConfig, LLMConfig,
                               PerformanceConfig, load_config)
from translator.llm import TranslationClient
from translator.render import render_page


class EchoLLM:
    """记录每次调用批次并回显译文的假 client（id → 【译】+原文前缀）。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        batch = json.loads(kwargs["messages"][-1]["content"])
        self.calls.append(batch)
        out = {k: f"【译】{v[:20]}" for k, v in batch.items()}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(out, ensure_ascii=False)))])


def _make_pdf(pages_text: list[str], tmp_path: Path) -> Path:
    doc = pymupdf.open()
    for txt in pages_text:
        page = doc.new_page()
        if txt:
            page.insert_text((72, 90), txt, fontsize=11)
    p = tmp_path / "t.pdf"
    doc.save(str(p))
    doc.close()
    return p


def _cfg(src: Path, tmp_path: Path, **llm_kw) -> Config:
    return Config(
        io=IOConfig(input=str(src), output_dir=str(tmp_path / "out")),
        llm=LLMConfig(**llm_kw),
        features=FeatureConfig(watermark_removal=False,
                               preserve_formatting=False,
                               translation_cache=False),
        performance=PerformanceConfig(layout_workers=1),
    )


# ---------- 1. 批字符预算组批 ----------

def test_pack_batches_char_budget():
    tc = TranslationClient(EchoLLM(), model="m", batch_size=10,
                           batch_char_budget=30)
    paras = ["x" * 20, "y" * 20, "z" * 5]
    assert tc._pack_batches([0, 1, 2], paras) == [[0], [1, 2]]


def test_pack_batches_long_para_alone():
    """单段超预算 → 独占一批（段落原子，不拆）。"""
    tc = TranslationClient(EchoLLM(), model="m", batch_size=10,
                           batch_char_budget=10)
    paras = ["a" * 40, "b"]
    assert tc._pack_batches([0, 1], paras) == [[0], [1]]


def test_pack_batches_zero_budget_is_count_mode():
    """budget=0 → v0.4.2 纯段数行为。"""
    tc = TranslationClient(EchoLLM(), model="m", batch_size=2,
                           batch_char_budget=0)
    paras = ["a" * 100, "b" * 100, "c" * 100]
    assert tc._pack_batches([0, 1, 2], paras) == [[0, 1], [2]]


def test_pack_batches_size_cap_still_applies():
    """字符预算内但段数达 batch_size 上限 → 开新批（上限语义保留）。"""
    tc = TranslationClient(EchoLLM(), model="m", batch_size=2,
                           batch_char_budget=3000)
    paras = ["a", "b", "c"]
    assert tc._pack_batches([0, 1, 2], paras) == [[0, 1], [2]]


def test_translate_paragraphs_char_budget_splits_calls():
    fake = EchoLLM()
    tc = TranslationClient(fake, model="m", batch_size=10,
                           batch_char_budget=30, max_llm_calls=10)
    paras = ["x" * 20, "y" * 20, "z" * 5]
    out, calls = tc.translate_paragraphs(paras)
    assert calls == 2
    assert [len(c) for c in fake.calls] == [1, 2]   # 2 段/1 段切分
    assert all(o.startswith("【译】") for o in out)


# ---------- 2. 缓存容量上限 ----------

def test_cache_max_entries_prune(tmp_path):
    cache = TranslationCache(tmp_path / "c.db", max_entries=5)
    for i in range(20):
        cache.put(f"k{i}", f"s{i}", f"d{i}")
    cache.prune()
    assert cache.count() == 5
    # 淘汰的是最旧条目
    assert cache.get("k0") is None
    assert cache.get("k19") == "d19"


def test_cache_unlimited_when_zero(tmp_path):
    cache = TranslationCache(tmp_path / "c.db", max_entries=0)
    for i in range(10):
        cache.put(f"k{i}", "s", "d")
    cache.prune()
    assert cache.count() == 10


# ---------- 3. 单元格译文缺失回灌（dry-run 丢字回归） ----------

def test_render_cell_fallback_keeps_original_text(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 110), "Alpha", fontsize=10)
    layout = {
        "paragraphs": [],
        "tables_cells": [{"bbox": pymupdf.Rect(90, 95, 200, 125),
                          "text": "Alpha"}],
        "formulas": [],
    }
    render_page(page, layout, [], font_path="")   # cell_texts=None（dry-run）
    assert "Alpha" in page.get_text()


# ---------- 4. 扫描页惰性 OCR ----------

BODY = ("This page has a normal text layer with plenty of characters, "
        "well above the fifty character threshold used to detect scanned "
        "pages in the OCR fallback path.")


def test_ocr_uninstalled_warns_and_keeps_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("translator.ocr.engine_available", lambda e: False)
    src = _make_pdf([BODY, "", BODY], tmp_path)     # 中间页扫描页候选
    cfg = _cfg(src, tmp_path)
    from translator.pipeline import translate_document
    stats = translate_document(cfg, client=EchoLLM())
    joined = " ".join(stats["warnings"])
    assert "paddleocr" in joined and "p.2" in joined
    out = pymupdf.open(stats["output"])
    assert len(out) == 3                          # 无附录页
    out.close()


def test_ocr_appends_translation_page(tmp_path, monkeypatch):
    monkeypatch.setattr("translator.ocr.engine_available", lambda e: True)
    # v0.7.0: 管线走多引擎投票入口 → 打点在 ocr_page_lines_scored
    import pymupdf as _pm
    monkeypatch.setattr(
        "translator.ocr.ocr_page_lines_scored",
        lambda page, engine="paddle", src_lang="en", dpi=200:
            [(_pm.Rect(60, 90, 520, 105),
              "Scanned page content recognized by OCR engine.", 0.9)])
    src = _make_pdf([BODY, "", BODY], tmp_path)
    cfg = _cfg(src, tmp_path)
    from translator.pipeline import translate_document
    stats = translate_document(cfg, client=EchoLLM())
    assert stats["ocr_pages"] == 1
    out = pymupdf.open(stats["output"])
    assert len(out) == 4                          # 3 原页 + 1 附录页
    appendix = out[2].get_text()                  # 插在扫描页(p.2)之后
    assert "[OCR" in appendix and "【译】" in appendix
    out.close()


def test_ocr_disabled_engine_no_warning(tmp_path):
    src = _make_pdf([BODY, "", BODY], tmp_path)
    cfg = _cfg(src, tmp_path)
    from translator.config import OCRConfig
    cfg.ocr = OCRConfig(engine="none")
    from translator.pipeline import translate_document
    stats = translate_document(cfg, client=EchoLLM())
    assert not any("paddleocr" in w for w in stats["warnings"])


# ---------- 5. 布局并行一致性 ----------

MULTI = [BODY, BODY.replace("OCR", "second"), "Third page body text " * 3,
         "Fourth page body text " * 3]


@pytest.mark.parametrize("workers", [1, 2])
def test_parallel_layout_matches_sequential(tmp_path, workers):
    src = _make_pdf(MULTI, tmp_path)
    cfg = _cfg(src, tmp_path)
    cfg.performance = PerformanceConfig(layout_workers=workers)
    from translator.pipeline import translate_document
    stats = translate_document(cfg, client=None)   # dry-run
    assert stats["pages"] == 4
    assert Path(stats["output"]).is_file()


def test_parallel_and_sequential_same_paragraph_counts(tmp_path):
    from translator.pipeline import translate_document
    src = _make_pdf(MULTI, tmp_path)
    counts = []
    for workers in (1, 2):
        cfg = _cfg(src, tmp_path)
        cfg.performance = PerformanceConfig(layout_workers=workers)
        counts.append(translate_document(cfg, client=None)["paragraphs"])
    assert counts[0] == counts[1]


# ---------- 6. JobManager 队列与 history ----------

def test_job_queue_and_history(tmp_path, monkeypatch):
    from server import jobs

    started: list[str] = []

    def fake_start(self, project_root):
        started.append(self.id)
        self.status = "running"

    monkeypatch.setattr(jobs.Job, "start", fake_start)
    mgr = jobs.JobManager(tmp_path)

    r1 = mgr.submit("a.yaml")
    assert r1["ok"] and not r1["queued"]
    r2 = mgr.submit("b.yaml")
    assert r2["ok"] and r2["queued"] and len(mgr.queue) == 1
    # 队列上限
    for i in range(jobs.MAX_QUEUE - 1):
        assert mgr.submit(f"f{i}.yaml")["ok"]
    assert not mgr.submit("overflow.yaml")["ok"]
    # 当前任务终态 → 接力 + 归档
    cur = mgr.job
    cur.status = "done"
    mgr._on_terminal(cur)
    assert mgr.job is not None and mgr.job.id == r2["job_id"]
    assert mgr.job.status == "running"
    assert len(mgr.history) == 1 and mgr.history[0]["id"] == r1["job_id"]


def test_queued_cancelled_job_skipped(tmp_path, monkeypatch):
    from server import jobs

    monkeypatch.setattr(jobs.Job, "start",
                        lambda self, root: setattr(self, "status", "running"))
    mgr = jobs.JobManager(tmp_path)
    mgr.submit("a.yaml")
    r2 = mgr.submit("b.yaml")
    r3 = mgr.submit("c.yaml")
    mgr.queue[0].cancel()               # 取消排队中的 b
    assert mgr.queue[0].status == "cancelled"
    cur = mgr.job
    cur.status = "error"
    mgr._on_terminal(cur)
    assert mgr.job.id == r3["job_id"]   # 跳过 b，直接跑 c


def test_worker_subprocess_with_pool_e2e(tmp_path):
    """真子进程 worker（-c + 控制文件 + 并行布局池）端到端。

    回归锁：v0.4.3 初版 worker 用 stdin 管道读控制命令，与 Windows
    spawn 进程池组合死锁（布局阶段永挂）。控制文件方案修复。
    """
    import time
    import yaml
    from server import jobs

    root = Path(__file__).resolve().parents[1]
    src = _make_pdf([BODY, BODY, BODY], tmp_path)     # ≥3 页触发并行布局
    cfgp = tmp_path / "w.yaml"
    cfgp.write_text(yaml.safe_dump({
        "io": {"input": str(src), "output_dir": str(tmp_path / "out")},
        "features": {"watermark_removal": False, "preserve_formatting": False,
                     "translation_cache": False},
        "performance": {"layout_workers": 2},
    }), encoding="utf-8")
    mgr = jobs.JobManager(root)
    r = mgr.submit(str(cfgp))
    assert r["ok"] and not r["queued"]
    job = mgr.current()
    deadline = time.time() + 90
    while time.time() < deadline and job.status not in jobs._TERMINAL:
        time.sleep(0.5)
    assert job.status == "done", f"status={job.status} err={job.error}"
    assert Path(job.output_path).is_file()
    # 控制文件清理可能略晚于 status=done（worker 收尾与状态置位竞态）——
    # 固定 0.5s 等待在高负载下偶发不足，改轮询（v0.8.0 修复套件 flaky）
    deadline = time.time() + 10
    while time.time() < deadline and list(root.glob(".ui_ctl_*")):
        time.sleep(0.25)
    assert not list(root.glob(".ui_ctl_*"))            # 控制文件已清理


# ---------- 7. 配置容错 ----------

def test_config_unknown_keys_ignored(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "io: {input: x.pdf, output_dir: out, bogus_key: 1}\n"
        "llm: {provider: openai, no_such_param: 9}\n"
        "features: {bogus: true}\n"
        "performance: {bogus: 1, layout_workers: 2}\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.performance.layout_workers == 2
    assert cfg.llm.provider == "openai"


def test_llm_config_has_char_budget_default():
    assert LLMConfig().batch_char_budget == 3000
