"""FastAPI 服务：静态 UI + REST/SSE API（v0.4.0 起）。

端点：
- GET  /                     → web/index.html
- POST /api/translate        → 提交翻译任务 {input, output_dir, llm?, features?}
                                （忙时入队，v0.4.3；队列满才 409）
- GET  /api/jobs/current     → 当前任务状态+事件快照
- GET  /api/jobs/current/stream → SSE 实时流（v0.5.0：事件推送替代轮询，
                                前端不支持 EventSource 时自动退轮询）
- GET  /api/jobs             → 当前任务 + 排队任务 + 历史归档（v0.4.3；
                                v0.5.0 历史落 SQLite，重启可查）
- POST /api/jobs/{id}/pause|resume|cancel
                                （v0.5.0: 排队中的任务也可取消）
- GET  /api/config           → 读 UI 配置（key 打码）
- PUT  /api/config           → 写 UI 配置
- POST /api/validate-key     → 测试 provider 连通性
- POST /api/glossary/save    → 校验并保存合并文本，路径供配置引用
- GET  /api/glossary/status  → 术语表加载状态（存在性+校验+条目数，v0.5.2）
- GET  /api/browse?path=     → 服务端目录浏览（浏览器拿不到真实路径）
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .glossary_io import collect_yaml_files, dump_merged, validate_and_merge, validate_text
from .jobs import JobManager
from .store import JobStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UI_CONFIG_PATH = PROJECT_ROOT / "ui_config.yaml"
# v0.5.0: 任务持久化库路径可用 PDF_TRANSLATOR_JOBS_DB 覆盖（测试隔离用）
JOBS_DB_PATH = Path(os.environ.get("PDF_TRANSLATOR_JOBS_DB")
                    or PROJECT_ROOT / ".ui_jobs.db")

app = FastAPI(title="pdf-translator UI")
# v0.5.0: 队列/历史持久化（.ui_jobs.db）——服务重启恢复未完成任务
manager = JobManager(PROJECT_ROOT, store=JobStore(JOBS_DB_PATH))


# ---------- 静态页 ----------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# ---------- 翻译任务 ----------
class TranslateReq(BaseModel):
    input: str
    output_dir: str = ""
    config: dict = Field(default_factory=dict)   # 完整 ui_config.yaml 内容


@app.post("/api/translate")
def translate(req: TranslateReq) -> dict:
    src = Path(req.input).expanduser()
    if not src.is_file():
        raise HTTPException(400, f"输入文件不存在: {src}")
    if src.suffix.lower() != ".pdf":
        raise HTTPException(400, "只支持 PDF")

    cfg_data = _build_run_config(req, src)
    run_cfg = PROJECT_ROOT / f".ui_run_config_{uuid.uuid4().hex[:8]}.yaml"
    run_cfg.write_text(yaml.safe_dump(cfg_data, allow_unicode=True),
                       encoding="utf-8")

    out_dir = cfg_data["io"]["output_dir"]
    result = manager.submit(str(run_cfg), input_path=str(src),
                            output_dir=out_dir)
    if not result.get("ok"):
        # v0.5.1 修复：队列满 409 时任务未创建，临时运行配置没人清理
        # （Job._cleanup_files 只在任务终态时触发）——这里兜底删除
        try:
            run_cfg.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(409, result.get("error", "submit failed"))
    return result


def _build_run_config(req: "TranslateReq", src: Path) -> dict:
    """由 UI 配置合成运行配置（v0.4.3: 文件名带任务 id 防忙时覆盖）。

    v0.5.0 修复（B1）: UI 重载后 key 输入框为空、collectConfig 提交的是
    空 api_key——旧版 run config 只带空 key，worker 的 resolve() 只查
    环境变量不查 ui_config.yaml 存量 → 已保存过 key 的用户照样 401。
    这里从存储配置回填（key 只在本机文件间流转，不出本机）。
    """
    # 由 UI 配置合成临时运行配置
    cfg_data = req.config or {}
    cfg_data.setdefault("io", {})
    cfg_data["io"]["input"] = str(src)
    out_dir = req.output_dir or cfg_data["io"].get("output_dir") or str(src.parent)
    cfg_data["io"]["output_dir"] = out_dir
    llm_data = cfg_data.get("llm") or {}
    if not (llm_data.get("api_key") or "").strip() \
            or "***" in llm_data.get("api_key", ""):
        stored = _stored_llm()
        if stored.get("api_key"):
            llm_data["api_key"] = stored["api_key"]
    return cfg_data


def _stored_llm() -> dict:
    """读取 ui_config.yaml 的 llm 段（不存在返回空 dict）。"""
    if not UI_CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(
            UI_CONFIG_PATH.read_text(encoding="utf-8")).get("llm", {}) or {}
    except Exception:
        return {}


@app.get("/api/jobs/current")
def current_job() -> dict:
    job = manager.current()
    if not job:
        return {"status": "idle", "queue_len": len(manager.queue)}
    snap = job.snapshot()
    snap["queue_len"] = len(manager.queue)
    return snap


@app.get("/api/jobs")
def all_jobs() -> dict:
    """v0.4.3: 当前任务 + 排队任务 + 历史归档（v0.5.0: 历史持久化重启可查）。"""
    snap = manager.current_snapshot()
    return {
        "current": snap["current"],
        "queued": snap["queued"],
        "history": manager.history[-20:],
    }


class JobAction(BaseModel):
    action: str   # pause/resume/cancel


@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str) -> dict:
    # v0.4.3: 动作白名单——旧版 getattr(job, action) 任意方法可调
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(400, f"未知操作: {action}")
    # v0.5.0 修复（B2）: 旧版只查当前任务，排队中的任务永远 404——
    # 用户提交错了只能干等它排到队首。排队任务现在可取消/暂停意图登记
    job = manager.act(job_id, action)
    if job is None:
        raise HTTPException(404, "任务不存在或已结束")
    return job.snapshot()


# ---------- SSE 实时流（v0.5.0: 任务 2-5；v0.5.1: Last-Event-ID 续传）----------
@app.get("/api/jobs/current/stream")
async def job_stream(last_event_id: str | None = Header(
        default=None, alias="Last-Event-ID")):
    """当前任务/队列的服务端推送事件流。

    帧格式 `id: <seq>\\ndata: {json}\\n\\n`：
    - {"kind":"state", ...}   全量状态（连接建立首发 + 队列推进时）
    - {"kind":"job_event", "event":..., "job":...}  任务事件（进度/阶段/警告）
    每 15s 发一行 `: ping` 心跳防中间层断连。事件泵线程 → SimpleQueue →
    本协程 0.25s 间隔取帧（无第三方依赖的线程→异步io 桥）。

    v0.5.1: 浏览器 EventSource 断线自动重连时携带 Last-Event-ID 请求头，
    本端从有界事件日志（JobManager._event_log，1000 条环形）补发错过
    的帧再接续实时流——网络闪断/标签页休眠不再丢进度与警告。
    """
    q = manager.subscribe()
    replay: list[tuple[int, dict]] = []
    if last_event_id and isinstance(last_event_id, str):
        try:
            replay = manager.events_after(int(last_event_id))
        except ValueError:
            replay = []

    async def gen():
        try:
            if not replay:
                snap = manager.current_snapshot()
                yield (f"id: {manager._event_seq}\n"
                       f"data: {json.dumps({'kind': 'state', **snap}, ensure_ascii=False)}\n\n")
            for eid, payload in replay:
                yield (f"id: {eid}\n"
                       f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
            if replay:
                # 重放后补一帧全量状态对齐（客户端可能错过 state 类帧）
                s = manager.current_snapshot()
                yield (f"id: {manager._event_seq}\n"
                       f"data: {json.dumps({'kind': 'state', **s}, ensure_ascii=False)}\n\n")
            last_beat = time.monotonic()
            while True:
                try:
                    payload = q.get_nowait()
                except Exception:
                    payload = None
                if payload is not None:
                    if payload.get("kind") == "manager":
                        # 队列推进/新提交 → 补发全量状态
                        s = manager.current_snapshot()
                        yield (f"id: {payload.get('eid', manager._event_seq)}\n"
                               f"data: {json.dumps({'kind': 'state', **s}, ensure_ascii=False)}\n\n")
                    else:
                        yield (f"id: {payload.get('eid', 0)}\n"
                               f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
                    continue
                now = time.monotonic()
                if now - last_beat >= 15.0:
                    yield ": ping\n\n"
                    last_beat = now
                await asyncio.sleep(0.25)
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------- 配置读写 ----------
def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "***" + key[-4:] if len(key) > 12 else "***"


@app.get("/api/config")
def get_config() -> dict:
    raw = {}
    if UI_CONFIG_PATH.exists():
        raw = yaml.safe_load(UI_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    llm = raw.get("llm", {}) or {}
    masked = {**llm}
    if masked.get("api_key"):
        masked["api_key_masked"] = _mask(masked["api_key"])
        masked["has_key"] = True
        del masked["api_key"]
    else:
        masked["has_key"] = False
    # v0.7.1: 首启向导触发条件——ui_config.yaml 不存在即向导模式
    from translator import __version__ as app_version
    return {"config": {**raw, "llm": masked},
            "first_run": not UI_CONFIG_PATH.exists(),
            "version": app_version}


@app.get("/api/presets")
def presets() -> dict:
    """v0.7.1: provider 预设 + 档位调优参数（首启向导下拉/预填数据源）。

    单一来源 translator.config：PRESETS（base_url/env）、PROVIDER_TUNING
    （按档位自动写入的整组推荐参数）、RECOMMENDED_MODELS（推荐模型预填）。
    """
    from translator.config import (PRESETS, PROVIDER_TUNING,
                                   RECOMMENDED_MODELS)
    return {"providers": PRESETS, "tuning": PROVIDER_TUNING,
            "recommended_model": RECOMMENDED_MODELS}


class ConfigReq(BaseModel):
    config: dict


@app.put("/api/config")
def put_config(req: ConfigReq) -> dict:
    cfg = req.config
    # 前端传回打码 key 时保留旧值
    old = {}
    if UI_CONFIG_PATH.exists():
        old = yaml.safe_load(UI_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    new_llm = cfg.get("llm", {})
    old_llm = old.get("llm", {})
    # 打码 key 检测用子串匹配（_mask 长键格式 sk-xxx***xxxx 并不以 ***
    # 结尾——endswith 判不出，会把打码串当真 key 存进去毁掉旧值）
    if "***" in new_llm.get("api_key", "") or (
            not new_llm.get("api_key") and old_llm.get("api_key")):
        new_llm["api_key"] = old_llm.get("api_key", "")
    UI_CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return {"ok": True}


# ---------- 连通性测试 ----------
class ValidateReq(BaseModel):
    provider: str
    api_key: str = ""
    base_url: str = ""
    model: str
    target_lang: str = "zh"
    # v0.5.1: 与翻译任务共用超时/重试参数（旧版写死 timeout=15、单次不重试
    # ——UI 配的「请求超时/重试次数」对连通性测试不生效，慢模型误报失败）
    timeout: float = 0.0
    max_retries: int = 0


def _stored_llm_timeout_retry() -> tuple[float, int]:
    """存储配置里的超时/重试（缺省 120s / 2 次，与 LLMConfig 默认一致）。"""
    llm = _stored_llm()
    try:
        timeout = float(llm.get("timeout") or 120.0)
    except (TypeError, ValueError):
        timeout = 120.0
    try:
        retries = max(1, int(llm.get("max_retries") or 2))
    except (TypeError, ValueError):
        retries = 2
    return timeout, retries


@app.post("/api/validate-key")
def validate_key(req: ValidateReq):
    """发一条最小翻译请求验证连通性。key 打码时用存储的旧值。

    v0.5.1: 请求参数 > 存储的 ui_config llm 段 > 默认值，与翻译任务
    行为一致（慢模型带思维链时 15s 必超时，误判 key 无效）。
    """
    from translator.config import PRESETS
    from translator.langs import prompt_lang_name
    p = PRESETS.get(req.provider, {})
    base_url = req.base_url or p.get("base_url", "")
    api_key = req.api_key
    # 打码 key 检测用子串匹配（_mask 长键格式 sk-xxx***xxxx 不以 *** 结尾，
    # endswith 判不出会把打码串当真 key 发给网关）
    if "***" in api_key:
        api_key = ""
    if not api_key:
        env_name = p.get("env") or ""
        if env_name:
            api_key = os.environ.get(env_name, "")
    if not api_key:
        # v0.8.1: 空/仅注释的 ui_config.yaml 会让 yaml.safe_load 返回
        # None——旧版直接 .get() 抛 AttributeError → 500。_stored_llm
        # 已做容错，这里复用（key 回填语义不变）
        api_key = _stored_llm().get("api_key", "")

    st_timeout, st_retries = _stored_llm_timeout_retry()
    timeout = max(5.0, float(req.timeout or 0) or st_timeout)
    max_retries = max(1, int(req.max_retries or 0) or st_retries)

    # v0.8.3: LLMClientPool 统一构造——SDK max_retries=0（重试单层化：
    # 本端点的 attempt 循环是唯一重试层），用毕关池
    from translator.llm import LLMClientPool
    pool = LLMClientPool(base_url, api_key or "sk-noop", timeout)
    t0 = time.time()
    last_err = ""
    try:
        for attempt in range(1, max_retries + 1):
            try:
                # v0.8.3: 走池墙钟——头部阶段（_receive_response_headers）
                # 楔死时 httpx read timeout 不触发，裸调会让端点无限挂起
                resp = pool.call(
                    lambda: pool.client.chat.completions.create(
                        model=req.model,
                        messages=[
                            {"role": "system",
                             "content": f"Translate to {prompt_lang_name(req.target_lang)}. "
                                        f"Output only the translation."},
                            {"role": "user", "content": "Hello."},
                        ],
                        max_tokens=50,
                    ),
                    "validate-key")
                sample = resp.choices[0].message.content or ""
                # v0.7.1 修复：latency 键此前缺引号（裸名）→ NameError 被
                # except 吞掉，API 成功也回 ok=False——连通性测试恒失败
                return {"ok": True, "latency": round(time.time() - t0, 1),
                        "sample": sample[:60]}
            except Exception as e:
                last_err = str(e)[:300]
                if attempt < max_retries:
                    time.sleep(min(2.0 * attempt, 5.0))
    finally:
        pool.close()
    return JSONResponse(status_code=200,
                        content={"ok": False, "error": last_err})


# ---------- 术语表批量导入 ----------
class GlossaryCollectReq(BaseModel):
    path: str
    recursive: bool = True
    depth: int = 2


class GlossaryPreviewReq(BaseModel):
    paths: list[str]


class GlossarySaveReq(BaseModel):
    text: str


GLOSSARY_MERGED_PATH = PROJECT_ROOT / ".ui_glossary_merged.yaml"


@app.post("/api/glossary/collect")
def glossary_collect(req: GlossaryCollectReq) -> dict:
    """目录（递归 depth 层）或单个文件 → 候选 YAML 清单。"""
    files = collect_yaml_files(req.path, recursive=req.recursive,
                               depth=req.depth)
    if not files:
        raise HTTPException(404, "该位置没有找到 .yaml/.yml/.json 文件")
    return {"files": files}


@app.post("/api/glossary/preview")
def glossary_preview(req: GlossaryPreviewReq) -> dict:
    """合并+校验，返回 (ok, merged_text, issues)。error 存在时 ok=false。"""
    merged, issues = validate_and_merge(req.paths)
    errors = [i for i in issues if i["kind"] == "error"]
    return {"ok": not errors, "merged_text": dump_merged(merged),
            "issues": issues, "term_count": len(merged)}


@app.post("/api/glossary/save")
def glossary_save(req: GlossarySaveReq):
    """保存合并文本到本地临时术语表，路径供配置引用。

    若仍有 error（用户手改大框引入的）则拒绝保存并回传 issues。
    """
    merged, issues = validate_text(req.text)
    errors = [i for i in issues if i["kind"] == "error"]
    if merged is None or errors:
        return JSONResponse(status_code=422,
                            content={"ok": False, "issues": errors or issues})
    GLOSSARY_MERGED_PATH.write_text(
        dump_merged(merged), encoding="utf-8")
    return {"ok": True, "path": str(GLOSSARY_MERGED_PATH),
            "term_count": len(merged)}


@app.get("/api/glossary/status")
def glossary_status(path: str = "") -> dict:
    """v0.5.2: 术语表文件加载状态，供 UI 行内显示「已加载 · N 条术语」。

    只做本地校验不发请求；路径为空/文件缺失时 exists=false（不报错，
    属于「未设置」而非异常）。条目数与 /api/glossary/preview 同源
    （validate_text），含 warning 不影响 ok。
    """
    raw = path.strip()
    p = Path(raw).expanduser() if raw else None
    if p is None or not p.is_file():
        return {"exists": False, "ok": False, "term_count": 0}
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"exists": True, "ok": False, "term_count": 0}
    merged, issues = validate_text(text)
    errors = [i for i in issues if i["kind"] == "error"]
    return {"exists": True, "ok": merged is not None and not errors,
            "term_count": len(merged) if merged is not None else 0}


# ---------- 目录浏览 ----------
@app.get("/api/browse")
def browse(path: str = "") -> dict:
    """列出目录内容（只读）。空路径从常用根开始。"""
    if not path:
        # 常用起点：按存在性挑第一个（v0.4.2 修复：旧版 next(iter(...))
        # 不检查存在性，原生 Windows 上拿到 /mnt/c/Users 直接 404，
        # 文件浏览器打不开）
        candidates = ["/mnt/c/Users", str(Path.home())]
        candidates += [f"/mnt/{c}" for c in "defghijkl"
                       if Path(f"/mnt/{c}").exists()]
        path = next((c for c in candidates if Path(c).is_dir()),
                    str(Path.home()))
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise HTTPException(404, f"路径不存在: {p}")
    if not p.is_dir():
        p = p.parent
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
                entries.append({"name": child.name, "dir": is_dir})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "无权限读取该目录")
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else "",
            "entries": entries[:400]}


# ---------- 输出文件预览 ----------
@app.get("/api/output")
def get_output(path: str, download: int = 0) -> FileResponse:
    """输出 PDF 预览/下载。

    v0.4.2：旧版完成链接用 file:// 协议，Chrome/Edge 从 http 页面点击
    会被「Not allowed to load local resource」拦截——改为服务端转发，
    浏览器内联预览或下载均可用。仅放行 .pdf（本地单用户工具，防误取）。
    """
    p = Path(path).expanduser()
    if p.suffix.lower() != ".pdf" or not p.is_file():
        raise HTTPException(404, f"输出文件不存在: {p}")
    if download:
        return FileResponse(p, media_type="application/pdf",
                            filename=p.name)
    return FileResponse(p, media_type="application/pdf")
