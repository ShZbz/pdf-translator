"""CLI 入口：python -m translator.cli -c config.yaml [--dry-run] [-v] [-p quick]"""
from __future__ import annotations

import argparse
import sys


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
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="stderr 日志加时间戳")
    args = ap.parse_args()

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

    # OpenAI 兼容 client（延迟 import：无网络/无 key 时仍可干跑）
    client = None
    if not args.dry_run:
        base_url, api_key = cfg.llm.resolve()
        if base_url:
            from openai import OpenAI
            # v0.8.2: timeout 透传——UI worker（jobs.py）v0.5.1 已修，CLI 漏了
            # 同款：SDK 默认 600s，慢/挂网关下单批可无限拖住整文档
            # （实测 opencode go 偶发流挂起），llm.timeout 配置对 CLI 不生效
            client = OpenAI(base_url=base_url, api_key=api_key or "sk-noop",
                            timeout=float(getattr(cfg.llm, "timeout", 120.0)
                                          or 120.0))

    if quick and client is not None:
        # 1) dry-run 冒烟（布局/字体/渲染管线零成本验证）
        print("[quick] 第 1/2 步：dry-run 验证管线…", file=sys.stderr)
        s0 = translate_document(cfg, client=None, verbose=args.verbose)
        for w in s0["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        print(f"[quick] dry-run OK → {s0['output']}", file=sys.stderr)
        # 2) 同一子集真实试译（dry-run 产物被覆盖；布局缓存复用）
        print("[quick] 第 2/2 步：试译前 2 页…", file=sys.stderr)
        stats = translate_document(cfg, client=client, verbose=args.verbose)
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

    stats = translate_document(cfg, client=client, verbose=args.verbose)
    mode = " (dry-run: 未调用 LLM)" if args.dry_run else ""
    for w in stats["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)
    print(f"output: {stats['output']}{mode}")
    print(f"pages={stats['pages']} paras={stats['paragraphs']} "
          f"llm_calls={stats['calls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
