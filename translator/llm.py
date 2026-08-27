"""任务3/4 性能与速度优化:LLM 并发批翻译 + 全局 RPM 节流。

设计（保持成本模型不变——调用次数不增，只是并发发车）：
- ThreadPoolExecutor 并发跑 batch，max_workers 可配（默认 3）
- 全局节流:所有 worker 共享一个 min_call_interval 锁，RPM 保护不因并发失效
- 退避逻辑保留:传输层失败指数退避（在 worker 内做，不阻塞其他批）
- 结果按 batch 原序回填;触顶后未发车的批直接放弃（不再烧预算）
- 失败批降级保留原文 + 警告（与串行版语义一致）
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .control import JobCancelled

_FORMULA_RE = re.compile(r"\[FORMULA_(\d+)\]")


class TranslationClient:
    def __init__(self, client, model: str, temperature: float = 0.0,
                 glossary_prompt: str = "", src_lang: str = "en",
                 tgt_lang: str = "zh", batch_size: int = 6,
                 batch_char_budget: int = 3000,
                 max_llm_calls: int = 10,
                 min_call_interval: float = 0.0,
                 max_workers: int = 3,
                 fallback_model: str = "",
                 timeout: float = 120.0,
                 max_retries: int = 2,
                 backoff_base: float = 8.0,
                 backoff_cap: float = 30.0,
                 retry_delay_cap: float = 60.0,
                 sink=None,
                 control=None):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.glossary_prompt = glossary_prompt
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.batch_size = max(1, int(batch_size))
        # v0.4.3: 批字符预算（按字符量组批，长短段不混批；0=纯段数模式）
        self.batch_char_budget = max(0, int(batch_char_budget))
        self.max_llm_calls = max_llm_calls
        self.min_call_interval = min_call_interval
        self.max_workers = max(1, int(max_workers))
        self.fallback_model = fallback_model
        # v0.2.3: provider 行为参数全部可配（换模型按其限额调 config 不动代码）
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.retry_delay_cap = float(retry_delay_cap)
        # 备用链:逗号分隔多模型,Gemini 限额按模型独立计数,逐个切换吃满
        self._fallback_chain = [m.strip() for m in (fallback_model or "").split(",")
                                if m.strip()]
        self._fb_idx = 0
        self.calls_used = 0          # 实际发出的 API 请求数（含失败，报告用）
        self.api_ok_calls = 0        # 收到可用响应的调用数（预算记账用）
        self.warnings: list[str] = []
        # ---- 并发基础设施 ----
        self._lock = threading.Lock()          # 保护计数器/warnings
        self._throttle_lock = threading.Lock() # 全局 RPM 节流
        self._last_call_ts = 0.0
        self._last_fail_transport = False      # 最近一次调用是否传输层失败
        self._last_daily_quota = False         # 最近一次失败是否日配额耗尽
        self._last_retry_delay: float | None = None   # 429 报文建议等待秒数
        self._in_flight = 0                    # 已发车未归的批（预算槽位预占）
        self._stop_spawning = False            # 触顶后停止发新车
        # ---- v0.4.0: UI 集成 ----
        self.sink = sink                       # EventSink（None=纯 CLI）
        self.control = control                 # JobControl（None=不可控）
        self._batches_total = 0                # 本文档总批数（进度分母）
        self._batches_ok = 0                   # 成功批计数（缓存命中不计）

    # ---- prompt 构造 ----
    def _system_prompt(self) -> str:
        from .langs import prompt_lang_name
        rules = [
            f"You are a professional academic translator from "
            f"{prompt_lang_name(self.src_lang)} to "
            f"{prompt_lang_name(self.tgt_lang)}.",
            f"Write the translation in {prompt_lang_name(self.tgt_lang)}"
            f" ({self.tgt_lang}).",
            "Input is a JSON object mapping numeric ids to paragraphs.",
            "Output MUST be a single JSON object with the SAME ids mapped to translations. No extra text.",
            "Keep placeholders like [FORMULA_0] EXACTLY as-is, unchanged count and position semantics.",
            "Transcribe leftover LaTeX-ish math markup by meaning (e.g. Mn$_{3-x}$Sn -> Mn₃₋ₓSn); never copy $, _{}, ^{} literally.",
            "Do not alter numbers, units, citations ([22], Fig. 3), or symbols like ρxy, µΩ-cm.",
        ]
        if self.glossary_prompt:
            rules.append(self.glossary_prompt)
        return "\n".join(rules)

    def _warn(self, msg: str) -> None:
        """警告统一出口：进 warnings 列表 + 推事件流（sink 存在时）。"""
        with self._lock:
            self.warnings.append(msg)
        if self.sink is not None:
            self.sink.warning(msg)

    def _throttle_wait(self) -> None:
        """全局节流:跨线程共享,距上次请求不足间隔则等待。
        锁内计算等待+更新时间戳,保证并发下间隔仍成立。"""
        if self.min_call_interval <= 0:
            return
        with self._throttle_lock:
            now = time.monotonic()
            wait = self.min_call_interval - (now - self._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_call_ts = time.monotonic()

    def _is_rate_limit_error(self, e: Exception) -> bool:
        text = str(e)
        return "429" in text or "'code': '1302'" in text or "'code': '1305'" in text

    def _is_daily_quota_error(self, e: Exception) -> bool:
        """日配额耗尽（RPD）——切 fallback 模型的信号。

        Gemini 免费档 429 报文含 'PerDay' quotaId；RPM 超限是
        'PerMinute'，等待即可恢复，不应切换。
        """
        text = str(e)
        if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
            return False
        return "PerDay" in text or "per day" in text.lower()

    def _retry_delay_seconds(self, e: Exception) -> float | None:
        """从 429 报文解析服务器建议的等待秒数（RetryInfo.retryDelay）。

        RPM 窗口惩罚必须等满,固定退避(16s)不够会二次撞墙（实测教训）。
        封顶 retry_delay_cap（可配）。
        """
        m = re.search(r"[Rr]etry in\s+([\d.]+)\s*s", str(e))
        if m:
            return min(float(m.group(1)) + 1.0, self.retry_delay_cap)
        return None

    def _switch_to_fallback(self) -> bool:
        """沿备用链切下一个模型（每模型独立配额）。链尽返回 False。

        fallback_model 支持逗号分隔多个: "gemini-3.5-flash-lite, gemini-3.5-flash"
        —— Gemini 限额按模型独立计数,链式切换可把各模型额度依次吃满。
        """
        msg = None
        with self._lock:
            while self._fb_idx < len(self._fallback_chain):
                nxt = self._fallback_chain[self._fb_idx]
                self._fb_idx += 1
                if nxt and nxt != self.model:
                    old = self.model
                    self.model = nxt
                    # 缓存 key 含 model:切换后未命中段自动走新模型的配额,
                    # 已翻译段落缓存命中不受影响（防串味设计使然）
                    # 警告在锁外发（_warn 要拿同一把非重入锁）
                    msg = (f"daily quota exhausted for {old}; "
                           f"switched to {nxt}")
                    break
        if msg:
            self._warn(msg)
            return True
        return False

    def _ask(self, batch: dict[str, str], attempt: int = 1,
             prev_fail_transport: bool = False) -> dict[str, str] | None:
        """单次调用（线程内执行），返回译文 map；失败返回 None。

        退避语义（v0.1.2 沿袭）:仅传输层失败(429/网络)重试前退避;
        内容校验失败(JSON 坏/key 错/占位符丢)不等待直接重试。
        """
        with self._lock:
            # 预算按成功响应计（v0.1.2 沿袭，有单测锁定）：429 等失败
            # 未消耗服务端配额不烧预算；_in_flight 防并发超发。
            if self.api_ok_calls + self._in_flight >= self.max_llm_calls \
                    or self._stop_spawning:
                return None
            self._in_flight += 1          # 预算槽位预占,防并发超发
        try:
            if attempt > 1 and prev_fail_transport:
                # 429 报文带 RetryInfo → 等服务器建议的秒数（封顶 retry_delay_cap）;
                # RPM 窗口惩罚固定退避不够会二次撞墙（实测 paper3 教训）
                wait = self._last_retry_delay
                if wait is None:
                    wait = min(self.backoff_base * (2 ** (attempt - 1)),
                               self.backoff_cap)
                time.sleep(wait)
            self._throttle_wait()
            with self._lock:
                self.calls_used += 1
            user = json.dumps(batch, ensure_ascii=False)
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    timeout=self.timeout,
                    messages=[
                        {"role": "system", "content": self._system_prompt()},
                        {"role": "user", "content": user},
                    ],
                )
                raw = resp.choices[0].message.content or ""
            except Exception as e:  # 网络/SDK 错误按失败批处理
                kind = "rate-limited" if self._is_rate_limit_error(e) else "error"
                self._warn(f"LLM call {kind} (attempt {attempt}): {e}")
                with self._lock:
                    self._last_fail_transport = True
                    self._last_daily_quota = self._is_daily_quota_error(e)
                    self._last_retry_delay = (self._retry_delay_seconds(e)
                                              if kind == "rate-limited" else None)
                return None
            parsed = self._parse(raw, set(batch))
            with self._lock:
                if parsed is not None:
                    self.api_ok_calls += 1
                    if self.api_ok_calls >= self.max_llm_calls:
                        self._stop_spawning = True
                self._last_fail_transport = False
                self._last_daily_quota = False
            return parsed
        finally:
            with self._lock:
                self._in_flight -= 1

    @staticmethod
    def _parse(raw: str, want_ids: set[str]) -> dict[str, str] | None:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or set(data.keys()) != want_ids:
            return None
        out: dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(v, str):
                return None
            out[k] = v
        return out

    @staticmethod
    def formula_counts_ok(src: str, dst: str) -> bool:
        return sorted(_FORMULA_RE.findall(src)) == sorted(_FORMULA_RE.findall(dst))

    # ---- 单批完整流程（worker 入口）----
    def _guarded_batch(self, batch_idx: list[int],
                       paras: list[str]) -> tuple[list[int], dict[int, str] | None]:
        """并发模式下的 worker 包装：取消/暂停检查点 + 异常隔离。

        取消时返回 None（已发车的批自然跑完，未发车的批放弃——
        与触顶放弃语义一致）；其他异常不炸线程池，按失败批降级。
        """
        try:
            if self.control is not None:
                self.control.checkpoint()
            return self._process_batch(batch_idx, paras)
        except JobCancelled:
            with self._lock:
                self._stop_spawning = True
            if self.sink is not None:
                self.sink.emit("cancelled_at_batch")
            raise
        except Exception as e:   # 防御：worker 内异常不炸线程池
            self._warn(f"batch worker exception: {e}")
            return batch_idx, None

    def _process_batch(self, batch_idx: list[int],
                       paras: list[str]) -> tuple[list[int], dict[int, str] | None]:
        """返回 (batch_idx, {全局段索引: 译文} 或 None=失败)。"""
        batch = {str(j + 1): paras[j] for j in batch_idx}
        got = None
        prev_fail_transport = False
        # v0.2.2: while 循环替代 for——链式切换(m1→m2→m3)需要重置 attempt,
        # for 迭代器不受循环体内赋值影响,attempt-=1 是无效操作（实测教训）
        # v0.2.3: 重试次数上限 max_retries 可配（1=不重试）
        attempt = 1
        while attempt <= self.max_retries:
            got = self._ask(batch, attempt=attempt,
                            prev_fail_transport=prev_fail_transport)
            if got is not None and all(
                    self.formula_counts_ok(batch[k], got[k]) for k in batch):
                break
            if got is None and attempt == 1 and self._last_daily_quota:
                # 日配额耗尽:切 fallback 模型后本批立即用新模型重试。
                # 切换成功则 attempt 重置（新模型配额独立,值得完整重试对）;
                # 切换失败（未配置/链尽）→ 正常走退避重试路径。
                if self._switch_to_fallback():
                    attempt = 1
                    continue
            got = None
            # 退避只对传输层失败;内容校验失败直接重试（_last_fail_transport
            # 由 _ask 设置,读回时加锁防并发写竞争——读错也只是退避策略偏差,无害）
            prev_fail_transport = bool(self._last_fail_transport)
            attempt += 1
        if got is None:
            ids = ", ".join(f"#{k}:{batch[k][:30]!r}" for k in batch)
            self._warn(
                f"batch failed after retry, keep source: [{ids}]")
            return batch_idx, None
        with self._lock:
            self._batches_ok += 1
        if self.sink is not None:
            self.sink.batch_done(self._batches_ok, self._batches_total,
                                 self.calls_used)
        out = {j: got[str(j + 1)] for j in batch_idx}
        return batch_idx, out

    def _pack_batches(self, miss: list[int],
                      paras: list[str]) -> list[list[int]]:
        """v0.4.3 组批策略：字符预算优先、段数为上限。

        旧版按固定段数切片（batch_size=6），长短段混批导致单批 token
        失衡——长批易超时失败、短批浪费调用。现按 batch_char_budget
        （默认 3000 字符）贪心装填：
        - 批内累计字符超预算 → 开新批
        - 段数达 batch_size 上限 → 开新批（保留旧参数语义为每批段数上限）
        - 单段自身超预算 → 独占一批（段落是原子翻译单元，不拆）
        - budget=0 → 纯段数模式（v0.4.2 行为）
        """
        budget = self.batch_char_budget
        batches: list[list[int]] = []
        cur: list[int] = []
        cur_chars = 0
        for i in miss:
            n = len(paras[i])
            if cur and (len(cur) >= self.batch_size
                        or (budget > 0 and cur_chars + n > budget)):
                batches.append(cur)
                cur, cur_chars = [], 0
            cur.append(i)
            cur_chars += n
        if cur:
            batches.append(cur)
        return batches

    # ---- 批量入口（并发版）----
    def translate_paragraphs(self, paras: list[str],
                             cache=None) -> tuple[list[str], int]:
        """翻译段落数组，返回 (译文数组, 实际调用次数)。

        并发策略:缓存命中先行;未命中批进线程池（max_workers 上限）;
        触顶后未发车的批直接保留原文。
        """
        results: list[str | None] = [None] * len(paras)

        # 1) 缓存先行（串行,快）
        miss = []
        for i, t in enumerate(paras):
            if cache is not None:
                hit = cache.get(cache.make_key(
                    "openai-compat", self.model,
                    self.src_lang, self.tgt_lang, t))
                if hit is not None:
                    results[i] = hit
                    continue
            miss.append(i)

        if not miss:
            return [r if r is not None else t for r, t in zip(results, paras)], 0

        # 2) 分批 + 并发执行（v0.4.3: 字符预算组批）
        batches = self._pack_batches(miss, paras)
        with self._lock:
            self._batches_total = len(batches)
        if self.sink is not None:
            self.sink.emit("translate_start", batches=self._batches_total,
                           paragraphs=len(miss))
        n_workers = min(self.max_workers, len(batches))
        if n_workers > 1:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(self._guarded_batch, b, paras)
                           for b in batches]
                for fut in futures:
                    batch_idx, out = fut.result()
                    if out is None:
                        continue
                    for j, dst in out.items():
                        results[j] = dst
                        if cache is not None:
                            cache.put(cache.make_key(
                                "openai-compat", self.model,
                                self.src_lang, self.tgt_lang, paras[j]),
                                paras[j], dst)
        else:
            for b in batches:
                if self.control is not None:
                    self.control.checkpoint()
                batch_idx, out = self._process_batch(b, paras)
                if out is None:
                    continue
                for j, dst in out.items():
                    results[j] = dst
                    if cache is not None:
                        cache.put(cache.make_key(
                            "openai-compat", self.model,
                            self.src_lang, self.tgt_lang, paras[j]),
                            paras[j], dst)

        # 3) 触顶未翻译段警告
        with self._lock:
            remaining = sum(1 for i in miss if results[i] is None)
            if remaining and self.api_ok_calls >= self.max_llm_calls:
                self.warnings.append(
                    f"max_llm_calls={self.max_llm_calls} reached; "
                    f"{remaining} paragraph(s) kept untranslated")

        final = [(r if r is not None else t) for r, t in zip(results, paras)]
        return final, self.calls_used
