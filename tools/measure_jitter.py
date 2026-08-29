#!/usr/bin/env python3
"""抖动量化对比（任务 2-3 P1 验收门 1.3b）。

提取每页同栏内相邻段落块的段间空隙与行距（行 pitch），输出
median / mean / stdev / 极差(range) / 方差(variance)——用于对比
逐段 insert_htmlbox（v0.7.0 基线）与页级 Story 接管的基线连续性。

用法：
    .venv/bin/python tools/measure_jitter.py <output.pdf> [更多.pdf ...]

说明：
- 段落块取 get_text("dict") 的 block 粒度（渲染产物的段=独立块）；
- 只统计同栏内 y 相邻、空隙 0~40pt 的块对（大空隙是图/表/标题分隔，
  与 baseline 网格无关，不计入）；
- 行距（line pitch）取块内相邻行 y0 差，统计其分布宽度（值越集中，
  网格越连续；页级 Story 的目标是全页共享同一 pitch 网格）。
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pymupdf

MAX_GAP = 40.0


def _col_of(rect: pymupdf.Rect, page_w: float) -> int:
    return 1 if (rect.x0 + rect.x1) / 2 > page_w / 2 else 0


def collect(pdf_path: str) -> dict:
    doc = pymupdf.open(pdf_path)
    gaps: list[float] = []
    pitches: list[float] = []
    residuals: list[float] = []   # 跨段基线网格残差（v0.7.1 新增）
    n_blocks = 0
    for page in doc:
        pw = page.rect.width
        blocks = []
        d = page.get_text("dict")
        for b in d["blocks"]:
            if b.get("type") != 0:
                continue
            lines = b.get("lines") or []
            if len(lines) < 1:
                continue
            r = pymupdf.Rect(b["bbox"])
            ys = sorted(l["bbox"][1] for l in lines)
            blocks.append({"rect": r, "col": _col_of(r, pw),
                           "ys": ys, "lines": lines})
            n_blocks += 1
        for col in (0, 1):
            cb = sorted((b for b in blocks if b["col"] == col),
                        key=lambda b: b["rect"].y0)
            for a, nxt in zip(cb, cb[1:]):
                g = nxt["rect"].y0 - a["rect"].y1
                if 0 <= g <= MAX_GAP:
                    gaps.append(round(g, 2))
            for b in cb:
                ys = b["ys"]
                for y0, y1 in zip(ys, ys[1:]):
                    p = y1 - y0
                    if 5 <= p <= 40:
                        pitches.append(round(p, 2))
            # 跨段网格残差：相邻段（首末行间距 d）对后段行距 pitch 取模，
            # 到最近整数倍的偏离。连续网格（页级 Story）→ 残差聚在 0 附近；
            # 逐段独立排版 → 引擎各自取整，残差在 [0, pitch/2] 摊开。
            for a, nxt in zip(cb, cb[1:]):
                if len(a["ys"]) < 1 or len(nxt["ys"]) < 2:
                    continue
                ny = sorted(nxt["ys"])
                npitch = ny[1] - ny[0]
                if not (5 <= npitch <= 40):
                    continue
                d = ny[0] - a["ys"][-1]
                if d <= 0:
                    continue
                res = d % npitch
                residuals.append(round(min(res, npitch - res), 3))
    doc.close()
    return {"gaps": gaps, "pitches": pitches, "blocks": n_blocks,
            "residuals": residuals}


def report(name: str, vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    vals_sorted = sorted(vals)
    return {
        "n": len(vals),
        "median": round(statistics.median(vals), 2),
        "mean": round(statistics.mean(vals), 2),
        "stdev": round(statistics.pstdev(vals), 3),
        "variance": round(statistics.pvariance(vals), 3),
        "min": vals_sorted[0],
        "max": vals_sorted[-1],
        "range": round(vals_sorted[-1] - vals_sorted[0], 2),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    for pdf in argv[1:]:
        c = collect(pdf)
        g = report(pdf, c["gaps"])
        p = report("pitch", c["pitches"])
        pitch_spread = None
        if p.get("n"):
            from collections import Counter
            cnt = Counter(c["pitches"])
            top = cnt.most_common(1)[0][1]
            pitch_spread = {"distinct": len(cnt), "top_freq": top,
                            "top_ratio": round(top / len(c["pitches"]), 3)}
        r = report("resid", c.get("residuals") or [])
        near0 = 0
        if r.get("n"):
            near0 = sum(1 for v in c["residuals"] if v <= 0.15)
        print(f"== {Path(pdf).name}")
        print(f"   blocks={c['blocks']}")
        print(f"   段间空隙: {g}")
        print(f"   行距 pitch: {p}")
        print(f"   pitch 分布: {pitch_spread}")
        print(f"   跨段网格残差: {r}  残差≤0.15pt 占比="
              f"{(near0 / r['n']) if r.get('n') else 0:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
