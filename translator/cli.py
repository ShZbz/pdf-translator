"""CLI 入口：python -m translator.cli -c config.yaml [--dry-run] [-v]"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(prog="translator",
                                 description="学术 PDF 整册翻译工具")
    ap.add_argument("-c", "--config", required=True, help="config.yaml 路径")
    ap.add_argument("--dry-run", action="store_true",
                    help="只跑布局/水印/渲染管线,跳过 LLM 翻译(全部保留原文)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="stderr 日志加时间戳")
    args = ap.parse_args()

    from .config import load_config
    from .pipeline import translate_document

    cfg = load_config(args.config)

    # OpenAI 兼容 client（延迟 import：无网络/无 key 时仍可干跑）
    client = None
    if not args.dry_run:
        base_url, api_key = cfg.llm.resolve()
        if base_url:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key or "sk-noop")

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
