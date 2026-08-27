"""FastAPI 服务：静态 UI + REST API（v0.4.0）。

端点：
- GET  /                     → web/index.html
- POST /api/translate        → 提交翻译任务 {input, output_dir, llm?, features?}
- GET  /api/jobs/current     → 当前任务状态+事件快照
- POST /api/jobs/{id}/pause|resume|cancel
- GET  /api/config           → 读 UI 配置（key 打码）
- PUT  /api/config           → 写 UI 配置
- POST /api/validate-key     → 测试 provider 连通性
- GET  /api/browse?path=     → 服务端目录浏览（浏览器拿不到真实路径）
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .glossary_io import collect_yaml_files, dump_merged, validate_and_merge, validate_text
from .jobs import JobManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UI_CONFIG_PATH = PROJECT_ROOT / "ui_config.yaml"

app = FastAPI(title="pdf-translator UI")
manager = JobManager(PROJECT_ROOT)


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

    # 由 UI 配置合成临时运行配置
    cfg_data = req.config or {}
    cfg_data.setdefault("io", {})
    cfg_data["io"]["input"] = str(src)
    out_dir = req.output_dir or cfg_data["io"].get("output_dir") or str(src.parent)
    cfg_data["io"]["output_dir"] = out_dir

    run_cfg = PROJECT_ROOT / ".ui_run_config.yaml"
    run_cfg.write_text(yaml.safe_dump(cfg_data, allow_unicode=True),
                       encoding="utf-8")

    result = manager.submit(str(run_cfg))
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "submit failed"))
    return result


@app.get("/api/jobs/current")
def current_job() -> dict:
    job = manager.current()
    if not job:
        return {"status": "idle"}
    return job.snapshot()


class JobAction(BaseModel):
    action: str   # pause/resume/cancel


@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str) -> dict:
    job = manager.current()
    if not job or job.id != job_id:
        raise HTTPException(404, "任务不存在或已结束")
    fn = getattr(job, action, None)
    if not callable(fn):
        raise HTTPException(400, f"未知操作: {action}")
    fn()
    return job.snapshot()


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
    return {"config": {**raw, "llm": masked}}


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
    if new_llm.get("api_key", "").endswith("***") or (
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


@app.post("/api/validate-key")
def validate_key(req: ValidateReq):
    """发一条最小翻译请求验证连通性。key 打码时用存储的旧值。"""
    from translator.config import PRESETS
    from translator.langs import prompt_lang_name
    p = PRESETS.get(req.provider, {})
    base_url = req.base_url or p.get("base_url", "")
    api_key = req.api_key
    if api_key.endswith("***") or (not api_key and p.get("env")):
        env_name = p.get("env") or ""
        api_key = api_key if api_key and not api_key.endswith("***") \
            else os.environ.get(env_name, "") if env_name else ""
        if not api_key and UI_CONFIG_PATH.exists():
            stored = yaml.safe_load(
                UI_CONFIG_PATH.read_text(encoding="utf-8")).get("llm", {})
            api_key = stored.get("api_key", "")

    try:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key or "sk-noop",
                        timeout=15.0)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=req.model,
            messages=[
                {"role": "system",
                 "content": f"Translate to {prompt_lang_name(req.target_lang)}. "
                            f"Output only the translation."},
                {"role": "user", "content": "Hello."},
            ],
            max_tokens=50,
        )
        sample = resp.choices[0].message.content or ""
        return {"ok": True, "latency": round(time.time() - t0, 1),
                "sample": sample[:60]}
    except Exception as e:
        return JSONResponse(status_code=200,
                            content={"ok": False, "error": str(e)[:300]})


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
