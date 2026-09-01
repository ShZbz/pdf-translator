"""CLI 入口：python -m translator.cli -c config.yaml [--dry-run] [-v] [-p quick]

v0.8.5: 失败路径友好报错（FAILPATHS I8 收口）——坏 PDF/文件不存在/
配置非法等可预期失败打印一行 ERROR + 提示（退出码 2），不再甩裸
traceback；未预期异常仍完整上抛（bug 报告需要堆栈）。
"""
from __future__ import annotations

import argparse
import sys

import yaml


def _run(args) -> int:
    from .config import apply_provider_tuning, load_config
    from .pipeline import translate_document

    cfg = load_config(args.config)

    quick = args.preset == "quick"
    if quick:
        applied = apply_provider_tuning(cfg)
        if applied:
            print(f"[quick] 已按 provider '{cfg.llm.provider}' 档位应用推荐"
                  f"参数: {', '.join(applied)}", file=sys.stderr)
        if not (cfg.io.pages or "").strip():
            cfg.io.pages = "1-2"
        print(f"[quick] 试译范围: io.pages={cfg.io.pages!r}（配置里已写 "
              f"io.pages 则尊重配置）", file=sys.stderr)

    # v0.8.5: 页级重译命令（管线 actor 化第一步）——指定页的全部翻译
    # 单元绕过翻译缓存强制重译（1-based，与 io.pages 同风格），渲染结果
    # 缓存按译文内容寻址自动失效重渲染
    force_pages: set[int] = set()
    if args.retranslate:
        from .config import parse_page_ranges
        n_pages = _peek_page_count(cfg.io.input)
        force_pages = set(parse_page_ranges(args.retranslate, n_pages))
        if not force_pages:
            print(f"WARNING: --retranslate {args.retranslate!r} 未选中任何"
                  f"有效页（共 {n_pages} 页），忽略", file=sys.stderr)

    # OpenAI 兼容 client（延迟 import：无网络/无 key 时仍可干跑）
    client = None
    if not args.dry_run:
        base_url, api_key = cfg.llm.resolve()
        if base_url:
            # v0.8.2: timeout 透传——SDK 默认 600s，慢/挂网关下单批可
            # 无限拖住整文档（实测 opencode go 偶发流挂起）。
            # v0.8.3: LLMClientPool 统一构造——自持连接池（楔死终结器）
            # + SDK max_retries=0（重试单层化，见 llm.LLMClientPool）
            from translator.llm import LLMClientPool
            client = LLMClientPool(base_url, api_key or "sk-noop",
                                   float(getattr(cfg.llm, "timeout", 120.0)
                                         or 120.0))
        else:
            # v0.8.3: 旧版静默降级干跑——provider 拼错/漏配 base_url 时
            # 用户拿到的是「原文原样输出」，却以为翻译完成了。显式告警。
            print("WARNING: 无法确定 API 地址（llm.base_url 为空且 provider "
                  f"{cfg.llm.provider!r} 无预设）——本次按 dry-run 运行，"
                  "输出保留原文", file=sys.stderr)

    if quick and client is not None:
        # 1) dry-run 冒烟（布局/字体/渲染管线零成本验证）
        print("[quick] 第 1/2 步：dry-run 验证管线…", file=sys.stderr)
        s0 = translate_document(cfg, client=None, verbose=args.verbose)
        for w in s0["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        print(f"[quick] dry-run OK → {s0['output']}", file=sys.stderr)
        # 2) 同一子集真实试译（dry-run 产物被覆盖；布局缓存复用）
        print("[quick] 第 2/2 步：试译前 2 页…", file=sys.stderr)
        stats = translate_document(cfg, client=client, verbose=args.verbose,
                                   retranslate_pages=force_pages or None)
        for w in stats["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        print(f"output: {stats['output']} (quick 试译)")
        print(f"pages={stats['pages']} paras={stats['paragraphs']} "
              f"llm_calls={stats['calls']}")
        print("[quick] 效果确认后跑全量：", file=sys.stderr)
        print(f"        python -m translator.cli -c {args.config}",
              file=sys.stderr)
        print("        （项目级翻译缓存已就绪——全量只为未译段落付调用）",
              file=sys.stderr)
        return 0

    stats = translate_document(cfg, client=client, verbose=args.verbose,
                               retranslate_pages=force_pages or None)
    mode = " (dry-run: 未调用 LLM)" if args.dry_run else ""
    for w in stats["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)
    print(f"output: {stats['output']}{mode}")
    print(f"pages={stats['pages']} paras={stats['paragraphs']} "
          f"llm_calls={stats['calls']}")
    return 0


def _peek_page_count(input_path: str) -> int:
    """--retranslate 页码校验用：只开文档数页数，失败返回大数（不拦截）。"""
    try:
        import pymupdf
        with pymupdf.open(input_path) as doc:
            return len(doc)
    except Exception:
        return 10 ** 9


# 可预期失败 → 一行 ERROR + 指向性提示（FAILPATHS I8：旧版裸 traceback，
# 可读性差且容易吓到用户；未预期异常保持完整堆栈便于报 bug）。
# 注意 pymupdf 自带 FileNotFoundError/PermissionError 等同名异常（都挂在
# RuntimeError 下，不是内建 OSError 族）——按类名匹配两边通吃。
def _hint_for(e: BaseException) -> str:
    name = type(e).__name__
    if isinstance(e, yaml.YAMLError):
        return "config.yaml 语法错误"
    if name == "FileNotFoundError" or "no such file" in str(e):
        return "输入文件不存在，检查路径拼写"
    if name == "PermissionError":
        return "无权限读取输入文件"
    if name == "IsADirectoryError":
        return "输入路径是目录，需要指向 .pdf 文件"
    if "as type pdf" in str(e) or "broken" in str(e).lower():
        return "文件不是有效 PDF 或已损坏"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="translator",
                                 description="学术 PDF 整册翻译工具")
    ap.add_argument("-c", "--config", required=True, help="config.yaml 路径")
    ap.add_argument("--dry-run", action="store_true",
                    help="只跑布局/水印/渲染管线,跳过 LLM 翻译(全部保留原文)")
    # v0.7.1: 新手上手预设档——防"跑了 10 分钟发现模型选错"
    ap.add_argument("-p", "--preset", choices=("quick",),
                    help="quick=dry-run 验证 → 按 provider 档位填推荐参数 → "
                         "前 2 页试译确认效果；确认后再跑全量（缓存使全量"
                         "只为未译段落付调用）")
    # v0.8.5: 页级重译（1-based，如 "3" 或 "1,3-4"；与 io.pages 同语法）
    ap.add_argument("--retranslate", metavar="PAGES", default="",
                    help="强制重译指定页（绕过翻译缓存；1-based，如 3 或 "
                         "1,3-4）——修完某页译文后只重付该页调用")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="stderr 日志加时间戳")
    args = ap.parse_args()

    try:
        return _run(args)
    except (ValueError, OSError, RuntimeError, yaml.YAMLError) as e:
        # ValueError=配置非法/页码段无效；OSError 含内建文件错误；
        # pymupdf 坏 PDF 的 FileDataError 与同名文件异常都是 RuntimeError
        # 子类（venv 实证）。这些错误的调用栈对排障无增益——一行说清 +
        # 指向提示即可；未预期异常不在此捕获，保持完整堆栈便于报 bug
        detail = str(e) or e.__class__.__name__
        print(f"ERROR: {detail}", file=sys.stderr)
        hint = _hint_for(e)
        if hint:
            print(f"  提示: {hint}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
