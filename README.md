# pdf-translator

学术 PDF 整册翻译工具：保留原始版面（图片/表格/公式/双栏）的 EN→ZH 翻译。

## 功能特性

- **图形界面（v0.4.0）**：Liquid Glass 风格本地 Web UI——路径选择/拖放、设置面板（模型/翻译/性能/高级四类）、实时进度条、批间暂停/恢复/取消、连通性测试、服务端目录浏览
- **版面保留**：redaction 只删文字层，图片、矢量图形、双栏结构原样保留
- **display 公式裁图回贴**：数学字体主导的独立公式区先裁成 300dpi 位图，翻译后原位浮回；公式编号 `(n)` 自动吸收进位图
- **三线表检测与翻译**：横线聚类启发式兜底（不依赖 PDF 表格标记），单元格逐格翻译原位回灌，数字列自动对齐
- **Algorithm 伪代码框保护**：伪代码框整框保留原文像素（中英混杂会破坏算法语义）
- **跨页断句合并**：页尾开放句 + 下页首段合并为一个翻译单元，译文按长度比例拆回两页
- **双栏重切**：跨栏块自动按中线拆分，左栏译文不侵入右栏
- **批量翻译**：全文档段落统一排队，batch JSON 协议，摊薄 LLM 调用次数
- **SQLite 缓存**：key = MD5(engine|model|lang|text)，二次运行零 LLM 调用
- **失败降级**：LLM 失败/触顶段落保留原文重灌，版面完整不塌陷
- **多 provider 支持**：OpenAI 兼容协议，内置 DeepSeek/智谱/Gemini/SiliconFlow/Ollama/LM Studio preset
- **双语对照模式**（可选开关）：译文在上、灰色原文在下同框对照，标题/公式区不重复排版；输出文件名带 `-bilingual` 后缀
- **术语表锁定**：外部 YAML 术语表注入翻译提示词 + 译后逐段校验，专业术语全篇一致；违例段落以 `glossary violation` 警告提示。仓库附物理/拓扑材料示例表 `glossary-physics-example.yaml`
- **版面元素识别**：页眉/页脚自动剔除（顶部 8% / 底部 6% 阈值判定，不译不干扰正文）；Fig./Table 开头的图注表注智能豁免误保护
- **零成本试跑**：CLI `--dry-run` 跳过 LLM 翻译跑完整布局/水印/渲染管线，验证配置是否正确不花一个 token

## 环境要求

- Python 3.11+
- 依赖：`pymupdf`、`openai`、`pyyaml`
- 中文字体：Windows 自带宋体/黑体即可；Linux 需安装 Noto Sans CJK 或思源字体
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
.venv/bin/python run_ui.py        # 起服务并自动开浏览器，默认 http://127.0.0.1:8618
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
  output_dir: "out"            # 输出目录（生成 <文件名>-Zh.pdf）
  source_lang: en
  target_lang: zh
llm:
  provider: deepseek           # 见下方 Provider 配置
  api_key: ""                  # 留空则从环境变量读取
  model: "deepseek-chat"
features:
  translation_cache: true      # 二次运行零调用
fonts:
  cjk: ""                      # 留空自动探测（Windows 找 simsun/simhei）
```

3. 设置 API key（环境变量优先于配置文件）：

```bash
export DEEPSEEK_API_KEY="sk-..."    # provider 对应的环境变量名见下表
python -m translator.cli -c myconfig.yaml
```

4. 运行结束后在 `output_dir` 里拿 `<文件名>-Zh.pdf`。

## Provider 配置

| provider | base_url | 环境变量 | 推荐模型 |
|---|---|---|---|
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
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
  batch_size: 6            # 每次 LLM 调用打包的段落数。限流严的模型调小到 2-3
  max_llm_calls: 40        # 单文档调用上限（防跑飞）
  min_call_interval: 2     # 相邻调用最小间隔秒。限流严的调大到 5-10
  max_workers: 3           # 并发线程数。免费档建议 1
  timeout: 120.0           # 单次请求超时秒（慢模型/长思考模型调大）
  max_retries: 2           # 单批最大尝试次数（1 = 失败不重试直接降级）
  backoff_base: 8.0        # 传输层失败退避基数秒（指数递增）
  backoff_cap: 30.0        # 退避上限秒
  retry_delay_cap: 60.0    # 服务端 RetryInfo 建议等待的封顶秒
  fallback_model: ""       # 逗号分隔备用链 "m2, m3"，主模型日配额耗尽自动切换
```

### 免费档使用备注（实测 2026-08）

- **智谱 GLM-4.7-flash（免费）**：能连通、翻译质量尚可，但免费档速率限制极紧
  （错误码 `1302 账户已达到速率限制`）。实测 8 页论文 39 次调用中 18 批被拒，
  即使 `min_call_interval: 2 / max_workers: 1` 也大量失败。若坚持使用：
  ```yaml
  llm:
    model: "glm-4.7-flash"
    batch_size: 3          # 小批
    max_workers: 1         # 关并发
    min_call_interval: 6   # 拉大间隔
    timeout: 180           # 该模型带思维链，响应慢
  ```
  代价是单文档耗时 10 分钟以上且仍可能部分降级。**日常使用推荐 deepseek-chat**。
- **Gemini API（免费 tier）**：`gemini-2.5-flash` 等模型的日配额按模型独立计数，
  耗尽的报错含 `PerDay quotaId`，此时可用 `fallback_model: "gemini-2.5-flash-lite"`
  自动切换。但**预付费余额归零**时报 `RESOURCE_EXHAUSTED: Your prepayment credits
  are depleted`——这是账户级的，所有模型一起不可用，换模型无效，只能充值或换 provider。
- **本地模型**（Ollama/LM Studio）：零成本无限流，`base_url` 指向本地端口即可；
  注意上下文窗口 ≥8k、指令遵循能力会影响 batch JSON 协议的成功率，失败段落自动降级保留原文。

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

26 个单测覆盖：provider 参数透传、跨页断句拆分、公式编号剥离、Algorithm 框判定、
三线表检测、页脚阈值、渲染回灌等核心逻辑。测试不需要网络和 API key。

## 项目结构

```
translator/
  cli.py         # 命令行入口
  pipeline.py    # 主流程编排（布局→裁图→排队→翻译→回灌）
  extract.py     # 文本块提取（文字层/OCR 降级）
  layout.py      # 版面分析（双栏/公式区/三线表/图注/页眉脚/Algorithm框）
  refsplit.py    # 参考文献条目重切
  llm.py         # LLM 客户端（批量协议/重试退避/fallback链/限流）
  cache.py       # SQLite 翻译缓存
  glossary.py    # 术语表锁定
  render.py      # 渲染回灌（redaction/重排/公式回贴）
  typography.py  # 中文字体排版
  wrap_mixed.py  # CJK/拉丁混排断行
  preprocess.py  # 水印移除
  control.py     # v0.4.0 暂停/恢复/取消（批间协作式检查点）
  events.py      # v0.4.0 进度事件流
server/
  app.py         # FastAPI：静态 UI + REST API（翻译提交/进度轮询/配置读写/目录浏览/key连通性测试）
  jobs.py        # 任务管理器（子进程隔离，JSONL 事件流 + stdin 控制管道）
web/
  index.html     # Liquid Glass 单文件前端
run_ui.py        # 一键启动（uvicorn + 自动开浏览器）
tests/           # 单元测试
config.example.yaml  # 配置模板
glossary-physics-example.yaml  # 物理/量子材料术语表示例
```

## 已知限制

- 手写体/艺术字扫描件依赖 OCR 质量（默认 PaddleOCR，需另装）
- 竖排文本、旋转页面不支持翻译（原样保留）
- 极复杂嵌套表格可能切分不准，单元格译文以警告提示
- 水印移除仅处理**文字层水印**（出版社/preprint 声明类）；扫描件中烤在图像像素里的水印无法移除
- UI 暂停为**批间暂停**：正在飞行中的那次 LLM 请求会跑完才停（几秒内生效）；
  取消同理，且不产出半成品 PDF
