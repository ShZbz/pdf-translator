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


@dataclass
class FeatureConfig:
    watermark_removal: bool = True
    preserve_formatting: bool = True
    glossary_lock: bool = True
    translation_cache: bool = True
    bilingual: bool = False


@dataclass
class OCRConfig:
    engine: str = "paddle"
    min_chars: int = 50


@dataclass
class PerformanceConfig:
    """v0.4.3 性能段：本地管线并行度与缓存容量。"""
    layout_workers: int = 0      # 布局阶段进程并行数（0=自动：min(4, cpu)；1=串行）
    cache_max_entries: int = 50000   # 翻译缓存容量上限（0=不限制），超出淘汰最旧


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


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    io_ = IOConfig(**{k: v for k, v in raw.get("io", {}).items()
                      if k in IOConfig.__dataclass_fields__})
    llm_raw = raw.get("llm", {})
    llm = LLMConfig(**{k: v for k, v in llm_raw.items() if k in LLMConfig.__dataclass_fields__})
    feat = _filtered(FeatureConfig, raw.get("features", {}))
    perf = _filtered(PerformanceConfig, raw.get("performance", {}))
    ocr = _filtered(OCRConfig, raw.get("ocr", {}))
    cfg = Config(io=io_, llm=llm, features=feat, performance=perf, ocr=ocr,
                 glossary_file=raw.get("glossary_file", ""),
                 fonts=raw.get("fonts") or {"cjk": ""})
    # 硬校验：输入输出路径必填
    if not cfg.io.input:
        raise ValueError("config.io.input is required")
    return cfg