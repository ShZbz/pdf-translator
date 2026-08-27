"""参考文献条目重切（extract 层）：块内嵌 ⏎[N] 行首的按条目边界拆块。

v0.2.2: 边界直接取 word 级 "[N]" token 的真实 y0（文本行数与视觉行数
不一一对应,按行数索引会漂移产出零高度 bbox——实测教训）。
条目 bbox = [本条目 [N] 词的 y0 - 1pt, 下一条目 [N] 词的 y0 或块底]。
"""
import re

import pymupdf

_REF_SPLIT_RE = re.compile(r"\n(?=\[\d+\]\s)")
_ENTRY_TOKEN_RE = re.compile(r"^\[(\d+)\]$")


def split_ref_blocks(page, blocks: list[dict]) -> list[dict]:
    """把块内嵌的多条参考文献按 [N] 行首边界拆成独立块。

    触发条件：块文本含 \\n[N] 行首模式（条目边界嵌在块中）。

    v0.4.2: 全页 words 至多取一次——旧版对每个待拆块各跑一次
    clip 提取（参考文献页常有 10+ 个多条件目块 = 10+ 次全页词提取）。
    """
    words: list | None = None   # 惰性：首个待拆块出现时才取全页词
    out: list[dict] = []
    for b in blocks:
        parts = _REF_SPLIT_RE.split(b["text"])
        if len(parts) <= 1:
            out.append(b)
            continue
        if words is None:
            words = page.get_text("words")
        # 该块区域内的全部词,找每个条目编号 token 的 y0
        bb = b["bbox"]
        entry_y: dict[int, float] = {}
        for w in words:
            # 词与块 bbox 相交即属于该块（等价旧版 clip 语义）
            if not (w[0] < bb.x1 and w[2] > bb.x0
                    and w[1] < bb.y1 and w[3] > bb.y0):
                continue
            m = _ENTRY_TOKEN_RE.match(w[4])
            if m:
                n = int(m.group(1))
                # 同一条目的编号词只记一次（取最先出现的）
                if n not in entry_y:
                    entry_y[n] = w[1]
        # parts[i] 以 [N] 开头 → 取其 y 作上边界
        starts: list[float | None] = []
        for part in parts:
            head_m = re.match(r"^\[(\d+)\]", part.strip())
            if head_m:
                n = int(head_m.group(1))
                starts.append(entry_y.get(n))
            else:
                starts.append(None)   # 首段可能无编号（续行开头）
        # 无编号首段 → 用块顶
        if starts and starts[0] is None:
            starts[0] = b["bbox"].y0
        # 构造子块 bbox
        bounds: list[tuple[float, float]] = []
        for i, part in enumerate(parts):
            y_top = starts[i]
            if y_top is None:
                # 找不到编号词（异常布局）→ 整块兜底
                bounds.append((b["bbox"].y0, b["bbox"].y1))
                continue
            # 下边界 = 下一个有坐标的起点或块底
            y_bot = b["bbox"].y1
            for s in starts[i + 1:]:
                if s is not None:
                    y_bot = s
                    break
            if y_bot <= y_top:      # 防倒挂/零高
                y_bot = min(b["bbox"].y1, y_top + 12.0)
            bounds.append((y_top, y_bot))
        for part, (yt, yb) in zip(parts, bounds):
            # v0.2.2 打磨: 不再上扩 1pt——相邻条目共享边界线,上扩会把
            # 本条目首行推进上一条目末行的空间,渲染后视觉叠印
            # （实测 paper3 p8: 6 处条目间行重叠皆源于此）
            sub = pymupdf.Rect(b["bbox"].x0, yt,
                               b["bbox"].x1, yb)
            nb = dict(b)
            nb["text"] = part
            nb["bbox"] = sub
            out.append(nb)
    return out
