"""D4 术语表：外部 YAML {src: dst}，注入 system prompt + 译后校验。"""
from __future__ import annotations

from pathlib import Path

import yaml


class Glossary:
    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping: dict[str, str] = dict(mapping or {})

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"glossary file not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("glossary YAML must be a flat {src: dst} mapping")
        return cls({str(k): str(v) for k, v in data.items()})

    def prompt_block(self) -> str:
        """注入 system prompt 的术语表全文块。"""
        if not self.mapping:
            return ""
        lines = ["Glossary (translate these terms EXACTLY as specified):"]
        for k, v in self.mapping.items():
            lines.append(f"- {k} => {v}")
        return "\n".join(lines)

    def check_translation(self, translated: str) -> list[str]:
        """译后校验：返回违反术语锁定的词条（源词出现而目标词未出现）。"""
        violations = []
        low = translated
        for k, v in self.mapping.items():
            if k in low and v not in translated:
                violations.append(k)
        return violations

    def fix_translation(self, translated: str) -> tuple[str, list[str]]:
        """v0.7.0 术语锁确定性修复：源词逐字残留时原位替换为目标词。

        替代 logit_bias 类约束解码的零调用方案——logit_bias 需要目标
        语言 token 化才能构造偏置向量，OpenAI 兼容网关多数静默忽略该
        参数（虚假安全感），不可跨 provider 依赖。确定性替换只处理
        "源词逐字出现在译文里"这一明确违例（LLM 漏译术语的典型形态），
        替换后词义与 prompt 注入路径完全一致，无创造性风险。
        返回 (修复后文本, 已修复词条)；无修复返回原文与空表。
        只替换每个词条的首处出现（多处的重复术语留给下一轮校验，
        防御性保守——修一处即满足锁定校验）。
        """
        fixed = translated
        done: list[str] = []
        for k, v in self.mapping.items():
            if v in fixed:
                continue          # 目标词已在：锁定已满足
            if k in fixed:
                fixed = fixed.replace(k, v, 1)
                done.append(k)
        return fixed, done
