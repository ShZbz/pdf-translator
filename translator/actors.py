"""管线 actor 化（v0.8.5，候选池「任务 2-4 完整形态」第一步落地）。

EventSink 自 v0.7.1 起已是阶段无关总线（stage 标签 + 订阅 + 命令通道）。
本模块把流式路径的两个后台线程升格为正式 actor：

- ``StageActor``：阶段 actor 基类。独立线程 + inbox/outbox 队列；
  worker 内异常经 outbox 原样传回消费者线程就地 raise（与旧版
  producer 把异常 put 进队列的纪律同构）；每处理一项先过
  JobControl.checkpoint（页级暂停/取消响应点）。
- ``LayoutActor``：布局阶段（自驱逐页产出 (pno, layout, pixmaps)）。
- ``OcrActor``：OCR 提取阶段（inbox 消费页号，outbox 产出 (pno, job)；
  引擎异常自捕获进 errors——消费者 join 后统一 raise，不炸线程）。

翻译阶段的 actor 是 llm.StreamingTranslator（add_unit 进队/批满发车/
finish 收口，自持线程池），本模块不重复包装。

渲染阶段维持批式（fit.mode=auto 的样式因子是文档级全局量，页级渲染
与翻译重叠存在结构性屏障）；热跑重渲染的耗时问题由 v0.8.5 整页渲染
结果缓存（doccache.renders）解决——屏障与缓存的关系见 PLAN 候选池。

页级命令（"重译某页"）：EventSink.post({"cmd": "retranslate",
"pages": [pno...]})（0-based）。JobControl.checkpoint 消费命令通道时把
未知命令转发给 bind_command_handler 注册的钩子——pipeline 在喂翻译
单元处消费该命令，指定页的单元绕过翻译缓存强制重译（StreamingTranslator
路径支持运行中到达；顺序路径支持开跑前的 retranslate_pages 参数）。
"""
from __future__ import annotations

import queue
import threading


class StageActor:
    """阶段 actor 基类：独立线程 + inbox/outbox。

    - send(item)：投递任务项；close() 投递结束哨兵（worker 处理完
      存量后退出）。
    - results()：迭代 outbox。worker 异常原样 raise（消费者在自己的
      except 里做收口，如 streamer.abort）；正常结束迭代终止。
    - run()：子类实现的工作体（不要直接调用）。
    """

    def __init__(self, name: str, control=None):
        self.name = name
        self._control = control
        self._inbox: "queue.Queue" = queue.Queue()
        self._outbox: "queue.Queue" = queue.Queue()
        self._thread: "threading.Thread | None" = None
        self.errors: list[BaseException] = []   # OcrActor 语义：自捕获异常

    # ---- 生命周期 ----
    def start(self) -> "StageActor":
        self._thread = threading.Thread(target=self._body, daemon=True,
                                        name=self.name)
        self._thread.start()
        return self

    def close(self) -> None:
        """通知 worker 结束（哨兵排队，存量任务仍会处理完）。"""
        self._inbox.put(None)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 数据面 ----
    def send(self, item) -> None:
        self._inbox.put(item)

    def results(self):
        """迭代产出；worker 异常就地 raise，正常结束停止迭代。"""
        while True:
            item = self._outbox.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def drain(self) -> list:
        """非阻塞取走当前 outbox 存量（join 后收结果用）。"""
        out: list = []
        while True:
            try:
                item = self._outbox.get_nowait()
            except queue.Empty:
                return out
            if item is None or isinstance(item, BaseException):
                continue
            out.append(item)

    # ---- 内部 ----
    def _checkpoint(self) -> None:
        if self._control is not None:
            self._control.checkpoint()

    def _body(self) -> None:
        try:
            self.run()
        except BaseException as e:      # 与旧 producer 纪律同构：异常传消费侧
            self._outbox.put(e)
        finally:
            self._outbox.put(None)

    def run(self) -> None:              # pragma: no cover - 子类实现
        raise NotImplementedError


class LayoutActor(StageActor):
    """布局阶段 actor：逐页产出 (pno, layout, pixmaps)。

    自驱（无 inbox 任务项）——close() 后跑完当前页即退出。首个产出前
    先把（水印清理后的）doc 落临时文件再独立打开：PyMuPDF 非线程安全，
    worker 只碰自己的 pdoc；主线程在首个产出到达前阻塞在 outbox.get，
    doc.save 无并发访问（与旧 producer 线程同时序）。
    cached 非空 = 版面缓存命中，直接回放缓存布局（不重跑启发式）；
    公式裁图走项目位图缓存（重跑 0 调用也不再重裁）。
    """

    def __init__(self, doc, tmp_path, n_pages: int, cached: "list | None",
                 engine: str, dcache, pix_fp: "str | None", control=None):
        super().__init__("layout-actor", control=control)
        self._doc = doc
        self._tmp_path = tmp_path
        self._n_pages = n_pages
        self._cached = cached
        self._engine = engine
        self._dcache = dcache
        self._pix_fp = pix_fp

    def run(self) -> None:
        import pymupdf
        from .layout import layout_page
        from .pipeline import _crop_formulas_cached
        self._doc.save(str(self._tmp_path))
        pdoc = pymupdf.open(str(self._tmp_path))
        try:
            for pno in range(self._n_pages):
                self._checkpoint()
                if self._cached is not None:
                    lay = self._cached[pno]
                else:
                    lay = layout_page(pdoc[pno], engine=self._engine)
                pixmaps = _crop_formulas_cached(
                    pdoc, pno, lay.get("formulas") or [],
                    self._dcache, self._pix_fp) if lay.get("formulas") else {}
                self._outbox.put((pno, lay, pixmaps))
        finally:
            try:
                pdoc.close()
            except Exception:
                pass


class OcrActor(StageActor):
    """OCR 提取阶段 actor：inbox 消费页号，outbox 产出 (pno, job|None)。

    引擎实例非线程安全 → 单 worker 串行消费（与旧 _ocr_worker 同纪律）。
    引擎异常自捕获进 self.errors 并退出（消费者 join 后统一 raise）；
    独立 Document 懒打开——首个任务到达时布局快照必然已落盘。
    """

    def __init__(self, tmp_path, cfg, mode: str, engines: list,
                 warnings: list, control=None):
        super().__init__("ocr-actor", control=control)
        self._tmp_path = tmp_path
        self._cfg = cfg
        self._mode = mode
        self._engines = engines
        self._warnings = warnings

    def run(self) -> None:
        import pymupdf
        from .pipeline import _ocr_page_job
        odoc = None
        try:
            while True:
                pno = self._inbox.get()
                if pno is None:
                    break
                if odoc is None:
                    odoc = pymupdf.open(str(self._tmp_path))
                self._checkpoint()
                job = _ocr_page_job(odoc, pno, self._cfg, self._mode,
                                    self._engines, self._warnings)
                self._outbox.put((pno, job))
        except BaseException as e:      # 引擎崩溃/取消：记下交消费者统一处理
            self.errors.append(e)
        finally:
            if odoc is not None:
                try:
                    odoc.close()
                except Exception:
                    pass
