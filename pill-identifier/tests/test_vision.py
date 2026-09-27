import io
import json
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from app import vision


def jpeg(w, h, color=(200, 200, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


RESULT = {"pills": [{
    "label": "흰색 원형", "count": 1, "form": "정제", "shape": "원형", "color_primary": "하양",
    "color_secondary": "없음", "imprint_front": "DLI DLI", "imprint_back": "", "imprint_confidence": "높음",
    "has_mark": False, "mark_description": "", "score_line": "-", "package_text": "", "notes": "",
    "views": [{"photo": 1, "x0": 100, "y0": 100, "x1": 300, "y1": 300},
              {"photo": 9, "x0": 0, "y0": 0, "x1": 10, "y1": 10}],
}], "photo_issues": []}


class FakeClient:
    """첫 응답은 zoom 두 번(하나는 잘못된 사진 번호), 두 번째 응답은 최종 JSON."""

    def __init__(self):
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return NS(stop_reason="tool_use", content=[
                NS(type="tool_use", id="t1", name="zoom", input={"photo": 1, "x0": 100, "y0": 100, "x1": 300, "y1": 300}),
                NS(type="tool_use", id="t2", name="zoom", input={"photo": 5, "x0": 0, "y0": 0, "x1": 50, "y1": 50}),
            ])
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(RESULT))])


def test_zoom_loop_and_thumbnails():
    client = FakeClient()
    out = vision.extract_pill_features([(jpeg(4000, 3000), "image/jpeg")], client=client)

    assert client.calls[0]["cache_control"] == {"type": "ephemeral"}
    first = client.calls[0]["messages"][0]["content"]
    assert "2576×1932" in first[0]["text"]  # 긴 변 2576으로 맞춰서 보냄

    results = client.calls[1]["messages"][-1]["content"]
    ok, bad = results
    assert ok["tool_use_id"] == "t1" and [b["type"] for b in ok["content"]] == ["text", "image"]
    assert bad["is_error"] is True and "사진 번호" in bad["content"]

    pill = out["pills"][0]
    assert "views" not in pill
    assert len(pill["photos"]) == 1 and pill["photos"][0].startswith("data:image/jpeg;base64,")
    assert out["zooms"] == 1 and out["mode"] == "zoom"


def test_images_per_request_stay_within_limit():
    # 사진 6장이면 확대는 14번까지만 (6 + 14 = 20장)
    client = FakeClient()
    vision.extract_pill_features([(jpeg(300, 300), "image/jpeg")] * 6, client=client)
    assert "14번" in client.calls[0]["system"]


def test_falls_back_to_simple_mode_on_400():
    import anthropic
    import httpx

    class RejectTools(FakeClient):
        def create(self, **kw):
            if "tools" in kw:
                self.calls.append(kw)
                raise anthropic.BadRequestError(
                    "bad", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
            self.calls.append(kw)
            return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(RESULT))])

    client = RejectTools()
    out = vision.extract_pill_features([(jpeg(800, 600), "image/jpeg")], client=client)
    assert out["mode"] == "simple" and out["zooms"] == 0
    assert "tools" not in client.calls[-1] and "cache_control" not in client.calls[-1]
    assert "zoom" not in client.calls[-1]["system"]


def test_zoom_limit(monkeypatch):
    monkeypatch.setattr(vision, "MAX_ZOOMS", 0)
    client = FakeClient()
    vision.extract_pill_features([(jpeg(800, 600), "image/jpeg")], client=client)
    results = client.calls[1]["messages"][-1]["content"]
    assert all(r.get("is_error") for r in results)


def test_refusal():
    client = FakeClient()
    client.beta.messages.create = lambda **kw: NS(stop_reason="refusal", content=[])
    with pytest.raises(vision.VisionError):
        vision.extract_pill_features([(jpeg(100, 100), "image/jpeg")], client=client)


def test_bad_image():
    with pytest.raises(vision.VisionError):
        vision.prepare_image(b"not an image")


def test_crop_box_clamps_and_rejects_tiny():
    img = Image.new("RGB", (500, 400))
    assert vision.crop_box(img, 450, 350, 900, 900).size[0] <= 500
    assert vision.crop_box(img, 10, 10, 12, 12) is None
