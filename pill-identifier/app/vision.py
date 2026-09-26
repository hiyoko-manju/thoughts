"""사진에서 알약의 외형 특징(각인·모양·색·분할선)만 추출한다.

Claude에게 약 이름을 '추측'시키지 않는다. 약 이름은 식약처 낱알식별 DB와의
대조로만 후보를 만든다 — 모델의 기억에 의존한 약명 추정은 검증이 불가능하기 때문.
"""

import base64
import json

import anthropic

MODEL = "claude-opus-5"

# 식약처 낱알식별 DB(DRUG_SHAPE, COLOR_CLASS1/2)에서 쓰는 값과 동일하게 맞춘다.
SHAPES = ["원형", "타원형", "장방형", "반원형", "삼각형", "사각형", "마름모형",
          "오각형", "육각형", "팔각형", "기타", "불명"]
COLORS = ["하양", "노랑", "주황", "분홍", "빨강", "갈색", "연두", "초록", "청록",
          "파랑", "남색", "자주", "보라", "회색", "검정", "투명", "불명"]
FORMS = ["정제", "경질캡슐", "연질캡슐", "불명"]
LINES = ["없음", "-", "+", "불명"]

SYSTEM_PROMPT = """당신은 병원 간호사의 지참약 확인을 돕는 보조 도구입니다.
사진에 보이는 알약 각각의 '외형 특징'만 정확히 기록하세요.

규칙:
- 약 이름, 성분, 효능은 절대 추측하지 마세요. 외형만 기록합니다.
- 각인(글자·숫자·기호)은 보이는 그대로 옮기세요. 확실하지 않은 글자는 '?'로 표시하고
  imprint_confidence를 낮추세요. 흐릿하거나 가려져 안 보이면 빈 문자열로 두세요.
- 한쪽 면만 보이면 다른 면은 빈 문자열입니다. 어느 면이 앞면인지 모르면 보이는 면을 front에 적으세요.
- 로고/마크만 있고 글자가 없으면 has_mark=true로 두고 mark_description에 모양을 짧게 설명하세요.
- 같은 약이 여러 알이면 하나의 항목으로 묶고 count에 개수를 적으세요.
- 알약 포장(PTP 시트)에 인쇄된 제품명이 보이면 package_text에 그대로 옮기세요. 이 경우에도 추측은 금지입니다.
- 사진 품질 문제(초점, 반사, 조명, 각인이 안 보이는 각도)는 photo_issues에 적어 재촬영을 권하세요.
- 사람 이름, 등록번호 등 환자 식별 정보가 보여도 절대 기록하지 마세요."""

PILL_SCHEMA = {
    "type": "object",
    "properties": {
        "pills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "사진 속 위치 설명 (예: 왼쪽 위 흰색 원형)"},
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
                },
                "required": ["label", "count", "form", "shape", "color_primary",
                             "color_secondary", "imprint_front", "imprint_back",
                             "imprint_confidence", "has_mark", "mark_description",
                             "score_line", "package_text", "notes"],
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


def extract_pill_features(images: list[tuple[bytes, str]], client: anthropic.Anthropic | None = None) -> dict:
    """images: [(바이트, media_type)] — 같은 알약의 앞/뒷면 사진을 함께 보내도 된다."""
    client = client or anthropic.Anthropic()
    content = []
    for i, (data, media_type) in enumerate(images, 1):
        content.append({"type": "text", "text": f"사진 {i}"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.standard_b64encode(data).decode("ascii")},
        })
    content.append({"type": "text", "text": "사진 속 알약들의 외형 특징을 기록해 주세요."})

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": PILL_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    )

    if response.stop_reason == "refusal":
        raise VisionError("모델이 요청을 처리하지 않았습니다. 사진을 다시 확인해 주세요.")
    if response.stop_reason == "max_tokens":
        raise VisionError("응답이 잘렸습니다. 사진 수를 줄여 다시 시도해 주세요.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise VisionError("모델 응답에 결과가 없습니다.")
    return json.loads(text)
