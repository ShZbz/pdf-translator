"""任务3/4 性能与速度优化:LLM 并发批翻译 + 全局 RPM 节流。

设计（保持成本模型不变——调用次数不增，只是并发发车）：
- ThreadPoolExecutor 并发跑 batch，max_workers 可配（默认 3）
- 全局节流:所有 worker 共享一个 min_call_interval 锁，RPM 保护不因并发失效
- 退避逻辑保留:传输层失败指数退避（在 worker 内做，不阻塞其他批）
- 结果按 batch 原序回填;触顶后未发车的批直接放弃（不再烧预算）
- 失败批降级保留原文 + 警告（与串行版语义一致）

v0.7.0:
- 流式解码 + 首包即回（llm.stream，默认开）：stream=True 增量收 delta，
  增量 JSON 解析器逐对提交 "id":"译文"——收齐全部 id 即提前断流
  （不等尾部废话 token），快段不被同批慢尾段阻塞
- 批内分段重试：内容校验失败不再整批重试——只重问缺失/违例的 id，
  重试后仍缺的 id 逐段保留原文 + 警告（其余段正常回填）
- 句子级缓存（模板化文本）：ref 条目/图注按句拆分跨文档复用
  （全命中才免调；翻译成功且句数对齐时回填句缓存）

v0.8.3:
- 请求墙钟 + 连接池终结（①/⑤：网关异常 × 重试叠加的根治）：
  create()（流式/非流式头部+整包）在守护线程执行并设独立墙钟——
  实测 httpx2 read timeout 在 _receive_response_headers 路径不触发，
  真实网关可把请求楔死 25min+ 无异常（py-spy 实锤品类）。到点时
  LLMClientPool.rebuild() 终结旧连接池（close 强制解堵楔死读）并换新，
  本线程按 TimeoutError 走既有失败批重试语义
- 重试单层化：SDK max_retries=0（LLMClientPool 统一构造），SDK 层盲
  重试 × 自有重试的时延乘法废止（实测 5s 超时被放大到 29.7s）——
  重试语义全部收敛到本类（内容感知：只重问缺失/违例 id）
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .control import JobCancelled

_FORMULA_RE = re.compile(r"\[FORMULA_(\d+)\]")

# v0.7.0 流式增量解析：顶层 "id": "value" 对（转义感知，值须以 , 或 } 收尾
# 才算提交——最后一段的完整性只能由闭合符或流结束判定）
_KV_RE = re.compile(
    r'"(\d+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]')

# 句子级缓存的目标单元（模板化文本：跨文档复用安全；上下文依赖句不启用）
_TEMPLATE_KINDS = ("ref", "caption")
# 拉丁句边界（用于模板文本的句拆分；缩写点不当句界由 ≥8 字符下限兜底）
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _budget_rule(batch: dict[str, str],
                 budgets: "list[int | None] | None",
                 batch_idx: list[int]) -> str:
    """本批每 id 字符预算 → prompt 规则行（v0.6.0 任务 E）。

    batch 的 id 是批内序号（1..n），budgets 按 paras 全局索引对齐——
    用 batch_idx 映射。无任何预算时返回空串（prompt 不变）。
    """
    if not budgets:
        return ""
    parts = []
    for j in batch_idx:
        b = budgets[j] if j < len(budgets) else None
        if b and b > 0:
            parts.append(f"id {j + 1} <= {b}")
    if not parts:
        return ""
    return ("Length limits (characters in the target language, HARD): "
            + ", ".join(parts)
            + ". Each translation MUST fit its limit; compress wording "
              "(keep every technical fact, unit and citation) if needed.")


def _parse_pairs(raw: str, want_ids: set[str]) -> "tuple[dict[str, str] | None, set[str]]":
    """v0.7.0 从（可能不完整的）模型输出提取 "id":"译文" 对。

    返回 (got, missing)：
    - got=None  输出里一个可解析对都没有（内容失败，调用方按重试处理，
      不烧预算——与旧版 _parse 全有或全无语义对齐）
    - got 部分  流式半途/模型漏段：已闭合的对先用，缺失 id 分段重试
    值须以 , 或 } 闭合才算完整（流式下未闭合的尾段不可信）。
    非流式完整 JSON 走快路径（整体 json.loads，零正则开销）。
    """
    if not raw:
        return None, set(want_ids)
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and data \
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in data.items()):
                got = {k: v for k, v in data.items() if k in want_ids}
                if got and set(got) == set(data):
                    return got, set(want_ids) - set(got)
        except json.JSONDecodeError:
            pass
    got: dict[str, str] = {}
    for mm in _KV_RE.finditer(raw):
        k, v_raw = mm.group(1), mm.group(2)
        if k in want_ids and k not in got:
            try:
                got[k] = json.loads(f'"{v_raw}"')   # 反转义（\" \\n \uXXXX）
            except json.JSONDecodeError:
                continue
    if not got:
        return None, set(want_ids)
    return got, set(want_ids) - set(got)


class LLMClientPool:
    """自持 HTTP 连接池持有器（v0.8.3 ①：楔死终结器 + 重试单层化）。

    三处入口（CLI / UI worker / validate-key）统一用它构造 OpenAI 兼容
    client：
    - 自持 httpx2 连接池（openai SDK 3.x 的 HTTP 栈）：看门狗到点可从
      外部 close——SDK 自建池时外部拿不到句柄，楔死连接无法终结；
    - SDK max_retries=0：重试单层化。SDK 层盲重试（连接类错误一律
      重发）与 TranslationClient 自有重试相乘（5s 超时实测放大到
      29.7s）；废止后重试语义全部收敛到自有层（内容感知，只重问
      缺失/违例 id）。

    duck-type 协议（TranslationClient 识别）：
    ``.client``（当前 OpenAI 实例）/ ``.generation``（池代数）/
    ``.rebuild(reason, expect_gen)``（终结并重建）/ ``.close()``。
    构造纯离线（OpenAI() 不联网），无 key/无网环境可安全建。
    """

    def __init__(self, base_url: str, api_key: str, timeout: float):
        self._base_url = base_url
        self._api_key = api_key or "sk-noop"
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._generation = 0
        self._openai, self._http = self._build_new()

    def _build_new(self) -> tuple:
        # httpx2 是 openai SDK 3.x 的 HTTP 栈；SDK 2.x 装的是 httpx（API 同源
        # 的前身）——按装的是哪个 SDK 用哪个，任一缺失不阻塞池构造
        # （实测 Windows 侧 openai 2.x 环境无 httpx2，旧版直接 ImportError
        # 令全部真翻译失败）
        try:
            import httpx2 as _httpx
        except ImportError:
            import httpx as _httpx
        from openai import OpenAI
        http = _httpx.Client(timeout=_httpx.Timeout(self._timeout))
        openai_client = OpenAI(base_url=self._base_url, api_key=self._api_key,
                               timeout=self._timeout, max_retries=0,
                               http_client=http)
        return openai_client, http

    @property
    def client(self):
        return self._openai

    @property
    def generation(self) -> int:
        return self._generation

    def rebuild(self, reason: str = "", expect_gen: int | None = None) -> bool:
        """终结当前池并换新（看门狗楔死终结出口）。

        expect_gen：调用方在发起请求前捕获的池代数——不一致说明别的
        线程已重建过（本请求多半已被那次重建顺手解堵），跳过防把新池
        关掉。先建新池换引用（并发线程随后的请求立即落新池），再关旧
        池——close 强制解堵阻塞在旧池 socket 读上的请求（含楔死的
        _receive_response_headers），它们按传输层失败走既有重试语义。
        已知竞态：两个楔死同时到点会各重建一次，第二次会关掉第一次的
        新池（在飞请求被解堵重试）——代价可接受，不为此加序贯协议。
        """
        new_openai, new_http = self._build_new()
        rebuilt = False
        with self._lock:
            if expect_gen is None or expect_gen == self._generation:
                old_openai, old_http = self._openai, self._http
                self._openai, self._http = new_openai, new_http
                self._generation += 1
                rebuilt = True
            else:
                old_openai, old_http = new_http, new_openai   # 放弃：新池关掉
        for c in (old_http, old_openai):
            try:
                c.close()
            except Exception:
                pass
        return rebuilt

    def close(self) -> None:
        """进程收尾用（validate 等一次性路径）；close 后本池不可再用。"""
        with self._lock:
            pools = (self._http, self._openai)
            self._openai = self._http = None
            self._generation += 1
        for c in pools:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass

    def call(self, fn, what: str, deadline: float = 0.0):
        """fn() 带独立墙钟执行（与 TranslationClient._run_with_wallclock 同款
        楔死终结语义，供 validate-key 等不经 TranslationClient 的一次性路径
        复用——旧版 validate 直接裸调 create()，头部阶段楔死时端点无限挂起，
        正是 v0.8.3 要根治的品类）。

        到点：rebuild 终结旧池（解堵楔死读）→ 仍无结果则 TimeoutError；
        调用方按传输层失败语义处理（validate 的 attempt 循环即重试层）。
        """
        out: dict = {}
        done = threading.Event()
        gen0 = self._generation

        def _run():
            try:
                out["v"] = fn()
            except BaseException as e:
                out["e"] = e
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True,
                         name="llm-call").start()
        dl = float(deadline or 0.0) or (self._timeout * 2.0 + 30.0)
        if not done.wait(dl):
            self.rebuild(f"{what} wallclock {dl:.0f}s", expect_gen=gen0)
            done.wait(5.0)
            if not done.is_set():
                raise TimeoutError(
                    f"{what} wedged > {dl:.0f}s (connection pool rebuilt)")
        if "e" in out:
            raise out["e"]
        return out.get("v")


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
                 control=None,
                 stream: bool = True,
                 sentence_cache: bool = True,
                 stream_deadline: float = 0.0,
                 request_deadline: float = 0.0):
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
        self.cache_hits = 0          # v0.5.1: 缓存命中段数（文档级节省报表）
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
        # v0.7.0: 流式解码 / 句子级缓存开关（llm.stream / llm.sentence_cache）
        self.stream = bool(stream)
        self.sentence_cache = bool(sentence_cache)
        self.sent_cache_hits = 0               # 句子级缓存命中段数（报表用）
        # v0.8.2: 流式总墙钟上限（秒；0=自动 2×timeout+30）。测试注入口
        self._stream_deadline = float(stream_deadline or 0.0)
        # v0.8.3: create() 请求墙钟（流式头部/非流式整包共用；0=自动
        # 2×timeout+30）。测试注入口
        self._request_deadline = float(request_deadline or 0.0)

    # ---- v0.8.3: 连接池协议 + 请求墙钟 ----

    def _pool(self):
        """duck-type 连接池持有器（LLMClientPool/测试假池）；裸 client（mock
        注入）返回 None——墙钟仍生效，只是没有池可终结。"""
        c = self.client
        if c is not None and callable(getattr(c, "rebuild", None)) \
                and getattr(c, "client", None) is not None:
            return c
        return None

    def _cur_client(self):
        """当前请求用的 OpenAI 实例——池持有器则取实时引用（重建后立即生效）。"""
        pool = self._pool()
        return pool.client if pool is not None else self.client

    def _run_with_wallclock(self, fn, what: str):
        """fn() 在守护线程执行并设独立墙钟（v0.8.3 ①/⑤）。

        背景（v0.8.2 e2e 遗留）：httpx2 read timeout 在
        _receive_response_headers 路径实测不触发，真实网关可把请求楔死
        25min+ 无异常——HTTP 路径的独立墙钟是最后防线。到点：
        池持有器 → rebuild()（旧池 close 强制解堵楔死读，再稍等片刻让
        解堵异常自然返回）；裸 client（测试 mock）→ 直接按墙钟失败。
        两条路都把 TimeoutError 交给 _ask 的传输层失败语义（退避重试）。
        被放弃的守护线程随旧池 close 或网关最终响应自然消亡（daemon）。
        """
        out: dict = {}
        done = threading.Event()
        pool = self._pool()
        gen0 = getattr(pool, "generation", None) if pool is not None else None

        def _run():
            try:
                out["v"] = fn()
            except BaseException as e:      # 原样带回调用线程判型
                out["e"] = e
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True, name="llm-create")
        t.start()
        deadline = self._request_deadline or (self.timeout * 2.0 + 30.0)
        if not done.wait(deadline):
            if pool is not None:
                pool.rebuild(f"{what} wallclock {deadline:.0f}s",
                             expect_gen=gen0)
                done.wait(5.0)              # close 通常立刻解堵楔死读
            if not done.is_set():
                raise TimeoutError(
                    f"{what} wedged > {deadline:.0f}s "
                    f"(connection pool {'rebuilt' if pool is not None else 'absent'})")
        if "e" in out:
            raise out["e"]
        return out.get("v")

    # ---- prompt 构造 ----
    def _system_prompt(self, budget_rule: str = "") -> str:
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
        if budget_rule:
            # v0.6.0 任务 E：源头控长——每 id 的目标框字符预算（排版框
            # 装不下的译文会在渲染层被压缩，不如在翻译时就说清楚）
            rules.append(budget_rule)
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
             prev_fail_transport: bool = False,
             budget_rule: str = "") -> dict[str, str] | None:
        """单次调用（线程内执行），返回译文 map；失败返回 None。

        退避语义（v0.1.2 沿袭）:仅传输层失败(429/网络)重试前退避;
        内容校验失败(JSON 坏/key 错/占位符丢)不等待直接重试。
        budget_rule: v0.6.0 每 id 字符预算规则（拼进 system prompt）。
        v0.7.0 流式:stream=True 增量收 delta，_parse_pairs 逐对提交，
        收齐全部 id 即 break（提前断流，不等尾部 token）——省时省 token；
        某对值尚未闭合时该 id 不提交，交给重试路径只重问缺失 id。
        流式通道异常（网关不支持 stream 等）自动退非流式重发一次。
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
            messages = [
                {"role": "system",
                 "content": self._system_prompt(budget_rule)},
                {"role": "user", "content": user},
            ]
            try:
                raw = self._request(messages, want_ids=set(batch))
            except Exception as e:  # 网络/SDK 错误按失败批处理
                kind = "rate-limited" if self._is_rate_limit_error(e) else "error"
                self._warn(f"LLM call {kind} (attempt {attempt}): {e}")
                with self._lock:
                    self._last_fail_transport = True
                    self._last_daily_quota = self._is_daily_quota_error(e)
                    self._last_retry_delay = (self._retry_delay_seconds(e)
                                              if kind == "rate-limited" else None)
                return None
            parsed, _missing = _parse_pairs(raw, set(batch))
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

    def _request(self, messages: list[dict],
                 want_ids: set[str] | None = None) -> str:
        """一次 HTTP 请求（v0.7.0 流式优先；v0.8.3 create() 全程独立墙钟）。

        流式路径增量收 delta，每当缓冲区出现新的已闭合 "id":"…" 对就记账；
        want_ids 全部收齐即 break——模型爱在 JSON 后面跟的解释性废话
        一个 token 都不用等（实测省 10-30% 批耗时）。断流点之后所有对
        已由 , 或 } 闭合，_parse_pairs 必然全量命中。
        流式通道异常（网关不支持 stream/代理剥离 SSE）自动退非流式重发。
        v0.8.3: 流式/非流式的 create() 都经 _run_with_wallclock——头部
        等待阶段（_receive_response_headers）的 httpx2 read timeout 失效
        楔死由墙钟终结（池持有器 rebuild + TimeoutError → 退避重试）。
        """
        kwargs = dict(model=self.model, temperature=self.temperature,
                      timeout=self.timeout, messages=messages)
        if self.stream:
            stream = None
            try:
                buf = ""
                got: set[str] = set()
                need = {str(k) for k in want_ids} if want_ids else None
                # v0.8.2: 流式总墙钟上限（双保险）——实测网关可能停发内容
                # 但保持连接（SSE 保活字节周期到达，httpx read timeout 按
                # 单次读间隙计时被不断重置，worker 卡死在 ssl read 整文档
                # 假死，py-spy 实锤双线程全挂 _receive_response_body）。
                # ① 逐 chunk 墙钟检查（内容滴漏型停顿）；② 看门狗定时器到
                # 点强制 close 流（保活字节型停顿——注释行不产出 chunk，
                # 逐 chunk 检查永不触达）。墙钟 = 2×timeout+30s：正常批
                # 30-120s 完成，慢思考模型 timeout 本身已放大；超时走非流式
                # 重发（该路径 httpx 对整包读取有完整超时语义）
                deadline = self._stream_deadline or (self.timeout * 2.0 + 30.0)
                t0 = time.monotonic()
                stream = self._run_with_wallclock(
                    lambda: self._cur_client().chat.completions.create(
                        stream=True, **kwargs),
                    "stream create")
                if not hasattr(stream, "__iter__"):
                    # 网关/mock 无视 stream 参数直接回完整响应对象——
                    # 当非流式结果用（不再二次请求，响应已被本次消耗）
                    return stream.choices[0].message.content or ""

                def _close_quietly():
                    try:
                        stream.close()
                    except Exception:
                        pass

                watchdog = threading.Timer(deadline, _close_quietly)
                watchdog.daemon = True
                watchdog.start()
                try:
                    for chunk in stream:
                        if time.monotonic() - t0 > deadline:
                            raise TimeoutError(
                                f"stream stalled > {deadline:.0f}s without "
                                f"completing {len(need or ())} id(s)")
                        try:
                            delta = chunk.choices[0].delta.content or ""
                        except (IndexError, AttributeError):
                            delta = ""
                        if not delta:
                            continue
                        buf += delta
                        if need is not None and '"' in delta:
                            got.update(m.group(1) for m in _KV_RE.finditer(buf))
                            if need <= got:
                                break      # 收齐即断流（见 docstring）
                finally:
                    watchdog.cancel()
                return buf
            except Exception as e:
                # 流式通道不可用（网关不支持/代理剥离 SSE/墙钟超时）→ 非流式
                # 重发。429/配额类语义明确：直接上抛走退避/切链，别浪费一次
                # 非流式。
                if self._is_rate_limit_error(e) or self._is_daily_quota_error(e):
                    raise
                self._warn(f"stream failed ({e}); retrying non-stream")
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
        resp = self._run_with_wallclock(
            lambda: self._cur_client().chat.completions.create(**kwargs),
            "non-stream request")
        return resp.choices[0].message.content or ""

    @staticmethod
    def formula_counts_ok(src: str, dst: str) -> bool:
        return sorted(_FORMULA_RE.findall(src)) == sorted(_FORMULA_RE.findall(dst))

    # ---- 单批完整流程（worker 入口）----
    def _guarded_batch(self, batch_idx: list[int],
                       paras: list[str],
                       budgets: "list[int | None] | None" = None
                       ) -> tuple[list[int], dict[int, str] | None, str]:
        """并发模式下的 worker 包装：取消/暂停检查点 + 异常隔离。

        取消时返回 None（已发车的批自然跑完，未发车的批放弃——
        与触顶放弃语义一致）；其他异常不炸线程池，按失败批降级。
        v0.5.1: 返回值追加本批实际使用的模型名（fallback 链切换后
        self.model 会漂移，缓存 key 必须用翻译时点的模型快照——
        旧版用写缓存时点的 self.model，m1 译文可能落到 m2 的 key 下）。
        """
        try:
            if self.control is not None:
                self.control.checkpoint()
            return self._process_batch(batch_idx, paras, budgets=budgets)
        except JobCancelled:
            with self._lock:
                self._stop_spawning = True
            if self.sink is not None:
                self.sink.emit("cancelled_at_batch")
            raise
        except Exception as e:   # 防御：worker 内异常不炸线程池
            self._warn(f"batch worker exception: {e}")
            return batch_idx, None, self.model

    def _process_batch(self, batch_idx: list[int],
                       paras: list[str],
                       budgets: list["int | None"] | None = None
                       ) -> tuple[list[int], dict[int, str] | None, str]:
        """返回 (batch_idx, {全局段索引: 译文} 或 None=失败, 本批模型)。

        budgets: v0.6.0 与 paras 对齐的每段字符预算（None=不限），
        拼进本批 system prompt（协议 JSON 形状不变）。
        v0.7.0 批内分段重试：响应里已闭合且公式守恒的 id 先收下，
        只把缺失/违例的 id 打成子批重问（id 保持原编号，budget 规则
        仍指得对）；重试耗尽后仅缺失段保留原文 + 逐段告警，已成功段
        正常回填——旧版"一损俱损整批重试/整批丢弃"的成本模型废止。
        """
        batch = {str(j + 1): paras[j] for j in batch_idx}
        budget_rule = _budget_rule(batch, budgets, batch_idx)
        got: dict[str, str] = {}          # id -> 已验证译文
        prev_fail_transport = False
        model_used = self.model   # v0.5.1: 本批模型快照（fallback 切换防串 key）
        # v0.2.2: while 循环替代 for——链式切换(m1→m2→m3)需要重置 attempt,
        # for 迭代器不受循环体内赋值影响,attempt-=1 是无效操作（实测教训）
        # v0.2.3: 重试次数上限 max_retries 可配（1=不重试）
        attempt = 1
        while attempt <= self.max_retries:
            pending = [k for k in batch if k not in got]
            if not pending:
                break
            resp = self._ask({k: batch[k] for k in pending}, attempt=attempt,
                             prev_fail_transport=prev_fail_transport,
                             budget_rule=budget_rule)
            if resp is None and attempt == 1 and self._last_daily_quota:
                # 日配额耗尽:切 fallback 模型后本批立即用新模型重试。
                # 切换成功则 attempt 重置（新模型配额独立,值得完整重试对）;
                # 切换失败（未配置/链尽）→ 正常走退避重试路径。
                if self._switch_to_fallback():
                    model_used = self.model
                    attempt = 1
                    continue
            if resp:
                model_used = self.model
                for k, v in resp.items():
                    # 公式守恒的段才收；违例段留在 pending 集下轮重问
                    if k in batch and self.formula_counts_ok(batch[k], v):
                        got[k] = v
            if len(got) == len(batch):
                break
            # 退避只对传输层失败;内容校验失败直接重试（_last_fail_transport
            # 由 _ask 设置,读回时加锁防并发写竞争——读错也只是退避策略偏差,无害）
            prev_fail_transport = bool(self._last_fail_transport)
            attempt += 1
        missing = [k for k in batch if k not in got]
        if missing:
            ids = ", ".join(f"#{k}:{batch[k][:30]!r}" for k in missing)
            self._warn(
                f"segment(s) failed after retry, kept source: [{ids}]")
        if not got:
            return batch_idx, None, model_used
        with self._lock:
            self._batches_ok += 1
        if self.sink is not None:
            self.sink.batch_done(self._batches_ok, self._batches_total,
                                 self.calls_used)
        out = {j: got[str(j + 1)] for j in batch_idx if str(j + 1) in got}
        return batch_idx, out, model_used

    def _pack_batches(self, miss: list[int],
                      paras: list[str]) -> list[list[int]]:
        """v0.4.3 组批策略：字符预算优先、段数为上限。

        旧版按固定段数切片（batch_size=6），长短段混批导致单批 token
        失衡——长批易超时失败、短批浪费调用。现按 batch_char_budget
        （默认 3000 字符）装填：
        - 批内累计字符超预算 → 开新批
        - 段数达 batch_size 上限 → 开新批（保留旧参数语义为每批段数上限）
        - 单段自身超预算 → 独占一批（段落是原子翻译单元，不拆）
        - budget=0 → 纯段数模式（v0.4.2 行为）

        v0.8.1 first-fit：顺序贪心改为「放入第一个装得下的开批，否则开
        新批」——长段装不下即封批会把开批剩余字符空间浪费掉，first-fit
        让后续短段回填早批空位。批协议/id（全局段索引）/缓存 key 全不变，
        只改装填顺序（真实段长分布模拟 ~-8% 批数；「最长优先」反而差
        6-25%——段数上限对短段成约束，不用）。批内段序仍为文档序。
        """
        budget = self.batch_char_budget
        batches: list[list[int]] = []
        chars: list[int] = []              # 各开批当前字符量（与 batches 对齐）
        for i in miss:
            n = len(paras[i])
            placed = False
            if budget > 0:
                for k, cur in enumerate(batches):
                    if len(cur) < self.batch_size and chars[k] + n <= budget:
                        cur.append(i)
                        chars[k] += n
                        placed = True
                        break
            else:                          # 纯段数模式：first-fit 即顺序
                for k, cur in enumerate(batches):
                    if len(cur) < self.batch_size:
                        cur.append(i)
                        placed = True
                        break
            if not placed:
                batches.append([i])
                chars.append(n)
        return batches

    # ---- 批量入口（并发版）----
    # ---- v0.7.0 句子级缓存（模板化文本：ref 条目/图注）----

    @staticmethod
    def _sentence_segments(text: str) -> list[str]:
        """模板文本的句拆分（拉丁 .?! + CJK 。！？句界）。

        短段（<2 句）无句级增益——主缓存（整段精确匹配）已覆盖；
        句级缓存的增量价值在"跨文档复用部分重叠文本"。
        """
        if not text:
            return []
        segs = [s.strip() for s in
                re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text) if s.strip()]
        return segs

    def _sentence_key(self, cache, model: str, seg: str) -> str:
        return cache.make_key("openai-compat", model,
                              self.src_lang, self.tgt_lang, f"|seg|{seg}")

    def _sentence_cache_get(self, cache, text: str, model: str) -> str | None:
        """全部句段命中才拼装（部分命中不注入——上下文依赖句半拼危险）。"""
        segs = self._sentence_segments(text)
        if len(segs) < 2:
            return None
        vals = [cache.get(self._sentence_key(cache, model, s)) for s in segs]
        if any(v is None for v in vals):
            return None
        from .langs import lang_info
        joiner = "" if lang_info(self.tgt_lang).script == "cjk" else " "
        return joiner.join(v for v in vals if v)

    def _sentence_cache_put(self, cache, src: str, dst: str, model: str) -> None:
        """翻译成功且源/译文句数对齐时回填句缓存（错位不存，宁缺毋滥）。"""
        ss = self._sentence_segments(src)
        if len(ss) < 2:
            return
        ds = self._sentence_segments(dst)
        if len(ss) != len(ds):
            return
        for a, b in zip(ss, ds):
            cache.put(self._sentence_key(cache, model, a), a, b)

    def _store_cache(self, cache, j: int, paras: list[str], dst: str,
                     model_used: str, unit_kinds=None) -> None:
        """批成功段落的主缓存写 + 模板句缓存回填（统一出口）。"""
        cache.put(cache.make_key(
            "openai-compat", model_used,
            self.src_lang, self.tgt_lang, paras[j]), paras[j], dst)
        if self.sentence_cache and unit_kinds \
                and j < len(unit_kinds) and unit_kinds[j] in _TEMPLATE_KINDS:
            self._sentence_cache_put(cache, paras[j], dst, model_used)

    def _store_cache_many(self, cache, pairs: "list[tuple[int, str]]",
                          paras: list[str], model_used: str,
                          unit_kinds=None) -> None:
        """v0.8.1: 批成功段落的主缓存批量写（put_many 单事务——旧版逐条
        put 每条一次 commit/fsync，数百段文档数百次 fsync）+ 模板句缓存
        回填（模板文本量小，维持逐条）。"""
        if cache is None or not pairs:
            return
        rows = [(cache.make_key("openai-compat", model_used,
                                self.src_lang, self.tgt_lang, paras[j]),
                 paras[j], dst) for j, dst in pairs]
        cache.put_many(rows)
        if self.sentence_cache and unit_kinds:
            for j, dst in pairs:
                if j < len(unit_kinds) and unit_kinds[j] in _TEMPLATE_KINDS:
                    self._sentence_cache_put(cache, paras[j], dst, model_used)

    def _cache_first(self, cache, i: int, t: str,
                     budget: "int | None", kind: "str | None") -> str | None:
        """缓存先行统一出口：句级（模板文本）→ 预算档 → 主缓存。

        v0.6.0: 预算档重译结果 key 加 |#b{N} 后缀——先查预算档（此前
        超预算重译过的短版），再查主缓存（旧命中译文仍有效，只是
        可能超长触发渲染阶梯）
        命中计数（cache_hits/sent_cache_hits）由调用方累加。
        """
        if cache is None:
            return None
        if self.sentence_cache and kind in _TEMPLATE_KINDS:
            hit = self._sentence_cache_get(cache, t, self.model)
            if hit is not None:
                self.sent_cache_hits += 1
                return hit
        key = cache.make_key("openai-compat", self.model,
                             self.src_lang, self.tgt_lang, t)
        hit = None
        if budget:
            hit = cache.get(f"{key}|#b{budget}")
        if hit is None:
            hit = cache.get(key)
        return hit

    def translate_paragraphs(self, paras: list[str],
                             cache=None,
                             budgets: "list[int | None] | None" = None,
                             unit_kinds: "list[str | None] | None" = None
                             ) -> tuple[list[str], int]:
        """翻译段落数组，返回 (译文数组, 实际调用次数)。

        并发策略:缓存命中先行;未命中批进线程池（max_workers 上限）;
        触顶后未发车的批直接保留原文。
        budgets: v0.6.0 每段目标框字符预算（None/空=不限）。批 prompt
        带 HARD 上限；译后软校验 len > budget×1.15 的段单段重问一次
        （每文档上限 = max_llm_calls 的 10% 防风暴，超限接受交渲染阶梯）。
        unit_kinds: v0.7.0 与 paras 对齐的单元类型（"ref"/"caption"/None）。
        ref 条目/图注是模板化文本，启用句子级缓存跨文档复用；其余单元
        上下文依赖性强，只走整段主缓存。
        """
        results: list[str | None] = [None] * len(paras)
        budgets = list(budgets) if budgets else None
        if budgets is not None and len(budgets) != len(paras):
            self._warn(f"budgets length mismatch ({len(budgets)} vs "
                       f"{len(paras)}); budgets ignored")
            budgets = None

        # 1) 缓存先行（串行,快）
        # v0.6.0: 预算档重译结果 key 加 |#b{N} 后缀——先查预算档（此前
        # 超预算重译过的短版），再查主缓存（旧命中译文仍有效，只是
        # 可能超长触发渲染阶梯）
        miss = []
        for i, t in enumerate(paras):
            kind = unit_kinds[i] if unit_kinds and i < len(unit_kinds) else None
            if cache is not None:
                budget_i = budgets[i] if budgets else None
                hit = self._cache_first(cache, i, t, budget_i, kind)
                if hit is not None:
                    results[i] = hit
                    self.cache_hits += 1
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
                futures = [pool.submit(self._guarded_batch, b, paras, budgets)
                           for b in batches]
                for fut in futures:
                    batch_idx, out, model_used = fut.result()
                    if out is None:
                        continue
                    for j, dst in out.items():
                        results[j] = dst
                    self._store_cache_many(
                        cache, list(out.items()), paras, model_used,
                        unit_kinds)
        else:
            for b in batches:
                if self.control is not None:
                    self.control.checkpoint()
                # v0.5.0: 串行路径与并发路径同走 _guarded_batch——旧版直接调
                # _process_batch，worker 内意外异常（如极端内容的序列化失败）
                # 会炸掉整个任务；并发路径本就有隔离，两路径行为对齐
                batch_idx, out, model_used = self._guarded_batch(b, paras,
                                                                 budgets)
                if out is None:
                    continue
                for j, dst in out.items():
                    results[j] = dst
                self._store_cache_many(
                    cache, list(out.items()), paras, model_used, unit_kinds)

        # 3) v0.6.0 任务 E：超预算软校验 → 单段强约束重问（上限防风暴）
        if budgets:
            self._reask_over_budget(paras, results, budgets, cache)

        # 4) 触顶未翻译段警告
        with self._lock:
            remaining = sum(1 for i in miss if results[i] is None)
            if remaining and self.api_ok_calls >= self.max_llm_calls:
                self.warnings.append(
                    f"max_llm_calls={self.max_llm_calls} reached; "
                    f"{remaining} paragraph(s) kept untranslated")

        final = [(r if r is not None else t) for r, t in zip(results, paras)]
        return final, self.calls_used

    def _reask_over_budget(self, paras: list[str],
                           results: list["str | None"],
                           budgets: list["int | None"], cache) -> None:
        """超预算段单段重问一次（len(dst) > budget×1.15 触发）。

        每文档上限 max(1, max_llm_calls//10)；预算槽位走 _ask 的常规
        记账（触顶自动放弃）。重问结果通过校验才采纳（公式数一致），
        存预算档缓存 key（|#b{N} 后缀）；仍超限则接受并告警（渲染阶梯兜底）。
        v0.8.1: 候选先收集后并行重问（提交进临时池，不超过 max_workers；
        _ask 的 in-flight 记账/全局节流保证不超预算不越 RPM）——旧版串行
        重问，超预算段多时逐段排队等待。
        """
        cap = max(1, self.max_llm_calls // 10)
        cands: list[int] = []
        for i, b in enumerate(budgets):
            if len(cands) >= cap:
                break
            dst = results[i]
            if not b or b <= 0 or dst is None or len(dst) <= b * 1.15:
                continue
            # 源文自身已超预算（数字单元格/机构名等不可压缩内容）——
            # 重问不可能更短，直接交渲染阶梯（实测 paper3 表 I 数字格）
            if len(paras[i]) > b * 1.15:
                continue
            if self._stop_spawning:
                break
            cands.append(i)
        if not cands:
            return

        def _ask_one(i: int):
            b = budgets[i]
            over = len(results[i]) - b
            rule = (f"Length limit (HARD): id 1 <= {b} characters. The "
                    f"previous attempt was {over} characters over; compress "
                    f"the translation (keep every technical fact, unit and "
                    f"citation) to fit.")
            return self._ask({"1": paras[i]}, attempt=1, budget_rule=rule)

        if len(cands) == 1 or self.max_workers <= 1:
            got_list = [(i, _ask_one(i)) for i in cands]
        else:
            from concurrent.futures import ThreadPoolExecutor as _Pool
            with _Pool(max_workers=min(self.max_workers, len(cands))) as pool:
                got_list = list(zip(
                    cands, pool.map(_ask_one, cands)))
        for i, got in got_list:
            b = budgets[i]
            dst = results[i]
            if got is None or "1" not in got:
                self._warn(f"budget re-ask #{i} failed; kept over-limit "
                           f"translation ({len(dst)}/{b} chars), renderer "
                           f"ladder will compress")
                continue
            if not self.formula_counts_ok(paras[i], got["1"]):
                self._warn(f"budget re-ask #{i}: formula placeholders "
                           f"mismatch; kept original translation")
                continue
            if len(got["1"]) < len(dst):
                results[i] = got["1"]
                if len(got["1"]) > b * 1.15:
                    self._warn(f"budget re-ask #{i} still {len(got['1'])}/{b}"
                               f" chars; accepted, renderer ladder will "
                               f"compress")
                if cache is not None:
                    key = cache.make_key(
                        "openai-compat", self.model,
                        self.src_lang, self.tgt_lang, paras[i])
                    cache.put(f"{key}|#b{b}", paras[i], got["1"])
            else:
                self._warn(f"budget re-ask #{i} produced no shorter "
                           f"translation; kept over-limit original "
                           f"({len(dst)}/{b} chars), renderer ladder will "
                           f"compress")


class StreamingTranslator:
    """v0.7.0 布局-翻译流水线重叠：翻译单元流式进批，批满即发车。

    与 translate_paragraphs 同一套批协议/缓存/预算/触顶记账；区别是
    组批发生在布局产出页的同时（页 N 布局完 → 该页单元立即进组批
    队列，布局继续跑页 N+1）——50+ 页大文档省整段布局时间。

    add_unit(text, budget, kind) 由管线在每页布局完成时调用；
    finish() 在全部页喂完后 flush 开批、join 全部 future、跑预算软
    校验，返回与单元顺序对齐的译文数组（失败单元回落原文）。
    """

    def __init__(self, tc: "TranslationClient", cache=None):
        self.tc = tc
        self.cache = cache
        self.paras: list[str] = []
        self.budgets: list["int | None"] = []
        self.kinds: list = []
        self.results: list["str | None"] = []
        # v0.8.1 first-fit 开批组：未满的批保持开放等短单元回填（与
        # _pack_batches 同语义）；放满即发车（保住布局-翻译流水线重叠，
        # 不把全量攒到 finish）
        self._open: list[list[int]] = []
        self._open_chars: list[int] = []
        self._futures: list = []
        self._pool: "ThreadPoolExecutor | None" = None
        self._batches_spawned = 0

    def add_unit(self, text: str, budget: "int | None" = None,
                 kind: "str | None" = None) -> int:
        """喂入一个翻译单元；缓存命中即回填，未命中进开批。返回单元序号。"""
        i = len(self.paras)
        hit = self.tc._cache_first(self.cache, i, text, budget, kind)
        self.paras.append(text)
        self.budgets.append(budget)
        self.kinds.append(kind)
        self.results.append(hit)          # None=未命中（后面批填）
        if hit is not None:
            self.tc.cache_hits += 1
            return i
        tc = self.tc
        b = tc.batch_char_budget
        n = len(text)
        placed = False
        for k, cur in enumerate(self._open):
            if len(cur) < tc.batch_size and (b <= 0
                                             or self._open_chars[k] + n <= b):
                cur.append(i)
                self._open_chars[k] += n
                placed = True
                break
        if not placed:
            self._open.append([i])
            self._open_chars.append(n)
        # 放满（段数到顶/字符到顶）的批立即发车——不等到 finish
        self._flush_full()
        return i

    def _flush_full(self) -> None:
        """把已放满的开批发车，未满的保持开放（first-fit 回填空位）。"""
        if not self._open:
            return
        b = self.tc.batch_char_budget
        todo: list[list[int]] = []
        keep: list[list[int]] = []
        keep_chars: list[int] = []
        for cur, ch in zip(self._open, self._open_chars):
            if len(cur) >= self.tc.batch_size or (b > 0 and ch >= b):
                todo.append(cur)
            else:
                keep.append(cur)
                keep_chars.append(ch)
        if todo:
            self._open, self._open_chars = keep, keep_chars
            self._spawn(todo)

    def flush(self) -> None:
        """全部剩余开批发车（触顶/停止发车时按放弃语义清空）。"""
        if not self._open:
            return
        todo, self._open = self._open, []
        _chars, self._open_chars = self._open_chars, []
        self._spawn(todo)

    def abort(self) -> None:
        """v0.8.3: 失败/取消路径的批终结——撤销未起跑的批、不再等在飞批。

        _translate_streaming 的错误路径调用（布局线程异常/取消/OCR 失败）：
        旧版直接 raise，线程池既不 shutdown 也不 cancel——排队批继续烧
        LLM 调用，回退顺序路径后新旧两条路并发请求（预算/缓存记账互不
        知情）。cancel_futures 撤销队列中的批；已起跑的批让其自然结束
        （单批有请求墙钟兜底，不会无限挂）。幂等：重复调用无副作用。
        """
        self._open, self._open_chars = [], []
        pool, self._pool = self._pool, None
        futures, self._futures = self._futures, []
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
            for fut in futures:
                if not fut.done():
                    fut.cancel()

    def _spawn(self, batches: list[list[int]]) -> None:
        """提交批进线程池（池惰性建；触顶后按放弃语义丢弃）。"""
        tc = self.tc
        if tc.control is not None:
            tc.control.checkpoint()
        with tc._lock:
            if tc._stop_spawning:
                return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=tc.max_workers)
            if tc.sink is not None:
                # 流式模式总批数未知：先按 1 发车，_batches_total 随发车增长
                # （UI 进度分母随之调整）
                tc.sink.emit("translate_start", batches=1,
                             paragraphs=len(self.paras))
        for batch in batches:
            self._futures.append(self._pool.submit(
                tc._guarded_batch, batch, self.paras, self.budgets))
            self._batches_spawned += 1
        with tc._lock:
            tc._batches_total = self._batches_spawned

    def finish(self) -> "tuple[list[str], int]":
        """冲批 → join → 缓存回填 → 预算软校验 → 触顶警告。"""
        tc = self.tc
        self.flush()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
        for fut in self._futures:
            _batch_idx, out, model_used = fut.result()
            if out is None:
                continue
            for j, dst in out.items():
                self.results[j] = dst
            tc._store_cache_many(self.cache, list(out.items()),
                                 self.paras, model_used, self.kinds)
        if any(b for b in self.budgets):
            tc._reask_over_budget(self.paras, self.results, self.budgets,
                                  self.cache)
        with tc._lock:
            remaining = sum(1 for r in self.results if r is None)
            if remaining and tc.api_ok_calls >= tc.max_llm_calls:
                tc.warnings.append(
                    f"max_llm_calls={tc.max_llm_calls} reached; "
                    f"{remaining} paragraph(s) kept untranslated")
        final = [r if r is not None else t
                 for r, t in zip(self.results, self.paras)]
        return final, tc.calls_used
