import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, pilldb

DEMO = json.loads((Path(__file__).resolve().parent.parent / "data" / "demo_pills.json").read_text(encoding="utf-8"))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "pills.db"
    monkeypatch.setattr(pilldb, "DB_PATH", path)
    c = pilldb.connect(path)
    pilldb.upsert(c, DEMO)
    yield c
    c.close()


def pill(**kw):
    base = dict(label="x", count=1, form="정제", shape="원형", color_primary="하양", color_secondary="없음",
                imprint_front="", imprint_back="", imprint_confidence="높음", has_mark=False,
                mark_description="", score_line="없음", package_text="", notes="")
    return {**base, **kw}


# 실제 DB 표기 방식을 흉내 낸 행 (사용자 시험에서 나온 사례)
REALISTIC = [
    {"ITEM_SEQ": "T1", "ITEM_NAME": "라식스정", "PRINT_FRONT": "DLI분할선DLI", "PRINT_BACK": "-",
     "DRUG_SHAPE": "원형", "COLOR_CLASS1": "하양", "FORM_CODE_NAME": "나정"},
    {"ITEM_SEQ": "T2", "ITEM_NAME": "울트라셋세미정", "PRINT_FRONT": "JANSSEN", "PRINT_BACK": "S/M",
     "DRUG_SHAPE": "타원형", "COLOR_CLASS1": "노랑", "FORM_CODE_NAME": "필름코팅정"},
    {"ITEM_SEQ": "T3", "ITEM_NAME": "스트롱셋세미정", "PRINT_FRONT": "SS", "PRINT_BACK": "S/M",
     "DRUG_SHAPE": "장방형", "COLOR_CLASS1": "노랑", "FORM_CODE_NAME": "필름코팅정"},
    {"ITEM_SEQ": "T4", "ITEM_NAME": "세타돌세미정", "PRINT_FRONT": "DS", "PRINT_BACK": "S/M",
     "DRUG_SHAPE": "장방형", "COLOR_CLASS1": "노랑", "FORM_CODE_NAME": "필름코팅정"},
    {"ITEM_SEQ": "T5", "ITEM_NAME": "리나제틴정", "PRINT_FRONT": "IDL", "PRINT_BACK": "5",
     "DRUG_SHAPE": "원형", "COLOR_CLASS1": "분홍", "FORM_CODE_NAME": "필름코팅정"},
]


@pytest.fixture
def real(conn):
    pilldb.upsert(conn, REALISTIC)
    return conn


def test_normalize():
    assert pilldb.normalize_imprint(" d-m 5 ") == "DM5"
    assert pilldb.normalize_imprint("마크 AB") == "AB"
    assert pilldb.normalize_imprint("DLI분할선DLI") == "DLIDLI"
    assert pilldb.normalize_imprint("-") == ""


def test_scoreline_split_imprint_read_as_one_face(real):
    top = pilldb.find_candidates(real, pill(imprint_front="DLI DLI"))
    assert top[0]["item_seq"] == "T1" and top[0]["checks"]["각인"] is True


def test_scoreline_split_imprint_read_as_two_faces(real):
    top = pilldb.find_candidates(real, pill(imprint_front="DLI", imprint_back="DLI"))
    assert top[0]["item_seq"] == "T1" and top[0]["checks"]["각인"] is True


def test_exact_imprint_beats_partial_with_better_shape(real):
    # 사진에서 타원형을 장방형으로 봐도 각인이 정확히 맞는 약이 1등이어야 한다.
    top = pilldb.find_candidates(real, pill(shape="장방형", color_primary="노랑",
                                            imprint_front="JANSSEN", imprint_back="S/M"))
    assert top[0]["item_seq"] == "T2"
    assert top[0]["checks"]["모양"] == "near"
    assert top[0]["score"] - top[1]["score"] > 15


def test_near_color(real):
    top = pilldb.find_candidates(real, pill(color_primary="주황", imprint_front="IDL", imprint_back="5"))
    assert top[0]["checks"]["색상"] == "near"


def test_misread_first_letter_still_found(real):
    seqs = [c["item_seq"] for c in pilldb.find_candidates(real, pill(imprint_front="0LIDLI"))]
    assert "T1" in seqs


def test_renormalize(real):
    real.execute("UPDATE pills SET print_front_norm = 'STALE'")
    pilldb.renormalize(real)
    row = real.execute("SELECT print_front_norm FROM pills WHERE item_seq = 'T1'").fetchone()
    assert row[0] == "DLIDLI"


def test_exact_imprint_distinguishes_strengths(conn):
    top = pilldb.find_candidates(conn, pill(imprint_front="DM", imprint_back="10"))
    assert top[0]["item_seq"] == "DEMO0002"
    assert top[0]["checks"]["각인"] is True


def test_front_back_swapped(conn):
    top = pilldb.find_candidates(conn, pill(imprint_front="5", imprint_back="dm"))
    assert top[0]["item_seq"] == "DEMO0001"


def test_uncertain_char(conn):
    top = pilldb.find_candidates(conn, pill(form="경질캡슐", shape="장방형", color_primary="파랑",
                                            imprint_front="TC", imprint_back="2?0"))
    assert top[0]["item_seq"] == "DEMO0004"
    assert top[0]["checks"]["각인"] == "partial"


def test_unrelated_imprint_returns_nothing(conn):
    assert pilldb.find_candidates(conn, pill(imprint_front="ZZ9")) == []


def test_no_imprint_falls_back_to_shape_color(conn):
    seqs = {c["item_seq"] for c in pilldb.find_candidates(conn, pill(color_primary="분홍"))}
    assert seqs == {"DEMO0005"}


def test_identify_endpoint(conn, monkeypatch):
    fake = {"pills": [pill(imprint_front="EX", imprint_back="SR", shape="타원형", color_primary="노랑")],
            "photo_issues": []}
    monkeypatch.setattr(main, "extract_pill_features", lambda images, single=False: fake)
    client = TestClient(main.app)
    r = client.post("/api/identify", files=[("images", ("a.jpg", b"\xff\xd8fake", "image/jpeg"))])
    assert r.status_code == 200
    body = r.json()
    assert body["pills"][0]["candidates"][0]["item_seq"] == "DEMO0003"
    assert body["db"]["demo_only"] is True


def test_rejects_non_image(conn):
    client = TestClient(main.app)
    r = client.post("/api/identify", files=[("images", ("a.txt", b"hi", "text/plain"))])
    assert r.status_code == 400


def test_manual_search_endpoint(conn):
    client = TestClient(main.app)
    r = client.post("/api/search", json={"imprint_front": "dm", "imprint_back": "5", "shape": "원형"})
    assert r.status_code == 200
    assert r.json()["candidates"][0]["item_seq"] == "DEMO0001"


def test_retake_passes_single_flag(conn, monkeypatch):
    seen = {}

    def fake(images, single=False):
        seen["single"] = single
        return {"pills": [], "photo_issues": []}

    monkeypatch.setattr(main, "extract_pill_features", fake)
    client = TestClient(main.app)
    client.post("/api/identify?single=true", files=[("images", ("a.jpg", b"x", "image/jpeg"))])
    assert seen["single"] is True


def test_password_lock(conn, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "비번123")
    monkeypatch.setattr(main, "_failures", __import__("collections").defaultdict(list))
    client = TestClient(main.app)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", auth=("x", "wrong")).status_code == 401
    ok = client.get("/api/status", headers={"Authorization": "Basic " + __import__("base64").b64encode("x:비번123".encode()).decode()})
    assert ok.status_code == 200
    for _ in range(10):
        client.get("/api/status", auth=("x", "wrong"))
    assert client.get("/api/status", auth=("x", "wrong")).status_code == 429
