"""v0.8.3 验收单测：请求墙钟/楔死终结/重试单层化（①⑤）+ 字体子集化（③）。

- LLMClientPool：SDK max_retries=0（重试单层化）、rebuild 换池与
  expect_gen 代数防护、close 收口
- create() 独立墙钟（v0.8.2 遗留：_receive_response_headers 路径
  httpx2 read timeout 不触发，真实网关楔死 25min+ 无异常）：
  - 流式头部楔死 → rebuild 解堵 → 退非流式重发成功
  - 非流式楔死 → rebuild 解堵 → 自有重试（attempt 2）成功
  - 裸 client（mock 注入）楔死 → 墙钟秒级 TimeoutError（无池不炸）
- 字体子集化：嵌入 CJK 字体的文档 subset 后字体流显著缩小、文字不丢
零网络（无 LLM 调用；LLMClientPool 构造纯离线）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from translator.llm import LLMClientPool, TranslationClient  # noqa: E402
from tests.test_v080 import _cjk_font_or_skip  # noqa: E402

_OK = '{"1": "ok"}'


# ---------- 假池 / 假 client ----------

def _wedge_pool(wedge_stream: bool, wedge_nonstream: bool):
    """duck-type 假池（满足 TranslationClient._pool 协议）。

    首次 create 楔死在头部等待阶段；rebuild 模拟旧池 close 语义——
    解堵所有阻塞读（阻塞中的 create 以 ConnectionResetError 失败）。
    """
    p = SimpleNamespace()
    p.generation = 3
    p.rebuild_calls: list = []
    p.calls = 0
    p._closed = threading.Event()

    def rebuild(reason="", expect_gen=None):
        p.rebuild_calls.append((reason, expect_gen))
        p.generation += 1
        p._closed.set()
        return True

    def create(**kw):
        p.calls += 1
        first = p.calls == 1
        if first and ((kw.get("stream") and wedge_stream)
                      or (not kw.get("stream") and wedge_nonstream)):
            if p._closed.wait(10.0):
                raise ConnectionResetError("connection pool closed (rebuilt)")
            raise AssertionError("wedged create not terminated in 10s")
        if kw.get("stream"):
            pytest.fail("fake pool: unexpected stream call")
        msg = SimpleNamespace(content=_OK)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    p.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    p.rebuild = rebuild
    return p


def _slow_plain_client(delay: float):
    """裸假 client（无 rebuild 属性 → 无池协议）：create 慢响应。"""
    calls = {"n": 0}

    class _C:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    calls["n"] += 1
                    time.sleep(delay)
                    msg = SimpleNamespace(content=_OK)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=msg)])

    return _C(), calls


# ---------- LLMClientPool 单元 ----------

def test_llm_client_pool_single_layer_and_rebuild():
    """SDK max_retries=0（单层重试）+ rebuild 换池 + expect_gen 防护。"""
    pool = LLMClientPool("http://127.0.0.1:9", "k", 5.0)
    try:
        assert pool.client.max_retries == 0, "SDK 层重试必须归零"
        assert pool.generation == 0
        first = pool.client
        assert pool.rebuild("unit") is True
        assert pool.client is not first, "rebuild 必须换新池"
        assert pool.generation == 1
        cur = pool.client
        assert pool.rebuild("stale", expect_gen=99) is False, \
            "代数不匹配必须拒绝重建（防把新池关掉）"
        assert pool.client is cur and pool.generation == 1
    finally:
        pool.close()
    assert pool.client is None


# ---------- 请求墙钟（①/⑤）----------

def test_stream_create_wedge_rebuild_then_nonstream():
    """流式头部楔死：墙钟到点 rebuild 解堵 → 退非流式重发成功。"""
    pool = _wedge_pool(wedge_stream=True, wedge_nonstream=False)
    tc = TranslationClient(pool, model="m", timeout=0.01, max_retries=1,
                           stream=True, request_deadline=0.3)
    t0 = time.time()
    raw = tc._request([{"role": "user", "content": "x"}], want_ids={"1"})
    assert time.time() - t0 < 3, "墙钟必须秒级终结头部楔死"
    assert raw == _OK, raw
    assert len(pool.rebuild_calls) == 1, pool.rebuild_calls
    reason, expect_gen = pool.rebuild_calls[0]
    assert expect_gen == 3 and "wallclock" in reason, pool.rebuild_calls
    assert any("stream failed" in w for w in tc.warnings), tc.warnings


def test_nonstream_wedge_rebuild_then_own_retry():
    """非流式楔死：墙钟 rebuild 解堵 → 自有重试层（attempt 2）成功。"""
    pool = _wedge_pool(wedge_stream=False, wedge_nonstream=True)
    tc = TranslationClient(pool, model="m", timeout=0.01, max_retries=2,
                           stream=False, request_deadline=0.3,
                           backoff_base=0.01, backoff_cap=0.02)
    t0 = time.time()
    batch_idx, out, _model = tc._process_batch([0], ["hello"])
    assert time.time() - t0 < 3, "楔死+重建+重试须在秒级完成"
    assert out == {0: "ok"}, out
    assert pool.calls == 2, "重建后自有重试应走新池"
    assert len(pool.rebuild_calls) == 1, pool.rebuild_calls


def test_plain_client_request_wallclock():
    """裸 client（mock 注入，无池）：墙钟仍生效，秒级 TimeoutError。"""
    plain, calls = _slow_plain_client(delay=2.0)
    tc = TranslationClient(plain, model="m", timeout=0.01, max_retries=1,
                           stream=False, request_deadline=0.2)
    t0 = time.time()
    with pytest.raises(TimeoutError):
        tc._request([{"role": "user", "content": "x"}])
    assert time.time() - t0 < 1.5, "无池时墙钟也必须截断"
    assert calls["n"] == 1


# ---------- 字体子集化（③）----------

def test_subset_fonts_shrinks_cjk_and_keeps_text():
    """③: 嵌 CJK 字体的文档子集化后字体流大幅缩小、文字不丢。

    实测背景：完整 SimSun ttf 17.47MB 原样嵌入输出；几十个字形的
    小文档子集后应缩到 20% 以下。
    """
    pytest.importorskip("fontTools")
    font_path = _cjk_font_or_skip()
    suffix = Path(font_path).suffix.lower()
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    arch = pymupdf.Archive()
    arch.add(font_path, "cjk" + suffix)
    page.insert_htmlbox(
        pymupdf.Rect(40, 40, 555, 800),
        "<p>中文翻译工具输出体积子集化验证：常用汉字覆盖测试，"
        "学术文献翻译、公式占位符与术语表一致性检查。</p>",
        css=(f"@font-face {{font-family: ptcjk; src: url(cjk{suffix});}}"
             " p {font-family: ptcjk; font-size: 12pt;}"),
        archive=arch)

    def _font_bytes(d):
        out = {}
        for xref in range(1, d.xref_length()):
            try:
                bf, ext, _ty, content = d.extract_font(xref)
            except Exception:
                continue
            if content:
                out[f"{bf}|{ext}"] = len(content)
        return out

    before = _font_bytes(doc)
    assert before, "字体应已嵌入"

    from translator.pipeline import _subset_fonts_try
    logs: list[str] = []
    assert _subset_fonts_try(doc, logs.append) is True, logs
    assert any("subset done" in m for m in logs), logs

    data = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    rdoc = pymupdf.open("pdf", data)
    after = _font_bytes(rdoc)
    text = " ".join(rdoc[p].get_text()
                    for p in range(rdoc.page_count)).strip()
    rdoc.close()
    assert "子集化" in text, f"子集化后文字不能丢: {text!r}"
    assert after, "子集化后字体仍应嵌入"
    big_before = max(before.values())
    big_after = max(after.values())
    assert big_after < big_before * 0.2, \
        f"CJK 字体流应缩小 ≥80%: {big_before} -> {big_after}"


# ---------- v0.8.3 ②: reflow 链接重映射 ----------

def _norm_find_unit():
    from translator.render_reflow import _norm_find
    assert _norm_find("见 文 献[1] 与 [1]", "[1]") == (5, 8)
    assert _norm_find("见 文 献[1] 与 [1]", "[1]", 8) == (11, 14)
    assert _norm_find("没有目标文本", "[9]") is None
    assert _norm_find("", "x") is None
    assert _norm_find("数据 https://doi.org/10.1000/x 页面",
                      "https://doi.org/10.1000/x") == (3, 28)


def test_reflow_link_norm_find():
    """去空白归一化定位：换行/空格漂移下引文标记与 URL 精确定位。"""
    _norm_find_unit()


def _linked_source_doc():
    """合成双页源 PDF：p0 引文链接 + URI 链接；p1 文献条目（跳转目标）。"""
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    p0, p1 = doc[0], doc[1]        # new_page 返回的句柄会被后续插入失效
    p0.insert_text(pymupdf.Point(60, 80),
                   "See the method in [1] for details.", fontsize=10)
    p0.insert_text(pymupdf.Point(60, 120),
                   "Visit https://example.com/data here.", fontsize=10)
    p1.insert_text(pymupdf.Point(60, 60),
                   '[1] A. Author, "A study of reflow links," 2026.',
                   fontsize=9)
    p0.insert_link({"kind": pymupdf.LINK_GOTO,
                    "from": pymupdf.Rect(142, 68, 156, 84),
                    "page": 1, "to": pymupdf.Point(60, 60)})
    p0.insert_link({"kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(90, 108, 250, 124),
                    "uri": "https://example.com/data"})
    return doc


def _linked_layouts():
    r = lambda x0, y0, x1, y1: pymupdf.Rect(x0, y0, x1, y1)

    def para(i, x, y, w, h, t):
        return {"index": i, "bbox": r(x, y, x + w, y + h), "col": 0,
                "text": t, "size": 10.0, "spans": [], "is_heading": False,
                "is_caption": False, "is_ref": False, "is_verbatim": False,
                "is_alg_caption": False, "is_list_item": False}
    return [
        {"mode": "one",
         "paragraphs": [para(0, 55, 65, 400, 22, "See the method in [1] for details."),
                        para(1, 55, 105, 400, 22, "Visit https://example.com/data here.")],
         "tables": [], "tables_cells": [], "formulas": [], "hf_blocks": [],
         "fig_text_blocks": [], "figure_regions": [],
         "layout_engine": "heuristic"},
        {"mode": "one",
         "paragraphs": [para(0, 55, 50, 400, 22,
                             '[1] A. Author, "A study of reflow links," 2026.')],
         "tables": [], "tables_cells": [], "formulas": [], "hf_blocks": [],
         "fig_text_blocks": [], "figure_regions": [],
         "layout_engine": "heuristic"},
    ]


def test_reflow_links_uri_and_goto_restored():
    """URI 直通 + 引文 GoTo 重映射：译文内定位、跳转落文献条目页。"""
    fp = _cjk_font_or_skip()
    out, warns = _render_linked_fp(fp)
    uris = [l for p in range(len(out)) for l in out[p].get_links()
            if l["kind"] == pymupdf.LINK_URI]
    gotos = [l for p in range(len(out)) for l in out[p].get_links()
             if l["kind"] == pymupdf.LINK_GOTO]
    assert any(l.get("uri") == "https://example.com/data" for l in uris), uris
    # 引文跳转：目标页含文献条目译文；链接矩形下正是 "[1]"
    assert gotos, "citation GoTo link missing"
    tgt_pages = {l["page"] for l in gotos}
    for tp in tgt_pages:
        assert "[1] A. Author" in out[tp].get_text("text"), \
            f"goto target page {tp} lacks the reference entry"
    # 链接源矩形下是 [1]（译文内精确定位，不是整段）
    src_pg = next(p for p in range(len(out))
                  if any(l["kind"] == pymupdf.LINK_GOTO
                         for l in out[p].get_links()))
    gl = next(l for l in out[src_pg].get_links()
              if l["kind"] == pymupdf.LINK_GOTO)
    near = out[src_pg].get_text("text", clip=pymupdf.Rect(gl["from"]))
    assert "[1]" in near.replace(" ", ""), \
        f"goto link rect should cover the citation marker: {near!r}"
    # 全文无丢段
    compact = "".join(out[p].get_text("text") for p in range(len(out)))
    assert "数据见" in compact and "细节见文献" in compact
    out.close()


def test_reflow_links_degrade_without_match():
    """src 文本未在译文保形（译者改写）→ 链接丢弃计数告警，不丢段不炸。"""
    from tests.test_v080 import _cjk_font_or_skip
    fp = _cjk_font_or_skip()
    texts = {0: {0: "细节见第一条文献。", 1: "数据页面链接略。"},
             1: {0: "[1] A. Author，《重排链接研究》，2026。"}}
    out, warns = _render_linked_fp(fp, texts)
    compact = "".join(out[p].get_text("text") for p in range(len(out)))
    assert "细节见第一条文献" in compact          # 段落完整
    assert any("reflow links" in w for w in warns), warns  # 丢弃有告警
    out.close()


def _render_linked_fp(fp, texts=None):
    import types as _t
    from translator.render_reflow import render_reflow_document
    from translator.typography import Typography
    if texts is None:
        texts = {0: {0: "有关方法的细节见文献[1]。",
                     1: "数据见 https://example.com/data 页面。"},
                 1: {0: "[1] A. Author，《重排链接研究》，2026。"}}
    doc = _linked_source_doc()
    typo = Typography({"body": "", "cjk": ""}, "zh")
    reflow_cfg = _t.SimpleNamespace(columns="single", body_size=0,
                                    segment_blocks=500)
    warns: list = []
    data = render_reflow_document(
        _linked_layouts(), doc, texts, {}, {}, set(), {}, typo,
        fp, "zh", reflow_cfg, warns, log=lambda m: None)
    doc.close()
    return pymupdf.open("pdf", data), warns


def test_cache_dir_nesting_guard(tmp_path):
    """cache_dir 指向缓存目录名本身时回退父目录（防双层嵌套）。"""
    from translator.doccache import resolve_cache_root, CACHE_DIR_NAME
    inner = tmp_path / CACHE_DIR_NAME
    root, src = resolve_cache_root(str(inner), tmp_path / "x.pdf", tmp_path)
    assert root == tmp_path and src == "config", (root, src)
    root2, _ = resolve_cache_root(str(tmp_path), tmp_path / "x.pdf", tmp_path)
    assert root2 == tmp_path


# ---------- v0.8.3 发布前检查修复回归 ----------

def test_layout_cache_sel_roundtrip(tmp_path):
    """io.pages 子集版面缓存：sel 条目按 sel 命中、不与全量条目互覆。

    v0.8.3 修复背景：流式路径 save_layout 漏传 sel（子集试译会用子集
    版面覆盖全量版面条目）；load 侧两路径从未带 sel（sel 后缀条目
    永远读不到，子集试译每次白做布局）。
    """
    from translator.doccache import DocumentCache
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4\n")   # save_layout 会 stat 源文件
    dc = DocumentCache(tmp_path / "proj")
    try:
        lay_full = [{"paragraphs": [], "mode": "one"} for _ in range(8)]
        lay_sub = [{"paragraphs": [], "mode": "one"} for _ in range(2)]
        dc.save_layout("fp", "heuristic", 2, tmp_path / "x.pdf", 8, lay_full)
        dc.save_layout("fp", "heuristic", 2, tmp_path / "x.pdf", 2, lay_sub,
                       sel="1-2")
        assert dc.load_layout("fp", "heuristic", 2) == lay_full, \
            "全量条目不得被子集条目覆盖"
        assert dc.load_layout("fp", "heuristic", 2, sel="1-2") == lay_sub
        assert dc.load_layout("fp", "heuristic", 2, sel="5-6") is None
    finally:
        dc.close()


def test_qualify_fp_namespaces_subset_pixmap_keys():
    """子集运行位图缓存指纹带 sel 限定（页码重编号防全量/子集串图）。"""
    from translator.pipeline import _qualify_fp
    assert _qualify_fp("abc", "") == "abc"
    assert _qualify_fp("abc", "1-2") == "abc|sel1-2"
    assert _qualify_fp(None, "1-2") is None
    from translator.doccache import DocumentCache
    k_full = DocumentCache.pixmap_key("abc", 0, pymupdf.Rect(0, 0, 9, 9), 300)
    k_sub = DocumentCache.pixmap_key(_qualify_fp("abc", "1-2"), 0,
                                     pymupdf.Rect(0, 0, 9, 9), 300)
    assert k_full != k_sub, "子集与全量的同页同区域 key 必须不同"


def test_split_segments_respects_config_limit():
    """reflow.segment_blocks 配置生效（旧版渲染层读模块常量，配置无效）。"""
    from translator.render_reflow import Block, _split_segments

    def heading(n):
        return Block(kind="para", kind_cls="sec_title", text=f"H{n}")

    def body(n):
        return Block(kind="para", kind_cls="body", text=f"b{n}")

    blocks = [heading(0)] + [body(i) for i in range(6)] + [heading(1)] \
        + [body(i) for i in range(6)]
    segs = _split_segments(blocks, 5)
    # 满 5 时块尾不是标题（body 不切），继续攒到标题块才切 → [8, 6]
    assert [len(s) for s in segs] == [8, 6], \
        f"满 limit 后在标题边界切: {[len(s) for s in segs]}"
    assert segs[0][-1].kind_cls == "sec_title", "切段必须落在标题块后"
    # limit 大于总块数 → 单段
    assert len(_split_segments(blocks, 500)) == 1
    # 空块流 → 无段（零内容守卫接管）
    assert _split_segments([], 5) == []


def test_collect_page_links_skips_malformed():
    """奇形链接（缺 from/意外字段）单条跳过，不炸整个 reflow 渲染。"""
    from translator.render_reflow import _collect_page_links

    class _Page:
        def get_links(self):
            return [
                {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(0, 0, 9, 9),
                 "uri": "https://x.example"},
                {"kind": pymupdf.LINK_GOTO, "from": None},       # 缺矩形
                {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(0, 0, 1, 1),
                 "page": 99, "to": None},                        # 越界无 to
                {"kind": 999, "from": pymupdf.Rect(0, 0, 1, 1)}, # 未知类型
                {"kind": "bogus", "from": pymupdf.Rect(0, 0, 1, 1),
                 "page": "not-a-number"},                        # 字段意外
            ]

        def get_text(self, kind, clip=None):
            return "[1]"

    recs = _collect_page_links(_Page(), 2)
    assert len(recs) == 1 and recs[0]["href_kind"] == "uri", recs
    assert recs[0]["src"] == "[1]" and recs[0]["owner"] is None


def test_streaming_translator_abort_cancels_queued_batches():
    """流式路径失败收口：abort 撤销排队批（旧版继续烧 LLM 调用）。"""
    gate = threading.Event()
    calls = {"n": 0}

    class _BlockingLLM:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    calls["n"] += 1
                    gate.wait(5.0)
                    msg = SimpleNamespace(content=_OK)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=msg)])

    tc = TranslationClient(_BlockingLLM(), model="m", batch_size=1,
                           batch_char_budget=0, max_workers=1,
                           max_llm_calls=10, stream=False)
    from translator.llm import StreamingTranslator
    st = StreamingTranslator(tc, cache=None)
    st.add_unit("first")     # 批 [0] 发车 → 阻塞在 create（占唯一 worker）
    st.add_unit("second")    # 批 [1] 排队（max_workers=1）
    assert len(st._futures) == 2
    time.sleep(0.2)          # 让批 [0] 真正进入 create
    queued_fut = st._futures[1]
    st.abort()
    assert st._pool is None and st._futures == [] and st._open == []
    assert queued_fut.cancelled(), "排队批必须被撤销"
    gate.set()
    time.sleep(0.2)
    assert calls["n"] == 1, \
        f"排队批不得再发请求（实发 {calls['n']} 次）"

