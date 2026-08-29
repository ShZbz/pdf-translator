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


@dataclass
class IOConfig:
    input: str
    output_dir: str
    source_lang: str = "en"
    target_lang: str = "zh"


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
    # inplace=白块覆盖+原位回灌（观感更好，与图形重叠的块自动跳过）
    mode: str = "appendix"


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


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    io_ = IOConfig(**{k: v for k, v in raw.get("io", {}).items()
                      if k in IOConfig.__dataclass_fields__})
    llm_raw = raw.get("llm", {})
    llm = LLMConfig(**{k: v for k, v in llm_raw.items() if k in LLMConfig.__dataclass_fields__})
    llm._explicit = set(llm_raw.keys()) & set(LLMConfig.__dataclass_fields__)
    feat = _filtered(FeatureConfig, raw.get("features", {}))
    if feat.renderer not in ("writer", "htmlbox"):
        raise ValueError(
            f"features.renderer 必须是 'writer' 或 'htmlbox'，当前 {feat.renderer!r}")
    perf = _filtered(PerformanceConfig, raw.get("performance", {}))
    if perf.layout_engine not in ("heuristic", "pymupdf-layout"):
        raise ValueError(
            "performance.layout_engine 必须是 'heuristic' 或 'pymupdf-layout'，"
            f"当前 {perf.layout_engine!r}")
    ocr = _filtered(OCRConfig, raw.get("ocr", {}))
    if ocr.mode not in ("appendix", "inplace"):
        raise ValueError(
            f"ocr.mode 必须是 'appendix' 或 'inplace'，当前 {ocr.mode!r}")
    from .fit import FitConfig
    fit_cfg = None
    if "fit" in raw:
        try:
            fit_cfg = FitConfig.from_raw(raw.get("fit") or {})
        except ValueError as e:
            raise ValueError(f"fit 配置无效: {e}") from e
    cfg = Config(io=io_, llm=llm, features=feat, performance=perf, ocr=ocr,
                 glossary_file=raw.get("glossary_file", ""),
                 fonts=raw.get("fonts") or {"cjk": ""},
                 fit=fit_cfg)
    # 硬校验：输入输出路径必填
    if not cfg.io.input:
        raise ValueError("config.io.input is required")
    return cfg