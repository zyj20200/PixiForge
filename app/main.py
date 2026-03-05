"""
PixiForge - 定格动画自动生成器
后端 API (FastAPI)

流程：场景设定 → 分镜设计 → 首帧图片 → 逐帧生成 → 视频输出
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

# ━━━━━━━━━━━━━━━━ 日志配置 ━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pixiforge")
logger.setLevel(logging.INFO)

# ━━━━━━━━━━━━━━━━ 配置 ━━━━━━━━━━━━━━━━

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
PROJECTS_DIR = DATA_DIR / "projects"
OUTPUTS_DIR = DATA_DIR / "outputs"
STATIC_DIR = BASE_DIR / "static"

for d in [DATA_DIR, UPLOADS_DIR, PROJECTS_DIR, OUTPUTS_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://grok2api.zyj20200.workers.dev").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "")
DEFAULT_CHAT_MODEL = os.getenv("AI_CHAT_MODEL", "grok-4.1-thinking")
DEFAULT_IMAGE_MODEL = os.getenv("AI_IMAGE_MODEL", "grok-imagine-1.0")
DEFAULT_IMAGE_EDIT_MODEL = os.getenv("AI_IMAGE_EDIT_MODEL", "grok-imagine-1.0-edit")

logger.info("配置加载完成: AI_BASE_URL=%s, CHAT_MODEL=%s, IMAGE_MODEL=%s, EDIT_MODEL=%s",
            AI_BASE_URL, DEFAULT_CHAT_MODEL, DEFAULT_IMAGE_MODEL, DEFAULT_IMAGE_EDIT_MODEL)
logger.info("AI_API_KEY %s", "已配置" if AI_API_KEY else "未配置（请在 .env 中设置）")

# ━━━━━━━━━━━━━━━━ 内存存储 ━━━━━━━━━━━━━━━━

projects: dict[str, dict[str, Any]] = {}
projects_lock = Lock()

# ━━━━━━━━━━━━━━━━ FastAPI 应用 ━━━━━━━━━━━━━━━━


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载磁盘上已有的项目"""
    logger.info("========== PixiForge 启动 ==========")
    loaded = 0
    if PROJECTS_DIR.exists():
        for pdir in PROJECTS_DIR.iterdir():
            if pdir.is_dir():
                pfile = pdir / "project.json"
                if pfile.exists():
                    try:
                        proj = json.loads(pfile.read_text(encoding="utf-8"))
                        with projects_lock:
                            projects[proj["id"]] = proj
                        loaded += 1
                    except Exception as e:
                        logger.warning("加载项目失败 %s: %s", pdir.name, e)
    logger.info("从磁盘加载了 %d 个历史项目", loaded)
    logger.info("服务就绪，访问 http://0.0.0.0:8000")
    yield
    logger.info("========== PixiForge 关闭 ==========")


app = FastAPI(title="PixiForge", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ━━━━━━━━━━━━━━━━ 请求模型 ━━━━━━━━━━━━━━━━


class CreateProjectRequest(BaseModel):
    scene_description: str = Field(min_length=1)
    character_description: str = ""
    style_description: str = ""
    fps: int = Field(default=4, ge=2, le=24)
    duration_seconds: int = Field(default=3, ge=1, le=30)
    frame_count: int | None = None


class UpdateStoryboardRequest(BaseModel):
    frames: list[dict[str, Any]]


class GenerateFirstFrameRequest(BaseModel):
    prompt: str = Field(min_length=1)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = DEFAULT_CHAT_MODEL


class ImageGenRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = DEFAULT_IMAGE_MODEL
    n: int = Field(default=1, ge=1, le=4)


# ━━━━━━━━━━━━━━━━ 通用工具函数 ━━━━━━━━━━━━━━━━


def ensure_key() -> None:
    if not AI_API_KEY:
        raise HTTPException(status_code=500, detail="AI_API_KEY 未配置，请在 .env 文件中设置")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def auth_headers() -> dict[str, str]:
    ensure_key()
    return {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}


async def llm_chat(messages: list[dict], model: str = DEFAULT_CHAT_MODEL) -> dict[str, Any]:
    """调用 LLM 聊天接口"""
    url = f"{AI_BASE_URL}/v1/chat/completions"
    payload = {"model": model, "messages": messages}
    user_msg = messages[-1].get("content", "")[:80] if messages else ""
    logger.info("[LLM] 调用聊天接口 model=%s, prompt='%s...'", model, user_msg)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=auth_headers(), json=payload)
        elapsed = time.time() - t0
        if resp.status_code >= 400:
            logger.error("[LLM] 请求失败 status=%d, 耗时=%.1fs, body=%s", resp.status_code, elapsed, resp.text[:200])
            raise HTTPException(status_code=502, detail=f"LLM 请求失败: {resp.text}")
        logger.info("[LLM] 响应成功 status=%d, 耗时=%.1fs", resp.status_code, elapsed)
        return resp.json()


async def text_to_image(prompt: str, model: str = DEFAULT_IMAGE_MODEL, n: int = 1) -> dict[str, Any]:
    """文生图"""
    url = f"{AI_BASE_URL}/v1/images/generations"
    payload = {"model": model, "prompt": prompt, "n": n}
    logger.info("[文生图] 调用 model=%s, n=%d, prompt='%s...'", model, n, prompt[:80])
    t0 = time.time()
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=auth_headers(), json=payload)
        elapsed = time.time() - t0
        if resp.status_code >= 400:
            logger.error("[文生图] 失败 status=%d, 耗时=%.1fs, body=%s", resp.status_code, elapsed, resp.text[:200])
            raise HTTPException(status_code=502, detail=f"文生图失败: {resp.text}")
        logger.info("[文生图] 成功 耗时=%.1fs", elapsed)
        return resp.json()


async def image_to_image(
    prompt: str, image_path: Path, model: str = DEFAULT_IMAGE_EDIT_MODEL, n: int = 1
) -> dict[str, Any]:
    """图生图（图片编辑）"""
    ensure_key()
    url = f"{AI_BASE_URL}/v1/images/edits"
    logger.info("[图生图] 调用 model=%s, image=%s, prompt='%s...'", model, image_path.name, prompt[:80])
    t0 = time.time()
    with image_path.open("rb") as fp:
        files = {"image": (image_path.name, fp, "application/octet-stream")}
        data = {"model": model, "prompt": prompt, "n": str(n), "response_format": "url"}
        headers = {"Authorization": f"Bearer {AI_API_KEY}"}
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(url, headers=headers, data=data, files=files)
            elapsed = time.time() - t0
            if resp.status_code >= 400:
                logger.error("[图生图] 失败 status=%d, 耗时=%.1fs, body=%s", resp.status_code, elapsed, resp.text[:200])
                raise HTTPException(status_code=502, detail=f"图生图失败: {resp.text}")
            logger.info("[图生图] 成功 耗时=%.1fs", elapsed)
            return resp.json()


def get_image_url(payload: dict[str, Any]) -> str:
    """从 API 响应中提取图片 URL"""
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise HTTPException(status_code=502, detail="API 响应中没有图片数据")
    url = data[0].get("url")
    if not isinstance(url, str) or not url:
        raise HTTPException(status_code=502, detail="API 响应中没有图片 URL")
    return url


async def download_image(url: str, output_path: Path) -> None:
    """下载图片到本地"""
    logger.info("[下载] 开始下载图片 -> %s", output_path.name)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.get(url)
        elapsed = time.time() - t0
        if resp.status_code >= 400:
            logger.error("[下载] 失败 status=%d, 耗时=%.1fs", resp.status_code, elapsed)
            raise HTTPException(status_code=502, detail="图片下载失败")
        output_path.write_bytes(resp.content)
        size_kb = len(resp.content) / 1024
        logger.info("[下载] 完成 %s (%.1f KB, %.1fs)", output_path.name, size_kb, elapsed)


def extract_json(text: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON"""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).strip()
        raw = raw[:-3].strip() if raw.endswith("```") else raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, (dict, list)):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("无法从 LLM 响应中解析 JSON")


# ━━━━━━━━━━━━━━━━ 项目管理 ━━━━━━━━━━━━━━━━


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def frames_dir(pid: str) -> Path:
    return project_dir(pid) / "frames"


def save_project(proj: dict[str, Any]) -> None:
    """保存项目到内存和磁盘"""
    pid = proj["id"]
    pdir = project_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.json").write_text(
        json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with projects_lock:
        projects[pid] = proj
    logger.debug("[项目] 保存 pid=%s status=%s", pid, proj.get("status"))


def load_project(pid: str) -> dict[str, Any] | None:
    with projects_lock:
        if pid in projects:
            return dict(projects[pid])
    pfile = project_dir(pid) / "project.json"
    if pfile.exists():
        proj = json.loads(pfile.read_text(encoding="utf-8"))
        with projects_lock:
            projects[pid] = proj
        return dict(proj)
    return None


def require_project(pid: str) -> dict[str, Any]:
    proj = load_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    return proj


def update_project(pid: str, **patch: Any) -> dict[str, Any]:
    proj = require_project(pid)
    proj.update(patch)
    proj["updated_at"] = now_iso()
    save_project(proj)
    return proj


# ━━━━━━━━━━━━━━━━ 分镜生成 ━━━━━━━━━━━━━━━━


async def generate_storyboard(proj: dict[str, Any]) -> dict[str, Any]:
    """用 LLM 生成逐帧分镜"""
    fc = proj["frame_count"]
    logger.info("[分镜] 开始生成分镜 pid=%s, frame_count=%d", proj["id"], fc)
    prompt = f"""你是一位专业的定格动画分镜设计师。请根据以下信息，设计一个定格动画的逐帧分镜。

重要规则：
1. 总帧数为 {fc} 帧（FPS={proj['fps']}，时长={proj['duration_seconds']}秒）
2. 每帧之间的变化必须非常微小（这是定格动画的核心特点）
3. 角色的外观、服装、风格在每一帧中必须完全一致
4. 第一帧的 edit_prompt 必须留空字符串
5. 从第二帧开始，edit_prompt 描述相对于上一帧的微小变化
6. edit_prompt 用于图片编辑API，必须具体、可操作，例如 "move the left arm slightly upward by about 5 degrees"
7. 每帧的 description 应包含完整的画面描述
8. 所有 description 和 edit_prompt 使用英文（图片生成模型对英文效果更好）
9. title 和 summary 使用中文

场景描述：{proj['scene_description']}
角色描述：{proj['character_description']}
风格描述：{proj['style_description']}

请以严格的JSON格式返回（不要包含markdown代码块标记）：
{{
  "title": "动画标题（中文）",
  "summary": "一句话描述（中文）",
  "frames": [
    {{"index": 1, "description": "full visual description in English for text-to-image", "edit_prompt": ""}},
    {{"index": 2, "description": "full visual description in English", "edit_prompt": "specific small change from previous frame in English"}}
  ]
}}"""

    messages = [
        {
            "role": "system",
            "content": "你是一个严格的JSON生成器。只输出JSON，不输出其他内容。不要用markdown代码块包裹。",
        },
        {"role": "user", "content": prompt},
    ]

    resp = await llm_chat(messages, model=DEFAULT_CHAT_MODEL)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    logger.info("[分镜] LLM 返回内容长度=%d", len(content))

    if not content.strip():
        logger.error("[分镜] LLM 返回了空内容")
        raise HTTPException(status_code=502, detail="LLM 返回了空内容")

    parsed = extract_json(content)
    logger.info("[分镜] JSON 解析成功, 包含 %d 帧", len(parsed.get("frames", [])))

    # 规范化帧数据
    frames_raw = parsed.get("frames", [])
    frames = []
    for i, f in enumerate(frames_raw[:fc]):
        frames.append(
            {
                "index": i + 1,
                "description": f.get("description", f"Frame {i + 1} of the animation"),
                "edit_prompt": f.get("edit_prompt", "") if i > 0 else "",
            }
        )

    # 不足的帧用默认值补齐
    while len(frames) < fc:
        idx = len(frames) + 1
        frames.append(
            {
                "index": idx,
                "description": f"Continuation of the scene, frame {idx}/{fc}.",
                "edit_prompt": "Very slight continuation of motion from previous frame, maintaining all visual consistency.",
            }
        )

    result = {
        "title": parsed.get("title", "未命名动画"),
        "summary": parsed.get("summary", proj["scene_description"]),
        "frames": frames,
    }
    logger.info("[分镜] 生成完成: title='%s', 最终帧数=%d", result["title"], len(frames))
    return result


# ━━━━━━━━━━━━━━━━ 视频渲染 ━━━━━━━━━━━━━━━━


def render_video_ffmpeg(frame_dir: Path, fps: int, output_path: Path) -> tuple[bool, str]:
    """用 ffmpeg 合成 MP4"""
    logger.info("[渲染] 尝试 ffmpeg 合成 MP4, fps=%d, output=%s", fps, output_path.name)
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        logger.warning("[渲染] ffmpeg 未安装，将回退到 GIF")
        return False, "未安装 ffmpeg"

    input_pattern = frame_dir / "frame_%04d.jpg"
    command = [
        ffmpeg_path,
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(input_pattern),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        str(output_path),
    ]

    try:
        t0 = time.time()
        subprocess.run(command, check=True, capture_output=True, text=True)
        elapsed = time.time() - t0
        logger.info("[渲染] ffmpeg 合成成功 耗时=%.1fs, output=%s", elapsed, output_path.name)
        return True, ""
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "ffmpeg error")[-500:]
        logger.error("[渲染] ffmpeg 合成失败: %s", err[:200])
        return False, err


def render_gif(frame_dir: Path, fps: int, output_path: Path) -> None:
    """回退方案：用 Pillow 生成 GIF"""
    logger.info("[渲染] 使用 Pillow 生成 GIF, fps=%d", fps)
    frame_files = sorted(frame_dir.glob("frame_*.jpg"))
    if not frame_files:
        logger.error("[渲染] 没有找到帧文件")
        raise RuntimeError("没有可渲染的帧")
    logger.info("[渲染] 找到 %d 帧图片", len(frame_files))
    t0 = time.time()
    images = [Image.open(p).convert("RGB") for p in frame_files]
    duration = int(1000 / max(1, fps))
    images[0].save(output_path, save_all=True, append_images=images[1:], duration=duration, loop=0)
    for img in images:
        img.close()
    elapsed = time.time() - t0
    logger.info("[渲染] GIF 生成完成 耗时=%.1fs, output=%s", elapsed, output_path.name)


# ━━━━━━━━━━━━━━━━ 后台帧生成任务 ━━━━━━━━━━━━━━━━


async def run_frame_generation(pid: str) -> None:
    """后台任务：逐帧生成所有动画帧"""
    logger.info("[帧生成] ========== 开始后台帧生成任务 pid=%s ==========", pid)
    proj = require_project(pid)
    fdir = frames_dir(pid)
    fdir.mkdir(parents=True, exist_ok=True)

    storyboard = proj.get("storyboard")
    if not storyboard:
        logger.error("[帧生成] 没有分镜数据 pid=%s", pid)
        update_project(pid, status="failed", error="没有分镜数据")
        return

    sb_frames = storyboard["frames"]
    total = len(sb_frames)
    logger.info("[帧生成] 总共需要生成 %d 帧", total)

    # 首帧必须已存在
    first_frame_path = project_dir(pid) / "first_frame.jpg"
    if not first_frame_path.exists():
        logger.error("[帧生成] 首帧图片不存在 pid=%s", pid)
        update_project(pid, status="failed", error="首帧图片不存在")
        return

    job_start = time.time()
    try:
        # 复制首帧作为 frame_0001
        frame1_path = fdir / "frame_0001.jpg"
        shutil.copy2(first_frame_path, frame1_path)
        logger.info("[帧生成] 帧 1/%d 完成（复制首帧）", total)

        generated = [f"/project-files/{pid}/frames/frame_0001.jpg"]
        update_project(
            pid,
            status="generating_frames",
            generated_frames=generated,
            generation_current=1,
            generation_total=total,
            generation_progress=int(1 / total * 100),
            generation_message=f"帧 1/{total} 完成（首帧）",
        )

        prev_frame_path = frame1_path

        for i in range(1, total):
            frame_start = time.time()
            frame_info = sb_frames[i]
            edit_prompt = frame_info.get("edit_prompt", "")
            logger.info("[帧生成] 开始生成帧 %d/%d", i + 1, total)

            if not edit_prompt:
                edit_prompt = (
                    f"Very slight continuation of the animation. "
                    f"{frame_info.get('description', '')}"
                )

            # 添加风格一致性提示
            style_parts = []
            if proj.get("style_description"):
                style_parts.append(f"Maintain consistent style: {proj['style_description']}")
            if proj.get("character_description"):
                style_parts.append(
                    f"Keep character appearance exactly the same: {proj['character_description']}"
                )
            if style_parts:
                edit_prompt = edit_prompt + ". " + ". ".join(style_parts)

            # 调用图生图 API
            logger.info("[帧生成] 帧 %d/%d: 调用图生图API, prompt='%s...'", i + 1, total, edit_prompt[:60])
            result = await image_to_image(
                prompt=edit_prompt,
                image_path=prev_frame_path,
                model=DEFAULT_IMAGE_EDIT_MODEL,
            )

            image_url = get_image_url(result)
            frame_path = fdir / f"frame_{i + 1:04d}.jpg"
            await download_image(image_url, frame_path)

            frame_elapsed = time.time() - frame_start
            logger.info("[帧生成] 帧 %d/%d 完成 耗时=%.1fs", i + 1, total, frame_elapsed)

            prev_frame_path = frame_path
            generated.append(f"/project-files/{pid}/frames/frame_{i + 1:04d}.jpg")

            pct = int((i + 1) / total * 100)
            update_project(
                pid,
                generated_frames=list(generated),
                generation_current=i + 1,
                generation_progress=min(pct, 99),
                generation_message=f"帧 {i + 1}/{total} 完成",
            )

        job_elapsed = time.time() - job_start
        logger.info("[帧生成] ========== 全部 %d 帧生成完成 pid=%s, 总耗时=%.1fs ==========", total, pid, job_elapsed)
        update_project(
            pid,
            status="frames_ready",
            generation_progress=100,
            generation_message=f"所有 {total} 帧生成完成",
        )

    except Exception as exc:
        job_elapsed = time.time() - job_start
        logger.error("[帧生成] 任务失败 pid=%s, 耗时=%.1fs, error=%s", pid, job_elapsed, str(exc))
        update_project(
            pid,
            status="failed",
            error=f"帧生成失败: {str(exc)}",
            generation_message=f"生成失败: {str(exc)}",
        )


# ━━━━━━━━━━━━━━━━ API 路由 ━━━━━━━━━━━━━━━━


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


# ──── 项目 CRUD ────


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    pid = uuid.uuid4().hex[:12]
    fc = req.frame_count or (req.fps * req.duration_seconds)
    fc = max(2, min(fc, 200))
    logger.info("[Step1 场景设定] 创建项目 pid=%s, fps=%d, duration=%ds, frames=%d", pid, req.fps, req.duration_seconds, fc)
    logger.info("[Step1 场景设定] 场景='%s', 角色='%s', 风格='%s'", req.scene_description[:50], req.character_description[:50], req.style_description[:50])
    proj = {
        "id": pid,
        "status": "draft",
        "scene_description": req.scene_description,
        "character_description": req.character_description,
        "style_description": req.style_description,
        "fps": req.fps,
        "duration_seconds": req.duration_seconds,
        "frame_count": fc,
        "storyboard": None,
        "first_frame_url": None,
        "generated_frames": [],
        "generation_progress": 0,
        "generation_current": 0,
        "generation_total": fc,
        "generation_message": "",
        "video_url": None,
        "video_type": None,
        "error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    save_project(proj)
    logger.info("[Step1 场景设定] 项目创建完成 pid=%s", pid)
    return proj


@app.get("/api/projects")
async def list_projects():
    with projects_lock:
        all_projs = sorted(
            projects.values(), key=lambda p: p.get("created_at", ""), reverse=True
        )
    return {"projects": all_projs}


@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    return require_project(pid)


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    logger.info("[项目] 删除项目 pid=%s", pid)
    with projects_lock:
        projects.pop(pid, None)
    pdir = project_dir(pid)
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)
    for ext in [".mp4", ".gif"]:
        out = OUTPUTS_DIR / f"{pid}{ext}"
        if out.exists():
            out.unlink()
    return {"status": "deleted"}


# ──── 分镜 ────


@app.post("/api/projects/{pid}/storyboard/generate")
async def api_generate_storyboard(pid: str):
    """AI 生成分镜"""
    logger.info("[Step2 分镜设计] 开始AI生成分镜 pid=%s", pid)
    proj = require_project(pid)
    try:
        t0 = time.time()
        storyboard = await generate_storyboard(proj)
        elapsed = time.time() - t0
        logger.info("[Step2 分镜设计] 分镜生成完成 pid=%s, 帧数=%d, 耗时=%.1fs", pid, len(storyboard["frames"]), elapsed)
        proj = update_project(pid, storyboard=storyboard, status="storyboard_ready", error=None)
        return proj
    except Exception as exc:
        logger.error("[Step2 分镜设计] 分镜生成失败 pid=%s, error=%s", pid, str(exc))
        update_project(pid, error=f"分镜生成失败: {str(exc)}")
        raise HTTPException(status_code=502, detail=f"分镜生成失败: {str(exc)}")


@app.put("/api/projects/{pid}/storyboard")
async def api_update_storyboard(pid: str, req: UpdateStoryboardRequest):
    """用户编辑后更新分镜"""
    logger.info("[Step2 分镜设计] 用户更新分镜 pid=%s, 帧数=%d", pid, len(req.frames))
    proj = require_project(pid)
    storyboard = proj.get("storyboard") or {"title": "", "summary": "", "frames": []}

    storyboard["frames"] = [
        {
            "index": i + 1,
            "description": f.get("description", ""),
            "edit_prompt": f.get("edit_prompt", "") if i > 0 else "",
        }
        for i, f in enumerate(req.frames)
    ]

    fc = len(storyboard["frames"])
    proj = update_project(pid, storyboard=storyboard, frame_count=fc, status="storyboard_ready")
    logger.info("[Step2 分镜设计] 分镜已确认 pid=%s, 最终帧数=%d", pid, fc)
    return proj


# ──── 首帧 ────


@app.post("/api/projects/{pid}/first-frame/generate")
async def api_generate_first_frame(pid: str, req: GenerateFirstFrameRequest):
    """用 AI 文生图生成首帧"""
    logger.info("[Step3 首帧] AI文生图生成首帧 pid=%s, prompt='%s...'", pid, req.prompt[:60])
    require_project(pid)
    t0 = time.time()
    result = await text_to_image(prompt=req.prompt, model=DEFAULT_IMAGE_MODEL)
    image_url = get_image_url(result)

    first_frame_path = project_dir(pid) / "first_frame.jpg"
    await download_image(image_url, first_frame_path)

    # 确保是 JPEG 格式
    try:
        img = Image.open(first_frame_path).convert("RGB")
        img.save(first_frame_path, "JPEG", quality=95)
        img.close()
    except Exception:
        pass

    ff_url = f"/project-files/{pid}/first_frame.jpg"
    elapsed = time.time() - t0
    logger.info("[Step3 首帧] 首帧生成并保存完成 pid=%s, 耗时=%.1fs", pid, elapsed)
    proj = update_project(pid, first_frame_url=ff_url, status="first_frame_ready", error=None)
    return proj


@app.post("/api/projects/{pid}/first-frame/upload")
async def api_upload_first_frame(pid: str, image: UploadFile = File(...)):
    """上传首帧图片"""
    logger.info("[Step3 首帧] 用户上传首帧图片 pid=%s, filename=%s", pid, image.filename)
    require_project(pid)
    pdir = project_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    first_frame_path = pdir / "first_frame.jpg"

    tmp_path = pdir / f"tmp_{uuid.uuid4().hex}"
    tmp_path.write_bytes(await image.read())
    try:
        img = Image.open(tmp_path).convert("RGB")
        img.save(first_frame_path, "JPEG", quality=95)
        img.close()
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    ff_url = f"/project-files/{pid}/first_frame.jpg"
    logger.info("[Step3 首帧] 首帧上传并保存完成 pid=%s", pid)
    proj = update_project(pid, first_frame_url=ff_url, status="first_frame_ready", error=None)
    return proj


# ──── 帧生成 ────


@app.post("/api/projects/{pid}/generate-frames")
async def api_generate_frames(pid: str, background_tasks: BackgroundTasks):
    """启动后台帧生成任务"""
    logger.info("[Step4 帧生成] 收到帧生成请求 pid=%s", pid)
    proj = require_project(pid)

    if not proj.get("storyboard"):
        logger.warning("[Step4 帧生成] 无分镜数据 pid=%s", pid)
        raise HTTPException(status_code=400, detail="请先生成分镜")
    if not proj.get("first_frame_url"):
        logger.warning("[Step4 帧生成] 无首帧图片 pid=%s", pid)
        raise HTTPException(status_code=400, detail="请先设置首帧图片")

    # 清除旧帧
    fdir = frames_dir(pid)
    if fdir.exists():
        shutil.rmtree(fdir, ignore_errors=True)

    update_project(
        pid,
        status="generating_frames",
        generation_progress=0,
        generation_current=0,
        generation_message="开始生成帧...",
        generated_frames=[],
        error=None,
        video_url=None,
        video_type=None,
    )

    background_tasks.add_task(run_frame_generation, pid)
    logger.info("[Step4 帧生成] 后台任务已启动 pid=%s, 总帧数=%d", pid, proj["frame_count"])
    return {"status": "started", "project_id": pid}


# ──── 视频渲染 ────


@app.post("/api/projects/{pid}/render-video")
async def api_render_video(pid: str):
    """渲染最终视频"""
    logger.info("[Step5 视频输出] 开始渲染视频 pid=%s", pid)
    proj = require_project(pid)

    if proj["status"] not in ("frames_ready", "completed"):
        logger.warning("[Step5 视频输出] 状态不允许渲染 pid=%s, status=%s", pid, proj["status"])
        raise HTTPException(status_code=400, detail="请先完成所有帧的生成")

    fdir = frames_dir(pid)
    fps = proj["fps"]

    update_project(pid, status="rendering", generation_message="正在渲染视频...")

    # 优先使用 ffmpeg
    output_mp4 = OUTPUTS_DIR / f"{pid}.mp4"
    ok, reason = render_video_ffmpeg(fdir, fps, output_mp4)

    if ok:
        logger.info("[Step5 视频输出] MP4 渲染成功 pid=%s", pid)
        video_url = f"/outputs/{pid}.mp4"
        proj = update_project(
            pid,
            status="completed",
            video_url=video_url,
            video_type="video/mp4",
            generation_message="视频生成完成！",
            error=None,
        )
        return proj

    # 回退 GIF
    logger.info("[Step5 视频输出] ffmpeg 失败，回退到 GIF pid=%s", pid)
    output_gif = OUTPUTS_DIR / f"{pid}.gif"
    try:
        render_gif(fdir, fps, output_gif)
        logger.info("[Step5 视频输出] GIF 生成成功 pid=%s", pid)
        video_url = f"/outputs/{pid}.gif"
        proj = update_project(
            pid,
            status="completed",
            video_url=video_url,
            video_type="image/gif",
            generation_message=f"动画生成完成（GIF 格式，因为 {reason}）",
            error=None,
        )
        return proj
    except Exception as exc:
        logger.error("[Step5 视频输出] 渲染失败 pid=%s, error=%s", pid, str(exc))
        update_project(pid, status="failed", error=f"渲染失败: {str(exc)}")
        raise HTTPException(status_code=500, detail=f"渲染失败: {str(exc)}")


# ──── 工具接口 ────


@app.post("/api/chat")
async def chat(req: ChatRequest):
    logger.info("[工具] 聊天请求 model=%s, prompt='%s...'", req.model, req.prompt[:50])
    return await llm_chat([{"role": "user", "content": req.prompt}], model=req.model)


@app.post("/api/images/generate")
async def api_image_generate(req: ImageGenRequest):
    logger.info("[工具] 文生图请求 model=%s, n=%d", req.model, req.n)
    return await text_to_image(prompt=req.prompt, model=req.model, n=req.n)


@app.post("/api/images/edit")
async def api_image_edit(
    prompt: str = Form(...),
    model: str = Form(DEFAULT_IMAGE_EDIT_MODEL),
    n: int = Form(1),
    image: UploadFile = File(...),
):
    logger.info("[工具] 图生图请求 model=%s, filename=%s", model, image.filename)
    ext = Path(image.filename or "upload.png").suffix or ".png"
    temp_path = UPLOADS_DIR / f"edit_tmp_{uuid.uuid4().hex}{ext}"
    temp_path.write_bytes(await image.read())
    try:
        return await image_to_image(prompt=prompt, image_path=temp_path, model=model, n=n)
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = Path(file.filename or "upload.png").suffix or ".png"
    file_id = f"{uuid.uuid4().hex}{ext.lower()}"
    path = UPLOADS_DIR / file_id
    content = await file.read()
    path.write_bytes(content)
    logger.info("[工具] 文件上传 filename=%s, size=%.1fKB, id=%s", file.filename, len(content)/1024, file_id)
    return {"file_id": file_id, "url": f"/uploads/{file_id}"}


# ━━━━━━━━━━━━━━━━ 静态文件 ━━━━━━━━━━━━━━━━

app.mount("/project-files", StaticFiles(directory=str(PROJECTS_DIR)), name="project-files")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


