"""v0.6.0 排版自适配（fit）：测量基座 + 样式级全局因子 + 降级阶梯 + 源头控长。

设计原则（对齐专业 DTP 本地化流程：改样式表，不改单个文本框）：
- 块内自由重排：整段译文交给 Story 引擎在原段落外接框里重新断行
  （CJK 逐字可断 + 避头尾，西文按词断），断行位置允许与原文不同
- 样式级因子：第一遍测量全部段落各自"能装下的最大字号因子"，
  按样式类（正文/图注/文献条目/表格）各取一个统一因子，第二遍
  用统一因子重渲染——同类元素字号永远一致，杜绝"有的大有的小"
- 降级阶梯（膨胀方向 ZH→EN 等按序尝试）：
  ① 重排（免费，Story 自动） ② 向下扩框（≤ expand_lines 行高，
  下探不得越过同栏下邻元素） ③ 微压行距/字距（lead_steps/tracking，
  每级重测） ④ 类级字号收缩（下限 min_scale） ⑤ 仍放不下 → 告警
  溢出 + 极端兜底（scale_low=0 保证必出字），绝不静默丢段
- 收缩方向（EN→ZH）：文档整体填充率低时正文微升（body_boost，
  行业惯例同 pt 下 CJK 观感偏小），caption/ref/标题/单元格不参与
- 源头控长（estimate_char_budget）：翻译前按目标框实测字宽估算
  字符预算喂给 LLM，超预算单段带强约束重译一次，从上游消灭溢出
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf

# fit_scale 测量下探硬底（防病态段把整类拖到不可读）：类因子另有 min_scale
_HARD_FLOOR = 0.5
# 视为"无压缩"的因子容差（浮点二分残留）
_EPS = 0.004
# 测量二分误差安全余量（渲染字号再让 0.2%，钉死边界段）
_MEASURE_MARGIN = 0.002

# 拉丁/西里尔字宽标定串（数字+大小写混合，学术文本典型分布）
_CALIB_LATIN = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789")


@dataclass
class FitConfig:
    """fit 段配置（全部有默认值，风格对齐 ocr.mode）。"""
    mode: str = "auto"            # auto=两遍式+阶梯；off=旧行为（元素级引擎缩放）
    expand_lines: float = 1.0     # 向下扩框上限（基准行高倍数；0=禁用）
    lead_steps: list = field(default_factory=lambda: [0.95, 0.92])
    tracking: float = 0.995       # letter-spacing 系数（1=关闭）
    min_scale: float = 0.78       # 类字号下限
    body_boost: float = 1.05      # 收缩方向正文微升上限（≤1=关闭）

    @staticmethod
    def from_raw(raw: dict | None) -> "FitConfig":
        """YAML fit 段 → FitConfig（未知键忽略，mode 白名单）。"""
        raw = dict(raw or {})
        # YAML 1.1 把裸 off/on/no 解析成 bool——语义上 off=False 就是关闭
        if isinstance(raw.get("mode"), bool):
            raw["mode"] = "auto" if raw["mode"] else "off"
        cfg = FitConfig(**{k: v for k, v in raw.items()
                           if k in FitConfig.__dataclass_fields__})
        if cfg.mode not in ("auto", "off"):
            raise ValueError(
                f"fit.mode 必须是 'auto' 或 'off'，当前 {cfg.mode!r}")
        cfg.expand_lines = max(0.0, float(cfg.expand_lines))
        cfg.min_scale = min(max(float(cfg.min_scale), 0.5), 1.0)
        cfg.body_boost = max(1.0, float(cfg.body_boost))
        cfg.tracking = min(max(float(cfg.tracking or 1.0), 0.9), 1.0)
        steps = sorted({min(max(float(s), 0.8), 1.0)
                        for s in (cfg.lead_steps or []) if float(s) < 1.0},
                       reverse=True)
        cfg.lead_steps = steps
        return cfg


def fit_ladder(cfg: FitConfig, lead_steps: list[float] | None = None) \
        -> list[tuple[float, float]]:
    """降级阶梯的 (line-height 系数, tracking 系数) 级别序列。

    首级恒为 (1.0, 1.0)（原样重排）；其后逐级压行距；最深一级附加字距
    压缩。字号收缩不进阶梯——它在整个阶梯走完后仍溢出时按类因子下限执行。
    标题类可传更深的 lead_steps（单行标题行距视觉不可感，是标题唯一的
    兜底杠杆——f 恒 1 不参与字号收缩）。
    """
    steps = sorted({min(max(float(s), 0.8), 1.0)
                    for s in (lead_steps if lead_steps is not None
                              else (cfg.lead_steps or []))
                    if float(s) < 1.0}, reverse=True)
    ladder: list[tuple[float, float]] = [(1.0, 1.0)]
    for lead in steps:
        ladder.append((lead, 1.0))
    if cfg.tracking < 1.0 and steps:
        ladder.append((steps[-1], cfg.tracking))
    return ladder


_HEADING_CLASSES = ("title", "sec_title", "subsec_title", "head_plain")


def measure_fit_factor(rect: "pymupdf.Rect", html: str, css: str,
                       archive: "pymupdf.Archive | None",
                       f_min: float = _HARD_FLOOR,
                       f_max: float = 1.0) -> tuple[float, bool]:
    """测量一段 HTML 在给定 CSS（字号=基准）下能装下的最大字号因子。

    与 insert_htmlbox 同一引擎语义（Story + FZ_PLACE_STORY_FLAG_NO_OVERFLOW
    + body{margin:1px} 前缀），fit_scale 二分由 MuPDF C 层完成（~1ms/段）。
    返回 (因子, 是否在 [f_min, f_max] 内装下)：因子 < f_min 表示硬溢出
    （连 f_min 都装不下，渲染期走告警+兜底路径）。
    """
    import pymupdf._mupdf as mupdf
    w, h = rect.width, rect.height
    if w < 5 or h < 5 or not html.strip():
        return 1.0, True
    story = pymupdf.Story(html=html, user_css="body {margin:1px;}" + css,
                          archive=archive)
    try:
        fit = story.fit_scale(pymupdf.Rect(0, 0, w, h),
                              scale_min=1.0 / f_max, scale_max=1.0 / f_min,
                              flags=mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW)
    except Exception:
        return f_min, False
    f = 1.0 / fit.parameter if fit.parameter else f_min
    return f, bool(fit.big_enough)


def _quantile(sorted_vals: list[float], q: float) -> float:
    """已排序列表的分位数（线性插值）。"""
    if not sorted_vals:
        return 1.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


# 系统性溢出判别门槛：类内溢出段占比 ≥ 此值才走类级收缩
# （EN→ZH 收缩方向实测溢出率 7-11%，ZH→EN 膨胀方向接近 100%）
_SYSTEMIC_RATIO = 0.30
# 垂直填充行距上限（倍数；lh 1.35×1.12≈1.51，再大观感发飘）
_FILL_LEAD_CAP = 1.12
# 填充目标（行距撑到框高的 97% 为止，留 3% 防边界翻车）
_FILL_TARGET = 0.97


def compute_style_factors(spec_groups: dict[str, list[dict]],
                          cfg: FitConfig,
                          make_css: "callable",
                          archive: "pymupdf.Archive | None",
                          warnings: list[str] | None = None,
                          log=None) -> dict[str, dict]:
    """两遍式核心：按样式类算统一 (因子, 行距级, 字距级)。

    spec_groups: {类名: [spec...]}（spec 至少含 rect/html/tag，由
    render.collect_para_specs 产出——测量与渲染共用同一份规格）。
    make_css(spec, factor, lead, tracking) → CSS（因子进 font-size）。

    v0.6.1 方向判别（修复 v0.6.0"收缩方向全局陪绑缩小"回归）：
    - 类内溢出段占比 ≥ _SYSTEMIC_RATIO → 系统性溢出（ZH→EN 膨胀方向）：
      走降级阶梯 + 类因子 min(f_i) clamp [min_scale, 1]，全体统一缩小
    - 占比 < 门槛 → 孤立溢出（EN→ZH 收缩方向）：类因子恒 1.0，个别紧段
      用 per-spec override（降因子→降行距→行距阶梯→引擎兜底）消化，
      绝不让少数段把全类拖下水；body 类反向填充：
      ① 字号微升 min(body_boost, headroom 的 Q25)（分位数对离群段鲁棒，
        v0.6.0 的 min 规则会被 1 个紧段拖成不升反降）
      ② 垂直填充行距：撑到框高 97%（clamp ≤1.12）——把收缩方向的框底
        空白吃进段落内部（行距变透气），段落间 dangling 空隙就地消失，
        "译文按原版位重排"在视觉上成立
    - 标题类不缩字号（两级路径一致）；单元格/文献条目不参与微升/填充
    """
    warn = warnings if warnings is not None else []

    def _w(msg: str) -> None:
        warn.append(msg)
        if log is not None:
            log(msg)

    def _measure(spec: dict, factor: float, lead: float, track: float,
                 f_max: float = 1.0) -> float:
        f, _ok = measure_fit_factor(
            spec["rect"], spec["html"],
            make_css(spec, factor, lead, track),
            archive, f_min=_HARD_FLOOR, f_max=f_max)
        return f

    def _ladder_fits(spec: dict, ladder: list[tuple[float, float]],
                     base_factor: float = 1.0) -> dict | None:
        """按行距阶梯给单个段找能装下的级别（扩框已在 rect 里）。"""
        for lead, track in ladder[1:]:
            if _measure(spec, base_factor, lead, track) >= 1.0 - _EPS:
                return {"factor": base_factor, "lead": lead,
                        "tracking": track}
        return None

    factors: dict[str, dict] = {}
    for key, specs in spec_groups.items():
        if not specs:
            factors[key] = {"factor": 1.0, "lead": 1.0, "tracking": 1.0}
            continue
        heading_cls = key in _HEADING_CLASSES
        ladder_cls = fit_ladder(cfg) if not heading_cls else fit_ladder(
            cfg, list(cfg.lead_steps)
            + [s for s in (0.88, 0.85)
               if not cfg.lead_steps or s < cfg.lead_steps[-1]])

        # ---- 第一遍：基准级 (1.0, 1.0) 的 f_i ----
        fs0 = [_measure(s, 1.0, 1.0, 1.0) for s in specs]
        n_tight = sum(1 for f in fs0 if f < 1.0 - _EPS)
        systemic = n_tight / len(specs) >= _SYSTEMIC_RATIO

        if not systemic:
            # ---- 孤立溢出（收缩方向/正常文档）：类恒 1.0 + body 反向填充 ----
            factor, lead, tracking = 1.0, 1.0, 1.0
            if key == "body" and cfg.body_boost > 1.0:
                # headroom 一次测量（f_max 放开），fill = 1/headroom
                headrooms = sorted(_measure(s, 1.0, 1.0, 1.0,
                                            f_max=_FILL_LEAD_CAP * 3.0)
                                   for s in specs)
                avg_fill = sum(1.0 / max(h, 1e-6) for h in headrooms) \
                    / len(headrooms)
                if avg_fill < 0.85:
                    boost = min(cfg.body_boost, _quantile(headrooms, 0.25))
                    if boost > 1.0 + _EPS:
                        factor = boost
                        if log is not None:
                            log(f"fit[{key}]: fill {avg_fill:.2f} < 0.85 -> "
                                f"boost x{boost:.3f}")
                        # 垂直填充：升字号后重测 headroom，行距撑到 97%
                        hs2 = sorted(_measure(s, factor, 1.0, 1.0,
                                              f_max=_FILL_LEAD_CAP * 1.6)
                                     for s in specs)
                        lead_fill = min(_FILL_LEAD_CAP,
                                        _FILL_TARGET * _quantile(hs2, 0.15))
                        if lead_fill > 1.0 + _EPS:
                            lead = lead_fill
                            if log is not None:
                                log(f"fit[{key}]: fill-lead x{lead:.3f} "
                                    f"(eat box slack into leading)")
            # per-spec verify：装不下 (factor, lead) 的段逐级降级，
            # 绝不让它把全类拖下水（v0.6.0 回归根因）
            n_override = 0
            for s in specs:
                if _measure(s, factor, lead, tracking) >= 1.0 - _EPS:
                    continue
                ov = None
                if factor > 1.0 and _measure(s, factor, 1.0, 1.0) \
                        >= 1.0 - _EPS:
                    ov = {"factor": factor, "lead": 1.0}   # 只退填充行距
                elif _measure(s, 1.0, 1.0, 1.0) >= 1.0 - _EPS:
                    ov = {"factor": 1.0, "lead": 1.0}      # 回基准字号
                else:
                    ov = _ladder_fits(s, ladder_cls)       # 行距阶梯
                if ov is not None:
                    s["_fit_override"] = ov
                    n_override += 1
                else:
                    # 阶梯也装不下：引擎逐段缩放兜底（渲染期告警）
                    _w(f"fit[{key}]: {s['tag']} cannot fit even at 100% "
                       f"after ladder; engine will compress it")
            if n_override and log is not None:
                log(f"fit[{key}]: {n_override}/{len(specs)} para(s) got "
                    f"per-spec override (isolated overflow)")
            factors[key] = {"factor": factor, "lead": lead,
                            "tracking": tracking}
            continue

        # ---- 系统性溢出（膨胀方向）：阶梯 + 类因子 ----
        failing = [i for i, f in enumerate(fs0) if f < 1.0 - _EPS]
        cur_fs = list(fs0)
        chosen_level = ladder_cls[0]
        for lv_lead, lv_track in ladder_cls[1:]:
            new_failing = []
            for i in failing:
                f = _measure(specs[i], 1.0, lv_lead, lv_track)
                cur_fs[i] = f
                if f < 1.0 - _EPS:
                    new_failing.append(i)
            chosen_level = (lv_lead, lv_track)
            failing = new_failing
            if not failing:
                break
        f = min(cur_fs)
        if heading_cls:
            # 标题不缩字号：阶梯（扩框+行距+字距）用尽后仍溢出的标题交给
            # 渲染引擎逐段轻缩并告警（宁微缩也不溢出/丢字）
            if f < 1.0 - _EPS:
                _w(f"fit[{key}]: heading class cannot fit at 100% even "
                   f"after ladder ({f:.2f}); engine will compress "
                   f"individual headings")
            factors[key] = {"factor": 1.0,
                            "lead": chosen_level[0],
                            "tracking": chosen_level[1]}
            continue
        if f < cfg.min_scale:
            hard = [specs[i]["tag"] for i, ff in enumerate(cur_fs)
                    if ff < cfg.min_scale]
            _w(f"fit[{key}]: factor clamped {f:.2f} -> {cfg.min_scale:.2f}; "
               f"{len(hard)} para(s) may overflow: "
               + ", ".join(hard[:6]) + ("…" if len(hard) > 6 else ""))
            f = cfg.min_scale
        elif f < 1.0:
            # 测量二分误差余量：约束段按 f 恰好渲染时可能在 0.1% 边界
            # 翻车（引擎再做 0.998 元素缩放 → 同类 0.02pt 不一致），
            # 让 0.2% 的余量把边界钉死在"装得下"一侧
            f = max(f * (1.0 - _MEASURE_MARGIN), _HARD_FLOOR)
        factors[key] = {"factor": f,
                        "lead": chosen_level[0], "tracking": chosen_level[1]}

    return factors


# ---- 源头控长（任务 E）：目标框字符预算 ----

def _avg_char_width(font: "pymupdf.Font | None", base_size: float,
                    cjk: bool) -> float:
    """目标语言平均字宽（pt）：CJK ≈ 1em；拉丁/西里尔用标定串实测。"""
    if cjk:
        return base_size
    f = font or pymupdf.Font("helv")
    return f.text_length(_CALIB_LATIN, fontsize=base_size) / len(_CALIB_LATIN)


def estimate_char_budget(rect: "pymupdf.Rect", base_size: float,
                         lh: float, font: "pymupdf.Font | None" = None,
                         cjk: bool = False) -> int:
    """目标框在基准字号下能容纳的目标语言字符数（×0.92 安全系数）。

    预算 = (行宽/字宽) × (框高/行高) × 0.92。CJK 按全宽 1em 估，
    拉丁/西里尔用目标字体实测标定串宽度——预算只求量级正确
    （LLM 提示词有 15% 容差 + 渲染阶梯兜底，不追求逐字精确）。
    """
    if rect.width < 5 or rect.height < 5 or base_size <= 0:
        return 0
    cw = _avg_char_width(font, base_size, cjk)
    if cw <= 0:
        return 0
    chars_per_line = rect.width / cw
    n_lines = rect.height / (base_size * max(lh, 0.8))
    return int(chars_per_line * n_lines * 0.92)
