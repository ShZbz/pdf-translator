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
