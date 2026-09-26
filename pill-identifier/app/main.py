from pathlib import Path

import anthropic
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import pilldb
from .vision import COLORS, FORMS, SHAPES, VisionError, extract_pill_features

STATIC = Path(__file__).resolve().parent.parent / "static"
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGES = 6
MAX_BYTES = 5 * 1024 * 1024  # 브라우저에서 축소해 보내므로 보통 1MB 미만

app = FastAPI(title="지참약 사진 식별 보조")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
            raise HTTPException(400, "사진 용량이 너무 큽니다 (최대 5MB).")
        payload.append((data, f.content_type))
    # 사진은 메모리에서만 처리하고 서버에 저장하지 않는다.

    try:
        features = extract_pill_features(payload, single=single)
    except VisionError as e:
        raise HTTPException(502, str(e))
    except anthropic.RateLimitError:
        raise HTTPException(429, "요청이 많습니다. 잠시 후 다시 시도해 주세요.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"이미지 분석 서비스 오류 ({e.status_code})")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "이미지 분석 서비스에 연결할 수 없습니다.")

    with pilldb.connect() as conn:
        for pill in features["pills"]:
            pill["candidates"] = pilldb.find_candidates(conn, pill)
        db = pilldb.stats(conn)
    return {**features, "db": db}
