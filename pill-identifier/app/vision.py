"""사진에서 알약의 외형 특징(각인·모양·색·분할선)만 추출한다.

Claude에게 약 이름을 '추측'시키지 않는다. 약 이름은 식약처 낱알식별 DB와의
대조로만 후보를 만든다 — 모델의 기억에 의존한 약명 추정은 검증이 불가능하기 때문.

정확도를 위해 모델에게 zoom 도구를 준다. 모델이 알약 좌표를 지정하면 서버가
그 부분을 잘라 확대하고, 흰 알약의 음각이 잘 보이도록 흑백 대비 강조본도 함께 돌려준다.
"""

import base64
import io
import json

import anthropic
from PIL import Image, ImageDraw, ImageFilter, ImageOps

MODEL = "claude-opus-5"
MAX_EDGE = 2576  # 모델이 받는 최대 해상도 (긴 변). 이 이하면 좌표가 픽셀과 1:1.
ZOOM_EDGE = 1024  # 확대 이미지의 긴 변
MAX_ZOOMS = 24
MAX_TURNS = 12

# 식약처 낱알식별 DB(DRUG_SHAPE, COLOR_CLASS1/2)에서 쓰는 값과 동일하게 맞춘다.
SHAPES = ["원형", "타원형", "장방형", "반원형", "삼각형", "사각형", "마름모형",
          "오각형", "육각형", "팔각형", "기타", "불명"]
COLORS = ["하양", "노랑", "주황", "분홍", "빨강", "갈색", "연두", "초록", "청록",
          "파랑", "남색", "자주", "보라", "회색", "검정", "투명", "불명"]
FORMS = ["정제", "경질캡슐", "연질캡슐", "불명"]
LINES = ["없음", "-", "+", "불명"]

SYSTEM_PROMPT = """당신은 병원 간호사의 지참약 확인을 돕는 보조 도구입니다.
사진에 보이는 알약 각각의 '외형 특징'만 정확히 기록하세요.

작업 순서:
1. 사진 전체를 보고 알약들의 위치를 파악합니다. 여러 장이면 같은 알약의 다른 면일 수 있습니다
   (예: 사진 1은 앞면, 사진 2는 뒤집어서 찍은 뒷면). 위치·모양·색으로 같은 알약끼리 묶으세요.
2. 각인을 기록하기 전에 반드시 zoom 도구로 알약마다(면마다) 확대해서 읽으세요.
   흰색·연한 색 알약의 음각은 원본에서 거의 안 보이므로 함께 오는 '대비 강조' 이미지를 꼭 확인하세요.
   글자가 애매하면 더 좁게 다시 확대해도 됩니다.
3. 결과를 기록합니다.

각인 기록 규칙:
- 보이는 글자·숫자·기호를 그대로 옮기되, 글자가 기울거나 뒤집혀 있으면 바로 세운 방향으로 읽어 기록하세요.
- 분할선 위아래(또는 좌우)로 나뉜 글자는 같은 면입니다. 한 면에 순서대로 이어 적으세요 (예: 위 'DLI', 아래 'DLI' → 'DLI DLI').
- 확실하지 않은 글자는 '?'로 표시하고 imprint_confidence를 낮추세요. 안 보이면 빈 문자열로 두세요.
- 한쪽 면만 보이면 다른 면은 빈 문자열입니다. 어느 면이 앞면인지 모르면 글자가 많은 면을 front에 적으세요.
- 로고/마크만 있으면 has_mark=true, mark_description에 모양을 짧게 적으세요.

그 밖의 규칙:
- 약 이름, 성분, 효능은 절대 추측하지 마세요. 외형만 기록합니다.
- 같은 약이 여러 알이면 하나로 묶고 count에 개수를 적으세요 (같은 알약을 앞뒤로 찍은 것은 1알입니다).
- views에는 이 알약이 보이는 사진 번호와 알약을 감싸는 픽셀 좌표를 사진마다 적으세요.
- 알약 포장(PTP 시트)에 인쇄된 제품명이 보이면 package_text에 그대로 옮기세요.
- 사진 품질 문제(초점, 반사, 조명)는 photo_issues에 짧게 적으세요.
- notes는 판독에 필요한 내용만 한 문장 이내로 적으세요.
- 사람 이름, 등록번호 등 환자 식별 정보가 보여도 절대 기록하지 마세요."""

ZOOM_TOOL = {
    "name": "zoom",
    "description": ("사진의 일부를 잘라 확대해서 봅니다. 좌표는 해당 사진의 픽셀 좌표입니다. "
                    "알약 하나를 약간 여유 있게 감싸도록 지정하세요. "
                    "확대 원본과 흑백 대비 강조본 두 장이 돌아옵니다."),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "photo": {"type": "integer", "description": "사진 번호 (1부터)"},
            "x0": {"type": "integer"}, "y0": {"type": "integer"},
            "x1": {"type": "integer"}, "y1": {"type": "integer"},
        },
        "required": ["photo", "x0", "y0", "x1", "y1"],
        "additionalProperties": False,
    },
}

VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "photo": {"type": "integer"},
        "x0": {"type": "integer"}, "y0": {"type": "integer"},
        "x1": {"type": "integer"}, "y1": {"type": "integer"},
    },
    "required": ["photo", "x0", "y0", "x1", "y1"],
    "additionalProperties": False,
}

PILL_SCHEMA = {
    "type": "object",
    "properties": {
        "pills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "짧은 외형 설명 (예: 흰색 원형)"},
                    "count": {"type": "integer"},
                    "form": {"type": "string", "enum": FORMS},
                    "shape": {"type": "string", "enum": SHAPES},
                    "color_primary": {"type": "string", "enum": COLORS},
                    "color_secondary": {"type": "string", "enum": COLORS + ["없음"]},
                    "imprint_front": {"type": "string"},
                    "imprint_back": {"type": "string"},
                    "imprint_confidence": {"type": "string", "enum": ["높음", "중간", "낮음", "없음"]},
                    "has_mark": {"type": "boolean"},
                    "mark_description": {"type": "string"},
                    "score_line": {"type": "string", "enum": LINES},
                    "package_text": {"type": "string"},
                    "notes": {"type": "string"},
                    "views": {"type": "array", "items": VIEW_SCHEMA},
                },
                "required": ["label", "count", "form", "shape", "color_primary",
                             "color_secondary", "imprint_front", "imprint_back",
                             "imprint_confidence", "has_mark", "mark_description",
                             "score_line", "package_text", "notes", "views"],
                "additionalProperties": False,
            },
        },
        "photo_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pills", "photo_issues"],
    "additionalProperties": False,
}


class VisionError(Exception):
    pass


def prepare_image(data: bytes) -> Image.Image:
    """회전 정보를 반영하고 긴 변을 MAX_EDGE 이하로 맞춘다. 모델과 서버가 같은 픽셀 좌표를 쓰게 된다."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # Pillow는 형식에 따라 여러 예외를 던진다
        raise VisionError("사진을 읽을 수 없습니다.") from e
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    return img


def to_jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _image_block(img: Image.Image) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(to_jpeg(img)).decode("ascii")}}


def crop_box(img: Image.Image, x0: int, y0: int, x1: int, y1: int, pad: float = 0.15) -> Image.Image | None:
    """좌표를 정리하고 여유를 둬서 자른다. 유효하지 않으면 None."""
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    px, py = int((x1 - x0) * pad), int((y1 - y0) * pad)
    x0, y0 = max(0, x0 - px), max(0, y0 - py)
    x1, y1 = min(img.width, x1 + px), min(img.height, y1 + py)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return img.crop((x0, y0, x1, y1))


def _resize_to(img: Image.Image, edge: int) -> Image.Image:
    scale = edge / max(img.size)
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)


def enhance(img: Image.Image) -> Image.Image:
    """흰 알약의 음각이 보이도록 흑백 대비를 늘린다.

    배경이 대비 범위를 차지하지 않게, 알약이 있을 가운데 타원 영역의 밝기 분포만으로 늘린다.
    """
    gray = ImageOps.grayscale(img)
    mask = Image.new("L", gray.size, 0)
    w, h = gray.size
    ImageDraw.Draw(mask).ellipse((w * 0.2, h * 0.2, w * 0.8, h * 0.8), fill=255)
    gray = ImageOps.autocontrast(gray, cutoff=2, mask=mask)
    return gray.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=1)).convert("RGB")


def zoom(images: list[Image.Image], photo: int, x0: int, y0: int, x1: int, y1: int) -> list[dict]:
    if not 1 <= photo <= len(images):
        raise ValueError(f"사진 번호는 1~{len(images)}입니다.")
    crop = crop_box(images[photo - 1], x0, y0, x1, y1, pad=0.05)
    if crop is None:
        raise ValueError("영역이 너무 작거나 사진 밖입니다. 좌표를 다시 지정하세요.")
    big = _resize_to(crop, ZOOM_EDGE)
    return [{"type": "text", "text": "확대 원본"}, _image_block(big),
            {"type": "text", "text": "흑백 대비 강조"}, _image_block(enhance(big))]


def thumbnail(img: Image.Image, view: dict, edge: int = 320) -> str | None:
    """화면에 '촬영한 알약'으로 보여줄 작은 이미지 (data URL)."""
    crop = crop_box(img, view["x0"], view["y0"], view["x1"], view["y1"])
    if crop is None:
        return None
    if max(crop.size) > edge:
        crop = _resize_to(crop, edge)
    return "data:image/jpeg;base64," + base64.standard_b64encode(to_jpeg(crop, 85)).decode("ascii")


def extract_pill_features(raw_images: list[tuple[bytes, str]], single: bool = False,
                          client: anthropic.Anthropic | None = None) -> dict:
    """raw_images: [(바이트, media_type)] — 같은 알약의 앞/뒷면 사진을 함께 보내도 된다.

    single=True: 특정 알약 하나를 재촬영한 경우. 모든 사진을 같은 알약으로 보고 한 항목으로 합친다.
    """
    client = client or anthropic.Anthropic()
    images = [prepare_image(data) for data, _ in raw_images]

    content = []
    for i, img in enumerate(images, 1):
        content.append({"type": "text", "text": f"사진 {i} ({img.width}×{img.height})"})
        content.append(_image_block(img))
    if single:
        content.append({"type": "text", "text": (
            "모든 사진은 같은 한 종류의 알약을 여러 각도·면에서 다시 찍은 것입니다. "
            "사진들을 종합해 pills에 하나의 항목만 기록하고, 앞·뒷면 각인을 모두 채워 주세요.")})
    else:
        content.append({"type": "text", "text": "사진 속 알약들의 외형 특징을 기록해 주세요."})
    messages = [{"role": "user", "content": content}]

    zooms = 0
    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            tools=[ZOOM_TOOL],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": PILL_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            # zoom을 반복할 때마다 앞선 사진·확대 이미지를 다시 보내므로, 반복 부분은 캐시로 싸게 읽는다.
            cache_control={"type": "ephemeral"},
            messages=messages,
        )
        if response.stop_reason == "refusal":
            raise VisionError("모델이 요청을 처리하지 않았습니다. 사진을 다시 확인해 주세요.")
        if response.stop_reason == "max_tokens":
            raise VisionError("응답이 잘렸습니다. 사진 수를 줄여 다시 시도해 주세요.")
        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                if block.name != "zoom":
                    raise ValueError(f"알 수 없는 도구: {block.name}")
                if zooms >= MAX_ZOOMS:
                    raise ValueError("확대 횟수를 모두 썼습니다. 지금까지 본 내용으로 결과를 작성하세요.")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": zoom(images, **block.input)})
                zooms += 1
            except (TypeError, ValueError) as e:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(e), "is_error": True})
        messages.append({"role": "user", "content": results})
    else:
        raise VisionError("분석이 너무 오래 걸립니다. 사진 수를 줄여 다시 시도해 주세요.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise VisionError("모델 응답에 결과가 없습니다.")
    result = json.loads(text)

    for pill in result["pills"]:
        thumbs = []
        for view in pill.pop("views", []):
            if 1 <= view["photo"] <= len(images):
                t = thumbnail(images[view["photo"] - 1], view)
                if t:
                    thumbs.append(t)
        pill["photos"] = thumbs[:4]
    result["zooms"] = zooms
    return result
