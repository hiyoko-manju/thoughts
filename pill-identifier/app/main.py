import asyncio
import base64
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import pilldb
from .vision import COLORS, FORMS, SHAPES, VisionError, extract_pill_features

STATIC = Path(__file__).resolve().parent.parent / "static"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGES = 6
MAX_BYTES = 15 * 1024 * 1024  # 브라우저에서 긴 변 4096px로 줄여 보내므로 보통 2~5MB

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pill")

app = FastAPI(title="지참약 사진 식별 보조")

# 인터넷에 올릴 때는 APP_PASSWORD를 설정해 잠근다 (브라우저 기본 로그인 창, 아이디는 아무거나).
FAIL_WINDOW = 600
FAIL_LIMIT = 10
_failures: dict[str, list[float]] = defaultdict(list)


def _password_ok(header: str | None, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(given.encode(), password.encode())


@app.middleware("http")
async def require_password(request: Request, call_next):
    password = os.environ.get("APP_PASSWORD")
    if not password or request.url.path == "/healthz":
        return await call_next(request)
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    now = time.time()
    recent = [t for t in _failures[ip] if now - t < FAIL_WINDOW]
    _failures[ip] = recent
    if len(recent) >= FAIL_LIMIT:
        return PlainTextResponse("비밀번호를 여러 번 틀렸습니다. 10분 뒤 다시 시도하세요.", status_code=429)
    auth = request.headers.get("authorization")
    if _password_ok(auth, password):
        return await call_next(request)
    if auth:
        recent.append(now)
    return PlainTextResponse("비밀번호가 필요합니다.", status_code=401,
                             headers={"WWW-Authenticate": 'Basic realm="pill", charset="UTF-8"'})

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    with pilldb.connect() as conn:
        return {**pilldb.stats(conn), "shapes": SHAPES, "colors": COLORS, "forms": FORMS}


class ManualQuery(BaseModel):
    imprint_front: str = ""
    imprint_back: str = ""
    shape: str = "불명"
    color_primary: str = "불명"
    color_secondary: str = "없음"
    form: str = "불명"


@app.post("/api/search")
def search(q: ManualQuery):
    """사진 판독이 애매할 때 간호사가 직접 읽은 각인으로 검색. 외부 전송 없이 로컬 DB만 쓴다."""
    with pilldb.connect() as conn:
        return {"candidates": pilldb.find_candidates(conn, q.model_dump())}


# 분석은 1분 넘게 걸릴 수 있다. 휴대폰 브라우저는 오래 기다리는 요청을 끊으므로
# 바로 작업 번호를 돌려주고, 화면이 2초마다 결과를 물어보게 한다.
JOB_TTL = 15 * 60
_jobs: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def _error_message(e: Exception) -> str:
    if isinstance(e, VisionError):
        return str(e)
    if isinstance(e, anthropic.RateLimitError):
        return "요청이 많습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic API 키가 올바르지 않습니다. Render의 ANTHROPIC_API_KEY를 확인하세요."
    if isinstance(e, anthropic.APIStatusError):
        detail = getattr(e, "message", "") or ""
        return f"이미지 분석 서비스 오류 ({e.status_code}): {detail[:200]}"
    if isinstance(e, anthropic.APIConnectionError):
        return "이미지 분석 서비스에 연결할 수 없습니다."
    return f"알 수 없는 오류: {type(e).__name__}"


async def _run_job(job_id: str, payload: list, single: bool) -> None:
    job = _jobs[job_id]
    try:
        features = await run_in_threadpool(extract_pill_features, payload, single=single)
        with pilldb.connect() as conn:
            for pill in features["pills"]:
                pill["candidates"] = pilldb.find_candidates(conn, pill)
            features["db"] = pilldb.stats(conn)
        job.update(status="done", result=features)
        log.info("job %s done: %d pills, %s zooms, mode=%s", job_id, len(features["pills"]),
                 features.get("zooms"), features.get("mode"))
    except Exception as e:  # 어떤 오류든 화면에 알려야 한다
        log.exception("job %s failed", job_id)
        job.update(status="error", error=_error_message(e))


@app.post("/api/identify")
async def identify(images: list[UploadFile] = File(...), single: bool = Query(False)):
    if not images or len(images) > MAX_IMAGES:
        raise HTTPException(400, f"사진은 1~{MAX_IMAGES}장까지 올릴 수 있습니다.")
    payload = []
    for f in images:
        if f.content_type not in ALLOWED_TYPES:
            raise HTTPException(400, f"지원하지 않는 형식입니다: {f.content_type}")
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(400, "사진 용량이 너무 큽니다 (최대 15MB).")
        payload.append((data, f.content_type))
    # 사진은 메모리에서만 처리하고 서버에 저장하지 않는다.

    now = time.time()
    for old in [k for k, v in _jobs.items() if now - v["created"] > JOB_TTL]:
        del _jobs[old]
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"status": "running", "created": now}
    task = asyncio.create_task(_run_job(job_id, payload, single))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다. 서버가 재시작됐을 수 있으니 다시 분석해 주세요.")
    return {k: v for k, v in job.items() if k != "created"} | {"elapsed": round(time.time() - job["created"])}
