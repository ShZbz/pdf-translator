"""YAML 配置 → dataclass 校验 + provider presets（SCHEME §5）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PRESETS: dict[str, dict] = {
    "deepseek":    {"base_url": "https://api.deepseek.com/v1", "env": "DEEPSEEK_API_KEY"},
    "openai":      {"base_url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY"},
    "zhipu":       {"base_url": "https://open.bigmodel.cn/api/paas/v4", "env": "ZHIPU_API_KEY"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "env": "SILICONFLOW_API_KEY"},
    "gemini":      {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "env": "GEMINI_API_KEY"},
    "ollama":      {"base_url": "http://localhost:11434/v1", "env": ""},
    "lmstudio":    {"base_url": "http://localhost:1234/v1", "env": ""},
}

# v0.7.1: provider 档位调优参数——首启向导/`--quick` 按档位自动写入的整组
# 推荐值（应用时只填用户未显式配置的键，显式配置永远优先）。
# model 只预填项目实测过的型号；其余留空由用户按 provider 文档填写。
PROVIDER_TUNING: dict[str, dict] = {
    "deepseek":    {"model": "deepseek-v4-flash", "batch_size": 6,
                    "batch_char_budget": 3000, "max_workers": 3,
                    "min_call_interval": 0, "max_llm_calls": 40,
                    "timeout": 120.0},
    "zhipu":       {"model": "glm-4.7-flash", "batch_size": 4,
                    "batch_char_budget": 2400, "max_workers": 2,
                    "min_call_interval": 1.0, "max_llm_calls": 40,
                    "timeout": 120.0},
    "gemini":      {"model": "", "batch_size": 4, "batch_char_budget": 2400,
                    "max_workers": 1, "min_call_interval": 6.0,
                    "max_llm_calls": 30, "timeout": 180.0},
    "siliconflow": {"model": "", "batch_size": 4, "batch_char_budget": 2400,
                    "max_workers": 2, "min_call_interval": 1.0,
                    "max_llm_calls": 40, "timeout": 120.0},
    "openai":      {"model": "", "batch_size": 6, "batch_char_budget": 3000,
                    "max_workers": 3, "min_call_interval": 0,
                    "max_llm_calls": 40, "timeout": 120.0},
    "ollama":      {"model": "", "batch_size": 4, "batch_char_budget": 2400,
                    "max_workers": 1, "min_call_interval": 0,
                    "max_llm_calls": 40, "timeout": 300.0},
    "lmstudio":    {"model": "", "batch_size": 4, "batch_char_budget": 2400,
                    "max_workers": 1, "min_call_interval": 0,
                    "max_llm_calls": 40, "timeout": 300.0},
}

# 向导下拉的推荐模型（按项目实测/文档收录，未收录的 provider 留空手填）
RECOMMENDED_MODELS: dict[str, list[str]] = {
    "deepseek": ["deepseek-v4-flash"],
    "zhipu": ["glm-4.7-flash"],
    "gemini": [], "siliconflow": [], "openai": [],
    "ollama": [], "lmstudio": [],
}


def apply_provider_tuning(cfg: Config, provider: str | None = None) -> list[str]:
    """按 provider 档位补齐未显式配置的 llm 键（`--quick`/向导共用）。

    只覆盖「YAML 未显式写出」的键（_explicit 记录）；返回应用的键名列表。
    """
    p = provider or cfg.llm.provider
    tuning = PROVIDER_TUNING.get(p)
    if not tuning:
        return []
    applied = []
    for k, v in tuning.items():
        if k in cfg.llm._explicit:
            continue
        setattr(cfg.llm, k, v)
        applied.append(k)
    return applied


def parse_page_ranges(spec: str, n_pages: int) -> list[int]:
    """页码子集 "1-2,5" → 0-based 页索引（去重升序，越界钳制）。

    空串/全页 → None（不裁剪）。格式错误抛 ValueError（load_config 即报）。
    """
    spec = (spec or "").strip()
    if not spec:
        return []
    idx: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ValueError(f"io.pages 页码段无效: {part!r}")
            if lo < 1 or hi < lo:
                raise ValueError(f"io.pages 页码段无效: {part!r}")
        else:
            try:
                lo = hi = int(part)
            except ValueError:
                raise ValueError(f"io.pages 页码无效: {part!r}")
            if lo < 1:
                raise ValueError(f"io.pages 页码无效: {part!r}")
        for p in range(lo, hi + 1):
            if p <= n_pages:
                idx.add(p - 1)
    return sorted(idx)


@dataclass
class IOConfig:
    input: str
    # v0.7.1: output_dir 改为可选（空=回落输入文件目录——pipeline 既有
    # 行为；最小配置只需写 input）
    output_dir: str = ""
    source_lang: str = "en"
    target_lang: str = "zh"
    # v0.7.1: 页码子集 "1-2,5"（--quick 试译/抽样用；空=全部页）
    pages: str = ""


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 0.0
    batch_size: int = 6
    # v0.4.3: 批字符预算——组批按字符量而非纯段数，长短段混批导致单批
    # token 失衡（长批易超时失败）。0=关闭（退回纯段数模式）
    batch_char_budget: int = 3000
    # v0.5.0: provider 配额（RPM/TPM，0=未设置）。设置后自动换算
    # min_call_interval 与 batch_char_budget（显式写出的配置项优先）
    rpm_limit: int = 0
    tpm_limit: int = 0
    max_llm_calls: int = 10
    min_call_interval: float = 0.0   # 两次 LLM 请求最小间隔秒（免费档 RPM 保护）
    max_workers: int = 3             # 并发批翻译线程数（1=串行;免费档建议 ≤4）
    fallback_model: str = ""         # 主模型配额耗尽时切换的备用模型（空=不切换）
    # ---- v0.2.3 provider 行为参数（换模型时按其限额调这些，不动代码）----
    timeout: float = 120.0           # 单次请求超时秒（慢模型如 nemotron 调大）
    max_retries: int = 2             # 单批最大尝试次数（1=不重试）
    backoff_base: float = 8.0        # 传输层失败退避基数秒
    backoff_cap: float = 30.0        # 退避上限秒
    retry_delay_cap: float = 60.0    # 429 RetryInfo 建议等待的封顶秒
    # ---- v0.7.0 速度项 ----
    stream: bool = True              # 流式解码+首包即回（网关不支持自动退非流式）
    sentence_cache: bool = True      # 句子级缓存（仅 ref 条目/图注等模板文本）

    def resolve(self) -> tuple[str, str]:
        """返回 (base_url, api_key)，preset/env 兜底。

        v0.2.3 优先级修正: config.api_key > provider preset env
        (如 DEEPSEEK_API_KEY) > OPENCODE_API_KEY（通用兜底）。
        旧顺序 OPENCODE_API_KEY 先于 preset env——多 provider 切换时
        会把 A 家的 key 发给 B 家（实测：OPENCODE 存的是 Gemini key，
        切 deepseek 后 401 Authentication Fails）。
        """
        p = PRESETS.get(self.provider)
        base = self.base_url or (p["base_url"] if p else "")
        key = self.api_key
        if not key and p and p["env"]:
            import os
            key = os.environ.get(p["env"], "")
        if not key:
            import os
            key = os.environ.get("OPENCODE_API_KEY", "")
        return base, key

    # v0.5.0: load_config 会把 YAML 里显式出现的 llm 键记到这里——
    # effective() 据此区分「用户显式配置」与「默认值」，自动换算不覆盖前者
    _explicit: set = field(default_factory=set, repr=False, compare=False)

    def effective(self) -> "LLMConfig":
        """v0.5.0 配额自适应：按 rpm_limit/tpm_limit 换算调用间隔与批预算。

        规则（显式配置优先，未显式写的键才允许自动值）：
        - min_call_interval 未显式且 rpm_limit>0 → 60/rpm（把 RPM 摊满每分钟）
        - batch_char_budget 未显式且 rpm/tpm 均设 → (tpm/rpm)×3.2 字符/token
          ×0.8 安全系数，clamp 到 [400, 12000]——每分钟发满 rpm 次请求、
          每请求约 tpm/rpm 个 token，正好吃满 TPM 而不超限
        """
        from dataclasses import replace
        out = replace(self)
        if self.rpm_limit > 0:
            if "min_call_interval" not in self._explicit:
                out.min_call_interval = round(60.0 / self.rpm_limit, 2)
            if "batch_char_budget" not in self._explicit and self.tpm_limit > 0:
                per_call_tok = self.tpm_limit / self.rpm_limit
                budget = int(per_call_tok * 3.2 * 0.8)
                out.batch_char_budget = max(400, min(12000, budget))
        return out


@dataclass
class FeatureConfig:
    watermark_removal: bool = True
    preserve_formatting: bool = True
    glossary_lock: bool = True
    translation_cache: bool = True
    bilingual: bool = False
    # v0.5.1: htmlbox 转默认渲染引擎（灰度验证后接班）——insert_htmlbox
    # HTML+CSS 排版，自带 shaping/bidi/两端对齐，RTL/天城文可用；
    # writer=TextWriter 逐字排印（遗留稳定路径）
    renderer: str = "htmlbox"


@dataclass
class OCRConfig:
    engine: str = "paddle"
    min_chars: int = 50
    # v0.5.0: 扫描页译文呈现方式。appendix=附录页（保守默认）；
    # inplace=白块覆盖+原位回灌（观感更好，与图形重叠的块自动跳过）；
    # reconstruct=版面自监督重建（v0.7.0）：区域几何来自 GNN 影子页/
    # 几何分割（与识别质量解耦），译文原位回灌 + 原文对照附录页
    mode: str = "appendix"
    # v0.7.0: 多引擎投票（paddle/rapidocr/tesseract 可用子集）。
    # 空=回落单引擎 engine；多引擎时同行结果按 IoU 对齐投票，
    # 冲突行取置信度最高并告警
    engines: list = field(default_factory=list)


@dataclass
class PerformanceConfig:
    """v0.4.3 性能段：本地管线并行度与缓存容量。"""
    layout_workers: int = 0      # 布局阶段进程并行数（0=自动：min(4, cpu)；1=串行）
    cache_max_entries: int = 50000   # 翻译缓存容量上限（0=不限制），超出淘汰最旧
    # v0.5.0: 版面引擎。heuristic=内置启发式（默认）；pymupdf-layout=
    # 外部 GNN 版面检测（需 pip install pymupdf-layout，未装自动回退启发式）
    layout_engine: str = "heuristic"
    # v0.5.1: 版面结果落盘缓存（段落级断点续跑）——同一输入（路径+大小+
    # mtime+引擎）重跑时跳过布局阶段直达翻译（配合翻译缓存只剩增量段）
    layout_cache: bool = True
    # v0.7.0: 布局-翻译流水线重叠。auto=启发式引擎且页数≥12 时启用
    #（布局后台线程逐页产出，翻译批随页发车——大文档省整段布局时间）；
    # on=无条件启用；off=关闭（布局全完成才开始翻译）
    pipeline_overlap: str = "auto"
    # v0.7.1: 项目级缓存库位置（空=自动：输入文件目录，只读时退输出目录）。
    # 同一输入译到不同输出目录共享翻译缓存/版面缓存；文档按内容指纹索引
    cache_dir: str = ""
    # v0.8.3: 按文档字体子集化（fontTools 可选依赖，未装自动跳过并提示）。
    # 实测输出嵌完整 SimSun 17.5MB + SimHei 9.3MB——子集化后输出文件
    # 约 15MB → 2-4MB。false=保持旧版完整字体嵌入
    subset_fonts: bool = True


@dataclass
class OutputConfig:
    """v0.8.0 双模式输出（任务 2-3 / P3）。

    faithful=版面1:1对照（默认，服务对照阅读）；
    reflow=整文档语义重排（P3：文档模型→新模板→Story 流式写入自动
    断页，页面对应关系不存在，输出名带 -reflow 后缀，服务纯阅读）。
    """
    mode: str = "faithful"


@dataclass
class ReflowConfig:
    """v0.8.0 P3 reflow 模板/样式选项（任务 3.2/3.4.1）。"""
    # auto=沿用原文档主导栏数与栏宽；single=强制单栏
    columns: str = "auto"
    # 正文字号 pt（0=沿用原文档 body 众数字号，保持视觉延续）
    body_size: float = 0.0
    # 单 Story 块数软上限（超长文档在章节边界分段写入防内存）
    segment_blocks: int = 500


@dataclass
class RenderConfig:
    """v0.7.1 渲染微调（任务 2-3 P1）：faithful 模式整页 Story 接管。

    page_story: auto=页级预检全过才启用（默认）；on=强制启用（预检失败
    仍整页回退）；off=关闭（回到逐段 insert_htmlbox）。
    """
    page_story: str = "auto"


def _filtered(dc, raw: dict):
    """dataclass 构造过滤：忽略 YAML 里的未知键（拼错键不炸，向后兼容）。"""
    return dc(**{k: v for k, v in raw.items() if k in dc.__dataclass_fields__})


@dataclass
class Config:
    io: IOConfig
    llm: LLMConfig = field(default_factory=LLMConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    glossary_file: str = ""
    fonts: dict = field(default_factory=lambda: {"cjk": ""})
    ocr: OCRConfig = field(default_factory=OCRConfig)
    # v0.6.0: 排版自适配（两遍式渲染 + 样式级因子 + 降级阶梯 + 源头控长）
    fit: "FitConfig | None" = None
    # v0.7.1: 双模式输出 + 渲染微调（任务 2-3）
    output: OutputConfig = field(default_factory=OutputConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    # v0.8.0 P3: reflow 模板/样式选项
    reflow: ReflowConfig = field(default_factory=ReflowConfig)


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    # v0.8.2: 空节点容错——YAML 里「output:」这类只有键没有内容的节点是
    # None，旧版 raw.get("output", {}) 拿到 None 后 .items() 直接
    # AttributeError（用户手写最小配置即崩）；统一 or {} 走默认值
    io_ = IOConfig(**{k: v for k, v in (raw.get("io") or {}).items()
                      if k in IOConfig.__dataclass_fields__})
    llm_raw = raw.get("llm") or {}
    llm = LLMConfig(**{k: v for k, v in llm_raw.items() if k in LLMConfig.__dataclass_fields__})
    llm._explicit = set(llm_raw.keys()) & set(LLMConfig.__dataclass_fields__)
    feat = _filtered(FeatureConfig, raw.get("features") or {})
    if feat.renderer not in ("writer", "htmlbox"):
        raise ValueError(
            f"features.renderer 必须是 'writer' 或 'htmlbox'，当前 {feat.renderer!r}")
    ocr = _filtered(OCRConfig, raw.get("ocr") or {})
    if ocr.mode not in ("appendix", "inplace", "reconstruct"):
        raise ValueError(
            f"ocr.mode 必须是 'appendix' / 'inplace' / 'reconstruct'，"
            f"当前 {ocr.mode!r}")
    if not isinstance(ocr.engines, list):
        raise ValueError(f"ocr.engines 必须是列表，当前 {ocr.engines!r}")
    # v0.8.4: 引擎名白名单——拼错（'paddel'）旧版要到运行期才以
    # 「未安装引擎（pip install paddleocr）」警告出场，误导排查；
    # 配置期即报与 renderer/mode 同款错误
    _engines = ("paddle", "rapidocr", "tesseract")
    if ocr.engine and ocr.engine != "none" and ocr.engine not in _engines:
        raise ValueError(
            f"ocr.engine 必须是 {'/'.join(_engines)} 或 none，"
            f"当前 {ocr.engine!r}")
    for _e in ocr.engines:
        if _e not in _engines:
            raise ValueError(
                f"ocr.engines 项必须是 {'/'.join(_engines)}，当前 {_e!r}")
    perf = _filtered(PerformanceConfig, raw.get("performance") or {})
    if perf.layout_engine not in ("heuristic", "pymupdf-layout"):
        raise ValueError(
            "performance.layout_engine 必须是 'heuristic' 或 'pymupdf-layout'，"
            f"当前 {perf.layout_engine!r}")
    # YAML 1.1 把裸 on/off 解析成 bool——与 fit.mode 同款归一
    po = perf.pipeline_overlap
    if isinstance(po, bool):
        po = "on" if po else "off"
    perf.pipeline_overlap = (str(po) or "auto").strip().lower()
    if perf.pipeline_overlap not in ("auto", "on", "off"):
        raise ValueError(
            "performance.pipeline_overlap 必须是 'auto'/'on'/'off'，"
            f"当前 {perf.pipeline_overlap!r}")
    from .fit import FitConfig
    fit_cfg = None
    if "fit" in raw:
        try:
            fit_cfg = FitConfig.from_raw(raw.get("fit") or {})
        except ValueError as e:
            raise ValueError(f"fit 配置无效: {e}") from e
    # ---- v0.8.0: 双模式输出 + 渲染微调 + reflow 选项 ----
    out_cfg = _filtered(OutputConfig, raw.get("output") or {})
    if out_cfg.mode not in ("faithful", "reflow"):
        raise ValueError(
            f"output.mode 必须是 'faithful' 或 'reflow'，"
            f"当前 {out_cfg.mode!r}")
    reflow_cfg = _filtered(ReflowConfig, raw.get("reflow") or {})
    if reflow_cfg.columns not in ("auto", "single"):
        raise ValueError(
            f"reflow.columns 必须是 'auto' 或 'single'，"
            f"当前 {reflow_cfg.columns!r}")
    reflow_cfg.body_size = min(max(float(reflow_cfg.body_size or 0.0),
                                   0.0), 24.0)
    reflow_cfg.segment_blocks = max(int(reflow_cfg.segment_blocks or 500),
                                    50)
    feat_bilingual = bool((raw.get("features") or {}).get("bilingual"))
    if out_cfg.mode == "reflow" and feat_bilingual:
        raise ValueError(
            "reflow 模式暂不支持双语对照（双语为 faithful 专有排版）；"
            "请 output.mode: faithful 或关闭 features.bilingual")
    render_cfg = _filtered(RenderConfig, raw.get("render") or {})
    ps = render_cfg.page_story
    if isinstance(ps, bool):          # YAML 1.1 裸 on/off → bool
        ps = "on" if ps else "off"
    render_cfg.page_story = (str(ps) or "auto").strip().lower()
    if render_cfg.page_story not in ("auto", "on", "off"):
        raise ValueError(
            f"render.page_story 必须是 'auto'/'on'/'off'，"
            f"当前 {render_cfg.page_story!r}")
    cfg = Config(io=io_, llm=llm, features=feat, performance=perf, ocr=ocr,
                 glossary_file=raw.get("glossary_file", ""),
                 fonts=raw.get("fonts") or {"cjk": ""},
                 fit=fit_cfg, output=out_cfg, render=render_cfg,
                 reflow=reflow_cfg)
    # 硬校验：输入输出路径必填
    if not cfg.io.input:
        raise ValueError("config.io.input is required")
    return cfg