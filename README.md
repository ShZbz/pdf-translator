# pdf-translator

<div align="center">
  <img src="assets/ui-main.png" alt="pdf-translator Web UI" width="720"/>
</div>

学术 PDF 整册翻译工具：保留原始版面（图片/表格/公式/双栏），15 种国际常用语言互译。

## 功能特性

- **渲染引擎 htmlbox 转正**：默认 `htmlbox` 走 pymupdf `insert_htmlbox`（Story 排版引擎）——断行/避头尾/试排降字号交给引擎，自带两端对齐与复杂文字整形（shaping/bidi）；段落与表格单元格全走该路径；`features.renderer: writer` 可切回 TextWriter 逐字排印（遗留路径），两种引擎可逐段对比
- **排版自适配**：两遍式渲染——先测量全文每段"能装下的最大字号因子"，按样式类（正文/图注/文献条目/表格）各取统一因子再重排回灌，同类元素字号永远一致（对齐 InDesign/Trados 式 DTP 流程：改样式表，不改单个文本框）。译文膨胀方向（如 ZH→EN，溢出段占比≥30%）自动走 降级阶梯：整段重排（Story 引擎）→ 向下扩框（≤1 行高，不越下邻元素/页底）→ 微压行距/字距 → 类级字号收缩（下限 0.78，标题类不缩）；孤立溢出（占比<30%，如 EN→ZH 收缩方向）绝不陪绑全类缩字——正文反向填充：字号微升 ×1.05 + 行距吃掉框底空白（段间距不增反降）。装不下必告警不静默，极端兜底保证必出字。`fit.mode: off` 可关闭
- **源头控长**：翻译前按每段目标框实测字宽估算字符预算，随 prompt 告知模型（HARD 上限）；超预算 15% 的译文单段带强约束重译一次（每文档上限 = 调用上限的 10%），预算档结果独立缓存——从上游消灭一大半溢出，尤其表格单元格/图注这类不可越界的硬约束场景
- **RTL 与天城文解锁**：阿拉伯语/希伯来语（自动 `direction:rtl`）与印地语（天城文整形）可用，字体链自动探测（Windows Arial/Nirmala，Linux Noto 系）；目标语言为 RTL/天城文时 writer 引擎会自动切换到 htmlbox 并告警
- **任务持久化**：队列/历史落 SQLite（`.ui_jobs.db`），服务重启自动恢复未完成任务重新排队——配合翻译缓存与**版面缓存**，重跑只剩增量段；历史跨重启可查（`GET /api/jobs`，含输入路径/警告/缓存统计）
- **版面缓存·段落级断点续跑**：版面结果按输入文件（路径+大小+mtime+引擎）落盘缓存，同一输入重跑跳过布局阶段直达翻译（`performance.layout_cache`，默认开）
- **UI 实时推送**：SSE 端点 `/api/jobs/current/stream` 事件流（进度/阶段/警告），EventSource 优先，断线自动重连携带 `Last-Event-ID` 续传（服务端有界事件日志按 id 补发错过的帧），浏览器不支持时自动回退轮询
- **任务历史面板**：UI 左下角时钟按钮打开历史列表（最近 50 条）——重跑（沿用历史输入/输出 + 当前配置）、打开输出 PDF、展开查看警告与缓存节省统计
- **LLM 配额自适应**：`llm.rpm_limit`/`llm.tpm_limit` 填入 provider 配额后自动换算调用间隔（60/RPM）与批字符预算（TPM/RPM × 3.2 字符/token × 0.8 安全系数），显式写出的配置项优先
- **任务队列**：UI/API 忙时提交自动入队接力执行，任务终态归档 history（`GET /api/jobs` 可查）
- **批字符预算组批**：LLM 组批按 ~3000 字符/批贪心装填（`batch_char_budget`），长短段不混批，降低长批超时失败率；`batch_size` 为每批段数上限
- **布局阶段多进程并行**：逐页版面分析互相独立，`ProcessPoolExecutor` 并行（`performance.layout_workers`，0=自动），任何并行故障自动回退串行
- **扫描页 OCR**：文字层 < 50 字符的页检出为扫描页，paddleocr 可用时 OCR 提取并翻译——默认附录页方案（译文页插在扫描页后，零排版风险）；实验性 `ocr.mode: inplace` 白块覆盖+译文原位回灌（与页内插图重叠的块自动跳过保留原样）；未安装则警告并保留原样
- **pymupdf-layout 适配层**：`performance.layout_engine: pymupdf-layout` 启用 GNN 版面检测（图/表/公式结构化区域替代 bbox 启发式），未安装包自动回退内置启发式并告警
- **图形界面**：Liquid Glass 风格本地 Web UI——路径选择/拖放、设置面板（模型/翻译/性能/高级四类）、实时进度条（阶段+页/批粒度）、批间暂停/恢复/取消、连通性测试（与翻译共用超时/重试参数）、服务端目录浏览、任务历史面板（重跑/打开输出/看警告）、完成后浏览器内联预览输出 PDF
- **多语言翻译**：源/目标语言 15 种——中文、英语、日语、韩语、德语、法语、西班牙语、意大利语、葡萄牙语、俄语、土耳其语、越南语、阿拉伯语（RTL）、希伯来语（RTL）、印地语（天城文）；输出排版按目标语言自动选字体（中文宋体/黑体、日韩相应字体、西文 Times 系、阿拉伯语 Arial/Noto Naskh），输出文件名带语言标记（`-Zh`/`-Ja`/`-De`…）
- **跨平台字体自动探测**：Windows 原生 / WSL / Linux / macOS 全支持；字形覆盖率自动校验，缺字形语言会预警
- **版面保留**：redaction 只删文字层，图片、矢量图形、双栏结构原样保留
- **display 公式裁图回贴**：数学字体主导的独立公式区先裁成 300dpi 位图，翻译后原位浮回；公式编号 `(n)` 自动吸收进位图
- **三线表检测与翻译**：横线聚类启发式兜底（不依赖 PDF 表格标记），单元格逐格翻译原位回灌，数字列自动对齐
- **Algorithm 伪代码框保护**：伪代码框整框保留原文像素（中英混杂会破坏算法语义）
- **跨页断句合并**：页尾开放句 + 下页首段合并为一个翻译单元，译文按长度比例在词/标点边界拆回两页（拆点避开行首标点）；跨页连字符断词（`instrumen-tation`）自动拼回整词送译
- **双栏重切**：跨栏块自动按中线拆分，左栏译文不侵入右栏
- **批量翻译**：全文档段落统一排队，batch JSON 协议，摊薄 LLM 调用次数
- **SQLite 缓存**：key = MD5(engine|model|lang|text)，二次运行零 LLM 调用；容量上限可配（`performance.cache_max_entries`，超出淘汰最旧）；结束输出按文档维度的节省报表（命中段数/折算节省批次数/缓存总量，UI 完成行与历史面板可见）
- **失败降级**：LLM 失败/触顶段落保留原文重灌，版面完整不塌陷；表格单元格译文缺失时同样回灌原文
- **多 provider 支持**：OpenAI 兼容协议，内置 DeepSeek/智谱/Gemini/SiliconFlow/Ollama/LM Studio preset
- **双语对照模式**（可选开关）：译文在上、灰色原文在下同框对照，标题/公式区不重复排版；输出文件名带 `-bilingual` 后缀
- **术语表锁定**：外部 YAML 术语表注入翻译提示词 + 译后逐段校验，专业术语全篇一致；仓库附物理/量子材料示例表 `glossary-physics-example.yaml`
- **版面元素识别**：页眉/页脚自动剔除（顶部 8% / 底部 6% 阈值判定）；Fig./Table 开头的图注表注智能豁免误保护
- **零成本试跑**：CLI `--dry-run` 跳过 LLM 翻译跑完整布局/水印/渲染管线，验证配置是否正确不花一个 token

## 环境要求

- Python 3.11+
- 依赖：`pymupdf`、`openai`、`pyyaml`（UI 另需 `fastapi`、`uvicorn`）
- 字体：按目标语言自动探测（Windows 自带宋体/黑体/times 即可；Linux 需 Noto CJK / Noto Serif / DejaVu 任一；macOS 用系统字体）；找不到时可在 config `fonts:` 段显式指定
- 一个 LLM API key（任选一家，见下文 provider 配置）

## 安装

```bash
# 推荐用 uv 或 venv
uv venv .venv && source .venv/bin/activate    # Linux/macOS
uv pip install pymupdf openai pyyaml pytest

# Windows
uv venv .venv && .venv\Scripts\activate
uv pip install pymupdf openai pyyaml pytest
```

## 快速开始

### 方式 A：图形界面（推荐）

```bash
.venv/bin/python run_ui.py        # Linux/macOS/WSL，默认 http://127.0.0.1:8618
.venv/Scripts/python run_ui.py    # Windows 原生
```

浏览器里：填输入/输出路径（或拖 PDF 进窗口）→ 左下角 ⚙️ 设置 provider/key → 点「翻译」。
设置持久化在 `ui_config.yaml`（仅本地）。

### 方式 B：命令行

1. 复制示例配置并填写：

```bash
cp config.example.yaml myconfig.yaml
```

2. 编辑 `myconfig.yaml`：

```yaml
io:
  input: "paper.pdf"           # 待翻译 PDF 路径
  output_dir: "out"            # 输出目录（生成 <文件名>-<语言>.pdf）
  source_lang: en              # 源语言（版面启发式针对英文文献优化）
  target_lang: zh              # 目标语言，见下方语言表
llm:
  provider: deepseek           # 见下方 Provider 配置
  api_key: ""                  # 留空则从环境变量读取
  model: "deepseek-v4-flash"
features:
  translation_cache: true      # 二次运行零调用
fonts:
  cjk: ""                      # 留空自动探测；也可用 fonts.body/fonts.heading
```

3. 设置 API key（环境变量优先于配置文件）：

```bash
export DEEPSEEK_API_KEY="sk-..."    # provider 对应的环境变量名见下表
python -m translator.cli -c myconfig.yaml
```

4. 运行结束后在 `output_dir` 里拿 `<文件名>-<目标语言>.pdf`
   （如 `paper-Zh.pdf` / `paper-De.pdf`；双语模式 `-bilingual-<语言>.pdf`）。

## 支持语言

| code | 语言 | 输出排版字体（自动探测） |
|---|---|---|
| `zh` | 简体中文 | 宋体 / Noto Serif SC（标题黑体 / Noto Sans SC Bold） |
| `en` `de` `fr` `es` `it` `pt` `tr` `vi` | 西文 | Times New Roman / Noto Serif / DejaVu（标题粗体） |
| `ru` | 俄语 | 同上（Times/DejaVu 含西里尔） |
| `ja` | 日语 | MS Gothic / Yu Gothic / Noto Sans JP |
| `ko` | 韩语 | Malgun / Nanum / Noto Sans KR |
| `ar` | 阿拉伯语（RTL） | Arial / Noto Naskh Arabic / Amiri（自动 direction:rtl） |
| `he` | 希伯来语（RTL） | Arial / Noto Sans Hebrew（自动 direction:rtl） |
| `hi` | 印地语（天城文） | Nirmala UI / Mangal / Noto Sans Devanagari |

- 源语言同样从上表选择（默认 `en`）。版面分析启发式（图注/参考文献/标题判定）针对英文文献优化，其他源语言可运行但识别精度略降
- 每种语言附带字形覆盖率校验：选到的字体缺目标语言字符（豆腐块风险）时输出 warning 并提示在 `fonts:` 段指定字体
- RTL/天城文依赖 htmlbox 渲染引擎的整形能力：目标语言为 `ar`/`he`/`hi` 时即使配置了 `writer` 也会自动切到 htmlbox 并给出告警

## Provider 配置

| provider | base_url | 环境变量 | 推荐模型 |
|---|---|---|---|
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPU_API_KEY` | `glm-4.7-flash`（免费）/ `glm-4-plus` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | `gemini-2.5-flash` |
| `siliconflow` | `https://api.siliconflow.cn/v1` | `SILICONFLOW_API_KEY` | 各家开源模型 |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `ollama` | `http://localhost:11434/v1` | （无） | 本地模型 |
| `lmstudio` | `http://localhost:1234/v1` | （无） | 本地模型 |

key 解析优先级：**config 的 `api_key` > provider 专属环境变量 > `OPENCODE_API_KEY`（通用兜底）**。
多 provider 混用时建议全部走环境变量，避免把 A 家 key 发给 B 家。

### 限流/退避参数（换模型必看）

不同 provider 的速率限制差异很大，以下参数全部可在 config 的 `llm:` 段调整：

```yaml
llm:
  batch_size: 6            # 每批段落数上限。限流严的模型调小到 2-3
  batch_char_budget: 3000  # 每批字符预算：长短段不混批，降低长批超时失败率。
                           # 0=仅按段数
  rpm_limit: 0             # provider 每分钟请求配额。设置后自动换算
                           # min_call_interval=60/rpm；显式写出的键优先
  tpm_limit: 0             # 每分钟 token 配额。与 rpm 同设时自动换算
                           # batch_char_budget=(tpm/rpm)×3.2×0.8（clamp 400-12000）
  max_llm_calls: 40        # 单文档调用上限（防跑飞）
  min_call_interval: 2     # 相邻调用最小间隔秒。限流严的调大到 5-10
  max_workers: 3           # 并发线程数。免费档建议 1
  timeout: 120.0           # 单次请求超时秒（慢模型/长思考模型调大）
  max_retries: 2           # 单批最大尝试次数（1 = 失败不重试直接降级）
  backoff_base: 8.0        # 传输层失败退避基数秒（指数递增）
  backoff_cap: 30.0        # 退避上限秒
  retry_delay_cap: 60.0    # 服务端 RetryInfo 建议等待的封顶秒
  fallback_model: ""       # 逗号分隔备用链 "m2, m3"，主模型日配额耗尽自动切换

performance:               # 本地性能
  layout_workers: 0        # 版面分析进程并行数；0=自动 min(4,CPU)，1=串行
  cache_max_entries: 50000 # 翻译缓存条目上限（0=不限制），超出淘汰最旧
  layout_engine: heuristic # 版面引擎：heuristic=内置启发式（默认）；
                           # pymupdf-layout=GNN 版面检测（需 pip install
                           # pymupdf-layout，未装自动回退启发式并告警）
```

### 免费档使用备注（实测 2026-08）

- **智谱 GLM-4.7-flash（免费）**：能连通、翻译质量尚可，但免费档速率限制极紧（错误码 `1302 账户已达到速率限制`）。实测 8 页论文 39 次调用中 18 批被拒，即使 `min_call_interval: 2 / max_workers: 1` 也大量失败。若坚持使用：
  ```yaml
  llm:
    model: "glm-4.7-flash"
    batch_size: 3          # 小批
    max_workers: 1         # 关并发
    min_call_interval: 6   # 拉大间隔
    timeout: 180           # 该模型带思维链，响应慢
  ```
  代价是单文档耗时 10 分钟以上且仍可能部分降级。**日常使用推荐 deepseek-v4-flash**。
- **Gemini API（免费 tier）**：`gemini-2.5-flash` 等模型的日配额按模型独立计数，耗尽的报错含 `PerDay quotaId`，此时可用 `fallback_model: "gemini-2.5-flash-lite"` 自动切换。但**预付费余额归零**时报 `RESOURCE_EXHAUSTED: Your prepayment credits are depleted`——这是账户级的，所有模型一起不可用，换模型无效，只能充值或换 provider。
- **本地模型**（Ollama/LM Studio）：零成本无限流，`base_url` 指向本地端口即可；注意上下文窗口 ≥8k、指令遵循能力会影响 batch JSON 协议的成功率，失败段落自动降级保留原文。

## 命令行

```bash
python -m translator.cli -c myconfig.yaml [-v] [--dry-run]
```

`--dry-run`：跳过 LLM 翻译，完整跑布局/水印/渲染管线（全部段落保留原文）。
用来验证配置文件是否正确、预览版面识别效果，零 token 消耗。

日志输出每页布局统计（段落数/公式数/单元格数），结束打印总调用数与 warning 列表。
warning 类型：

- `batch failed after retry, keep source: [...]` — 这些批次翻译失败已降级为原文，可重跑（有缓存，成功的批次不会重复调用）
- `cellN: narrow box ... < text width` — 表格窄格文字略超格宽，仅提示不影响内容
- `glossary violation` — 术语表校验违例（启用 `glossary_lock` 时）
- `scanned pages ... not installed (pip install paddleocr)` — 检出扫描页但未装 OCR 引擎，该页保留原样
- `OCR page N: no text recognized` — OCR 未识别出文字，该页保留原样

## 术语表

专业文献术语一致性靠外部 YAML 术语表（可选）。格式为平面映射：

```yaml
# glossary.yaml
Chern number: 陈数
Berry curvature: 贝里曲率
quantum anomalous Hall effect: 量子反常霍尔效应
Weyl semimetal: 外尔半金属
```

工作方式（两道工序）：
1. **翻译前**：术语表全文注入 system prompt，LLM 按表翻译
2. **翻译后**：逐段校验英文原词是否残留，违例记 `glossary violation` 警告

启用：配置里填 `glossary_file: "glossary.yaml"`，或 UI 设置 → 翻译 → 术语表路径。
仓库附带的 **`glossary-physics-example.yaml`** 是物理/量子材料领域示例
（量子霍尔效应族、拓扑材料、贝里几何等约 120 条），可直接用也可当模板改。

## 测试

```bash
python -m pytest tests/ -q
```

157 个单测覆盖核心逻辑：批字符预算组批、缓存容量淘汰与命中统计、单元格原文回灌与 htmlbox 单元格回灌、扫描页 OCR（附录/原位）、布局并行与串行一致性、任务队列/history 归档/重启恢复/历史字段迁移、SSE 端点帧契约与 Last-Event-ID 重放、配额自适应换算、htmlbox/ writer 双渲染路径与 RTL 样张、排版自适配（测量基座/样式级因子/降级阶梯/扩框零重叠/微升/丢段兜底）、源头控长（预算规则/单段重问/预算档缓存）、small caps 节标题与整块粗体摘要的样式回归、OCR 行分组/图形避让/原位回贴、pymupdf-layout 适配层（1.28.x 五元组 + 未装回退）、provider 参数透传、跨页断句拆分（含边界标点与连字符合并）、公式编号剥离、Algorithm 框判定、三线表检测、页脚阈值、渲染回灌、多语言注册表/跨平台字体解析/Unicode 断行、服务端目录浏览与输出预览、key 回填等。
测试不需要网络和 API key。

## 项目结构

```
translator/
  cli.py         # 命令行入口
  pipeline.py    # 主流程编排（布局→裁图→排队→翻译→回灌；并行布局/OCR、配额自适应、OCR 原位回贴、双渲染引擎、版面缓存断点续跑、缓存节省报表）
  render.py      # 渲染回灌（redaction/重排/公式回贴；htmlbox 默认 + writer 遗留双引擎，RTL direction 支持）
  fit.py        # 排版自适配：测量基座（Story 同源）+ 样式级全局因子 + 降级阶梯 + 字符预算
  extract.py     # 文本块提取（扫描页检测 page_has_text_layer）
  layout.py      # 版面分析（双栏/公式区/三线表/图注/页眉脚/Algorithm框；pymupdf-layout 外部引擎适配层）
  refsplit.py    # 参考文献条目重切
  llm.py         # LLM 客户端（批字符预算组批/重试退避/fallback链/限流/配额自适应/缓存命中统计）
  langs.py       # 多语言注册表（15 种含 RTL/天城文）+ 跨平台字体解析
  cache.py       # SQLite 翻译缓存（容量上限淘汰）
  glossary.py    # 术语表锁定
  ocr.py         # 扫描页惰性 OCR（paddleocr 可选依赖；行级 bbox 提取供原位回贴）
  typography.py  # 期刊级排版（按目标语言选字体族）
  wrap_mixed.py  # CJK/拉丁/西里尔混排断行
  preprocess.py  # 水印移除
  control.py     # 暂停/恢复/取消（批间协作式检查点）
  events.py      # 进度事件流
server/
  app.py         # FastAPI：静态 UI + REST/SSE API（翻译提交/排队/进度轮询+SSE推送+Last-Event-ID续传/配置读写/目录浏览/key 连通性测试（共用超时重试）/输出预览/任务历史；key 回填/队列任务取消）
  jobs.py        # 任务管理器（子进程隔离，JSONL 事件流 + 控制文件轮询；忙时入队接力/事件广播（带 id 日志）/警告接活/崩溃归档/持久化恢复）
  store.py       # 任务持久化（SQLite：队列/历史/重启恢复；v0.5.1 增量列迁移）
  glossary_io.py # 术语表批量导入（合并/校验/行号级报错）
web/
  index.html     # Liquid Glass 单文件前端（含任务历史面板）
run_ui.py        # 一键启动（uvicorn + 自动开浏览器）
tests/           # 单元测试
config.example.yaml  # 配置模板
glossary-physics-example.yaml  # 物理/量子材料术语表示例
```

## 已知限制

- 扫描件翻译默认**附录页方案**：扫描页 OCR 文字翻译后插入附加译文页（`[OCR · p.N]` 标头），原扫描页不动。`ocr.mode: inplace`（实验性）改为白块覆盖+原位回灌，与页内插图重叠的块自动跳过。需要另装 `paddleocr`（`pip install paddleocr`），未安装时该页保留原样并给出警告；OCR 识别质量决定译文质量
- `writer` 渲染引擎为遗留路径：逐字排印不支持 RTL/复杂整形（阿拉伯语等目标会自动切到默认的 htmlbox）；个别 PDF 的字体子集嵌入可能缺字，遇到异常切换 renderer 即可（两种引擎可按段对比）
- 竖排文本、旋转页面不支持翻译（原样保留）
- 极复杂嵌套表格可能切分不准，单元格译文以警告提示
- 水印移除仅处理**文字层水印**（出版社/preprint 声明类）；扫描件中烤在图像像素里的水印无法移除
- UI 暂停为**批间暂停**：正在飞行中的那次 LLM 请求会跑完才停（几秒内生效）；取消同理，且不产出半成品 PDF；并行布局阶段的取消在页粒度生效
- 版面缓存按输入文件（路径+大小+mtime+引擎）失效：改代码升级启发式后旧缓存自动作废（缓存 key 含版本号），但同版本内反复微调输出不影响正确性

## 版本历史

各版本变更见 GitHub Releases：<https://github.com/ShZbz/pdf-translator/releases>