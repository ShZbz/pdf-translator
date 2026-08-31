"""配置键「端到端生效」审计（v0.8.4，PLAN 2-1 工具化）。

背景：v0.8.3 前出过一类回归——配置键在 config.py/config.example.yaml 里
都存在、load_config 也校验通过，但下游消费点读的是模块常量/旧参数，
配置改了不生效（reflow.segment_blocks 实例：4 个键就这么漏的）。
本工具把「键存在 ⇒ 真接线」做成可重复检查：

三向对照 + 消费点扫描：
  ① config.py dataclass 字段 ↔ config.example.yaml 键
     （含注释行里的可选键——YAML 解析器看不到 `# pages:`，纯 safe_load
     会漏报；SCHEME §5 的 flow style `io: {...}` 同理靠字段集兜底）
  ② 每个字段在 translator/（+server/run_ui 同键流转）里有无非 config.py
     的真实消费点——识别形态：`cfg.<section>.<field>` / `cfg.<field>` /
     `llm_eff.<field>`（LLMConfig.effective() 副本）/ `fit_cfg.<field>` /
     `getattr(cfg.x, "<field>", ...)` 字符串形态；输出 file:line
  ③ README.md 反向对照：文档提及的 section.key 必须真实存在
     （文档键过期 = 用户照着配 → _filtered 静默忽略 → 假配置）

输出「键 | 文档位置 | 消费点 | 状态」表；任一无消费/漂移行 → 退出码 1
（发布纪律：新增配置键的 PR 必须附无新增问题的本工具输出）。

用法：
    python tools/config_audit.py [--md] [--all]
      --md   输出 Markdown 表（默认对齐文本表）
      --all  连 OK 行也打印（默认只打印问题行 + 汇总）
"""
from __future__ import annotations

import re
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from translator.config import (Config, FeatureConfig, IOConfig, LLMConfig,  # noqa: E402
                               OCRConfig, OutputConfig, PerformanceConfig,
                               ReflowConfig, RenderConfig)
from translator.fit import FitConfig  # noqa: E402

# 段名 → dataclass（fit 在 fit.py，其余在 config.py）
SECTIONS: dict[str, type] = {
    "io": IOConfig, "llm": LLMConfig, "features": FeatureConfig,
    "performance": PerformanceConfig, "output": OutputConfig,
    "render": RenderConfig, "reflow": ReflowConfig, "fit": FitConfig,
    "ocr": OCRConfig,
}
# 顶层标量键（fonts 是自由 dict，见 FONTS_KEYS 特判）
TOP_KEYS = ("glossary_file",)
# dataclass 内部字段（非配置键）：LLMConfig._explicit 记录 YAML 显式键
_INTERNAL_FIELDS = {"_explicit"}
# fonts 段是自由 dict（无 dataclass 字段集）——合法键以 langs.py
# resolve_output_fonts 的读取清单为准（含 v0.2.2 旧名兼容键）
FONTS_KEYS = ("cjk", "body", "heading", "cjk_body", "cjk_heading")
# 有意的旧名兼容别名（langs.resolve_output_fonts 兼容读取）——不进
# example.yaml 也不算文档缺键，报告单列状态防门禁误报
LEGACY_ALIASES = {"fonts.cjk_body", "fonts.cjk_heading"}

# 消费点扫描的接收者形态（见模块 docstring ②；config.py 自身不算——
# 它是定义处不是消费处）。getattr 形态单独用字符串匹配。
_RECEIVER_VARS = {
    "cfg": None,          # cfg.<section>.<field> / cfg.<top>
    "llm_eff": "llm",     # LLMConfig.effective() 副本（pipeline 两处）
    "fit_cfg": "fit",     # FitConfig 实例（pipeline/render）
    "reflow_cfg": "reflow",
    "render_cfg": "render",
    "out_cfg": "output",
    "perf": "performance",
    "feat": "features",
    "io_": "io",
}
_SCAN_FILES = sorted(
    p for p in list((ROOT / "translator").glob("*.py"))
    + list((ROOT / "server").glob("*.py")) + [ROOT / "run_ui.py"]
    if p.name != "config.py" and p.name != "__init__.py"
)


def collect_fields() -> dict[str, list[str]]:
    """段名 → 字段名列表（dataclass 单一来源；fonts 是自由 dict 特判）。"""
    out: dict[str, list[str]] = {}
    for sec, dc in SECTIONS.items():
        out[sec] = [f.name for f in dc_fields(dc)
                    if f.name not in _INTERNAL_FIELDS]
    out[""] = list(TOP_KEYS)      # 顶层键（"" 段在报告里显示裸键名）
    out["fonts"] = list(FONTS_KEYS)
    return out


def parse_example_yaml() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """config.example.yaml → (活跃键, 注释提及键)。

    注释键（`# pages: "1-2"`）是真实支持的可选项——safe_load 看不见，
    不提取会把「文档化可选键」误报成 dataclass 缺 example 键。
    """
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    active: dict[str, set[str]] = {}
    data = yaml.safe_load(text) or {}
    for sec, body in data.items():
        if isinstance(body, dict):
            active[str(sec)] = {str(k) for k in body}
        else:
            active[str(sec)] = set()   # 标量段（glossary_file）无子键
    commented: dict[str, set[str]] = {}
    sec = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w]*):\s*(?:#.*)?$", line)
        if m:
            sec = m.group(1)
            continue
        # 注释里的键形如 `#   pages: "1-2"` / `# body: ""`（缩进注释）
        cm = re.match(r"^\s*#\s*([A-Za-z_][\w]*)\s*:", line)
        if cm and sec:
            commented.setdefault(sec, set()).add(cm.group(1))
    return active, commented


def find_consumptions(fields: dict[str, list[str]]) -> dict[str, list[str]]:
    """(section, field) → [file:line, ...] 消费点清单。

    只认可「配置对象接收者」上的属性访问/字符串 getattr——裸字段名
    相同（如 .size/.mode）不计数，防假阳性。
    """
    src_lines: list[tuple[Path, int, str]] = []
    for p in _SCAN_FILES:
        try:
            for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                src_lines.append((p, i, ln))
        except OSError:
            continue

    hits: dict[str, list[str]] = {}

    def _add(sec: str, field: str, path: Path, lineno: int) -> None:
        ref = f"{path.relative_to(ROOT).as_posix()}:{lineno}"
        lst = hits.setdefault(f"{sec}.{field}" if sec else field, [])
        if ref not in lst:
            lst.append(ref)

    for path, lineno, line in src_lines:
        for var, sec_of_var in _RECEIVER_VARS.items():
            # cfg.io.pages / cfg.pages（顶层）——cfg 的顶层与段级分开判
            if var == "cfg":
                for m in re.finditer(r"\bcfg\.([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)", line):
                    sec, fld = m.group(1), m.group(2)
                    if sec in SECTIONS:
                        _add(sec, fld, path, lineno)
                    elif sec == "fonts":
                        # cfg.fonts.get("cjk")——自由 dict 段
                        gm = re.search(r'\.get\("([A-Za-z_]\w*)"', line)
                        if gm:
                            _add("fonts", gm.group(1), path, lineno)
                for m in re.finditer(r"\bcfg\.([A-Za-z_][\w]*)\b", line):
                    fld = m.group(1)
                    if fld in TOP_KEYS or fld == "fonts":
                        _add("", fld, path, lineno)
            elif sec_of_var is not None:
                pat = rf"\b{re.escape(var)}\.([A-Za-z_]\w*)\b"
                for m in re.finditer(pat, line):
                    _add(sec_of_var, m.group(1), path, lineno)
        # 字符串 getattr：getattr(cfg.llm, "timeout", ...)——接收者任意，
        # 字段名命中即计（配置字段名与它类属性撞名时此处偏宽，可接受：
        # getattr 字符串形态本就是防御式读配置的主通道）
        for m in re.finditer(r'getattr\([^)]*?"([A-Za-z_]\w*)"', line):
            fld = m.group(1)
            for sec, flds in fields.items():
                if fld in flds:
                    _add(sec, fld, path, lineno)

    # config.py 内部：dataclass 方法体的 self.<field> 是真实接线（如
    # LLMConfig.resolve() 读 self.api_key/self.base_url、effective() 读
    # self.rpm_limit）——load_config/from_raw/_filtered 等解析代码用不到
    # self.<字段>，不会混入。这类消费点单列（定义文件内的方法接线）。
    cfg_py = ROOT / "translator" / "config.py"
    for i, line in enumerate(cfg_py.read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"\bself\.([A-Za-z_]\w*)\b", line):
            fld = m.group(1)
            for sec, flds in fields.items():
                if fld in flds:
                    ref = f"translator/config.py:{i}(self)"
                    lst = hits.setdefault(f"{sec}.{fld}" if sec else fld, [])
                    if ref not in lst:
                        lst.append(ref)

    # fonts 自由 dict 段：langs.resolve_output_fonts 的 cfg.get("body")
    # 读取链（receiver 名是局部 cfg——dict 非 Config，上面扫不到）
    langs_py = ROOT / "translator" / "langs.py"
    for i, line in enumerate(langs_py.read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r'\.get\("(cjk|body|heading|cjk_body|cjk_heading)"\)', line):
            _add("fonts", m.group(1), langs_py, i)
    return hits


def parse_readme_keys(fields: dict[str, list[str]]) -> dict[str, int]:
    """README.md 里提及的 section.key → 首次出现行号（反向对照用）。

    只认带段前缀的限定名（`llm.timeout`）；裸键名（文档里大量的
    `batch_char_budget` 简写）不参与存在性判定——歧义太高。
    """
    readme = ROOT / "README.md"
    if not readme.is_file():
        return {}
    known_secs = set(SECTIONS) | {"fonts"}
    out: dict[str, int] = {}
    for i, line in enumerate(readme.read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"\b([a-z_]+)\.([a-z_]+)\b", line):
            sec, fld = m.group(1), m.group(2)
            if sec in known_secs and fld not in ("py", "md", "yaml", "db",
                                                 "pdf", "txt"):
                out.setdefault(f"{sec}.{fld}", i)
    return out


def main() -> int:
    args = sys.argv[1:]
    md = "--md" in args
    show_all = "--all" in args

    fields = collect_fields()
    active, commented = parse_example_yaml()
    consumers = find_consumptions(fields)
    readme_keys = parse_readme_keys(fields)

    all_keys = [(sec, f) for sec, flds in fields.items() for f in flds]

    rows: list[dict] = []
    for sec, fld in all_keys:
        key = f"{sec}.{fld}" if sec else fld
        if sec == "":                      # 顶层标量键：example 顶层直接判
            doc = "example.yaml" if fld in active or fld in commented else "—"
        else:
            ex = active.get(sec, set())
            cm = commented.get(sec, set())
            if fld in ex:
                doc = "example.yaml"
            elif fld in cm:
                doc = "example.yaml(注释)"
            else:
                doc = "—"
        cons = consumers.get(key) or []
        if key in LEGACY_ALIASES:
            status = "旧名兼容(允许)"
        else:
            status = "OK" if cons else "无消费"
            if doc == "—":
                status = "example 缺键" + ("/无消费" if not cons else "")
        rows.append({"key": key, "doc": doc,
                     "cons": ", ".join(cons[:4])
                     + (f" +{len(cons) - 4}" if len(cons) > 4 else ""),
                     "n_cons": len(cons), "status": status})

    # example.yaml 里的未知键（dataclass 没有对应字段——load_config 的
    # _filtered 会静默忽略，拼错键的典型形态）
    for sec, keys in active.items():
        if sec in SECTIONS:
            for k in keys - set(fields.get(sec, [])):
                rows.append({"key": f"{sec}.{k}", "doc": "example.yaml",
                             "cons": "—", "n_cons": 0, "status": "未知键(拼错?)"})
        elif sec not in TOP_KEYS and sec not in ("fonts",):
            rows.append({"key": sec, "doc": "example.yaml", "cons": "—",
                         "n_cons": 0, "status": "未知段(拼错?)"})

    # README 反向对照：文档提及但键不存在
    valid_keys = {f"{s}.{f}" if s else f for s, f in all_keys}
    for key, line in readme_keys.items():
        if key not in valid_keys:
            rows.append({"key": key, "doc": f"README.md:{line}",
                         "cons": "—", "n_cons": 0,
                         "status": "README 键不存在(文档过期)"})

    problems = [r for r in rows if r["status"] not in ("OK", "旧名兼容(允许)")]
    shown = rows if show_all else (problems if problems else rows)

    if md:
        print("| 键 | 文档位置 | 消费点 | 状态 |")
        print("|---|---|---|---|")
        for r in shown:
            print(f"| `{r['key']}` | {r['doc']} | "
                  f"{r['cons'] or '—'} | {r['status']} |")
    else:
        w_key = max(len(r["key"]) for r in shown) if shown else 10
        w_doc = max(len(r["doc"]) for r in shown) if shown else 8
        w_con = max(len(r["cons"]) for r in shown) if shown else 4
        print(f"{'键'.ljust(w_key)} | {'文档位置'.ljust(w_doc)} | "
              f"{'消费点'.ljust(w_con)} | 状态")
        print("-" * (w_key + w_doc + w_con + 40))
        for r in shown:
            print(f"{r['key'].ljust(w_key)} | {r['doc'].ljust(w_doc)} | "
                  f"{(r['cons'] or '—').ljust(w_con)} | {r['status']}")

    n_ok = sum(1 for r in rows if r["status"] == "OK")
    print(f"\n汇总: {n_ok}/{len(rows)} OK；问题 {len(problems)} 行"
          + ("" if problems else " ✓")
          + "（--all 看全表 / --md 出 Markdown）")
    for r in problems:
        print(f"  !! {r['key']}: {r['status']}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
