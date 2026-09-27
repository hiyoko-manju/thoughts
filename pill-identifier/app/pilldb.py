"""식약처 의약품 낱알식별 정보를 담은 SQLite DB와 후보 검색."""

import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pills.db"

# 식약처 API 응답 필드 (소문자로 저장)
COLUMNS = [
    "item_seq", "item_name", "entp_name", "chart", "item_image",
    "print_front", "print_back", "drug_shape", "color_class1", "color_class2",
    "line_front", "line_back", "leng_long", "leng_short", "thick",
    "mark_code_front", "mark_code_back", "form_code_name", "class_name",
    "etc_otc_name", "edi_code",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pills (
    {", ".join(c + " TEXT" + (" PRIMARY KEY" if c == "item_seq" else "") for c in COLUMNS)},
    print_front_norm TEXT,
    print_back_norm TEXT
);
CREATE INDEX IF NOT EXISTS idx_pf ON pills(print_front_norm);
CREATE INDEX IF NOT EXISTS idx_pb ON pills(print_back_norm);
"""


# DB 각인 필드에 섞여 있는 설명 문구. 실제 각인이 아니므로 비교 전에 뺀다 (예: 'DLI분할선DLI').
ANNOTATIONS = ["십자분할선", "분할선", "각인없음", "마크", "없음"]


def normalize_imprint(s: str | None) -> str:
    """대소문자·공백·구두점 차이와 설명 문구를 없앤다."""
    if not s:
        return ""
    s = s.upper()
    for word in ANNOTATIONS:
        s = s.replace(word, "")
    return re.sub(r"[^0-9A-Z가-힣?]", "", s)


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> None:
    cols = COLUMNS + ["print_front_norm", "print_back_norm"]
    sql = f"INSERT OR REPLACE INTO pills ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    data = []
    for r in rows:
        r = {k.lower(): v for k, v in r.items()}
        data.append([r.get(c) for c in COLUMNS]
                    + [normalize_imprint(r.get("print_front")), normalize_imprint(r.get("print_back"))])
    conn.executemany(sql, data)
    conn.commit()


def renormalize(conn: sqlite3.Connection) -> None:
    """정규화 규칙이 바뀌었을 때 저장된 각인 비교값을 다시 계산한다."""
    rows = conn.execute("SELECT item_seq, print_front, print_back FROM pills").fetchall()
    conn.executemany("UPDATE pills SET print_front_norm = ?, print_back_norm = ? WHERE item_seq = ?",
                     [(normalize_imprint(r["print_front"]), normalize_imprint(r["print_back"]), r["item_seq"])
                      for r in rows])
    conn.commit()


def _imprint_similarity(observed: str, actual: str) -> float:
    """0~1. '?'는 아무 글자와 일치하는 것으로 본다."""
    if not observed or not actual:
        return 0.0
    if "?" in observed and len(observed) == len(actual):
        if all(o == "?" or o == a for o, a in zip(observed, actual)):
            return 0.95
    if observed == actual:
        return 1.0
    return SequenceMatcher(None, observed.replace("?", ""), actual).ratio()


def _imprint_score(front: str, back: str, row: sqlite3.Row) -> float:
    """관찰한 각인과 DB 각인의 유사도 (0~1).

    앞뒷면 구분이 불확실하므로 뒤바꿔서도 비교하고, 분할선 양쪽 글자를 앞/뒷면으로 나눠 읽은
    경우를 위해 두 면을 이어 붙여서도 비교한다.
    """
    pf, pb = row["print_front_norm"] or "", row["print_back_norm"] or ""

    def pair(a: str, b: str) -> float:
        scores = [s for s in (_imprint_similarity(front, a) if front else None,
                              _imprint_similarity(back, b) if back else None) if s is not None]
        return sum(scores) / len(scores) if scores else 0.0

    combined = 0.0
    if front and back:
        combined = max(_imprint_similarity(front + back, pf + pb), _imprint_similarity(front + back, pb + pf))
    return max(pair(pf, pb), pair(pb, pf), combined)


# 사진으로 구분이 어려운 이웃 값들: 불일치가 아니라 '근접'으로 본다.
SHAPE_NEAR = [{"타원형", "장방형"}, {"사각형", "마름모형"}, {"오각형", "육각형"}, {"육각형", "팔각형"}]
COLOR_NEAR = [{"분홍", "주황"}, {"분홍", "빨강"}, {"주황", "빨강"}, {"노랑", "주황"}, {"하양", "노랑"},
              {"하양", "회색"}, {"하양", "투명"}, {"연두", "초록"}, {"초록", "청록"}, {"청록", "파랑"},
              {"파랑", "남색"}, {"자주", "보라"}, {"자주", "분홍"}, {"갈색", "주황"}]


def _near(a: str, b: str, groups: list[set]) -> bool:
    return any(a in g and b in g for g in groups)


def _shape_ok(observed: str, actual: str | None) -> bool | str | None:
    if observed in ("불명", "") or not actual:
        return None
    if observed == actual:
        return True
    return "near" if _near(observed, actual, SHAPE_NEAR) else False


def _color_ok(p: dict, row: sqlite3.Row) -> bool | str | None:
    obs = {c for c in (p.get("color_primary"), p.get("color_secondary")) if c and c not in ("불명", "없음")}
    if not obs:
        return None
    actual = [c for c in (row["color_class1"], row["color_class2"]) if c]
    if any(o in a for o in obs for a in actual):
        return True
    if any(_near(o, part.strip(), COLOR_NEAR) for o in obs for a in actual for part in re.split(r"[,|/ ]", a)):
        return "near"
    return False


def _form_ok(observed: str, actual: str | None) -> bool | None:
    if observed in ("불명", "") or not actual:
        return None
    is_capsule = "캡슐" in actual
    return (observed != "정제") == is_capsule


# 점수: 각인이 압도적으로 중요하다. 모양·색·제형은 같은 각인 안에서 순서를 가르는 정도.
IMPRINT_WEIGHT = 100
FEATURE_WEIGHTS = {"모양": 10, "색상": 10, "제형": 5}
MAX_SCORE = IMPRINT_WEIGHT + sum(FEATURE_WEIGHTS.values())


def _score(imprint: float, checks: dict) -> int:
    total = IMPRINT_WEIGHT * imprint ** 2
    for key, w in FEATURE_WEIGHTS.items():
        v = checks[key]
        total += w if v is True else w / 2 if v == "near" else -w if v is False else 0
    return round(max(total, 0) / MAX_SCORE * 100)


def _prefilter_keys(front: str, back: str) -> set[str]:
    """DB에서 1차 후보를 좁힐 2글자 조각들. 첫 글자를 잘못 읽어도 걸리도록 앞뒤 조각을 모두 쓴다."""
    keys = set()
    for k in (front, back):
        for piece in (k[:2], k[-2:]):
            if len(piece) == 2 and "?" not in piece:
                keys.add(piece)
    return keys


def find_candidates(conn: sqlite3.Connection, pill: dict, limit: int = 5) -> list[dict]:
    front = normalize_imprint(pill.get("imprint_front"))
    back = normalize_imprint(pill.get("imprint_back"))
    has_imprint = bool((front + back).replace("?", ""))

    if has_imprint:
        # 각인의 앞 2글자로 1차 후보를 좁힌 뒤 유사도를 계산한다.
        keys = _prefilter_keys(front, back)
        if keys:
            where = " OR ".join("print_front_norm LIKE ? OR print_back_norm LIKE ?" for _ in keys)
            params = [f"%{k}%" for k in keys for _ in range(2)]
            rows = conn.execute(f"SELECT * FROM pills WHERE {where}", params).fetchall()
        else:
            rows = conn.execute("SELECT * FROM pills WHERE print_front_norm != '' OR print_back_norm != ''").fetchall()
    else:
        # 각인이 없거나 안 보이면 모양·색만으로 찾되, 결과는 참고용일 뿐이다.
        rows = conn.execute(
            "SELECT * FROM pills WHERE drug_shape = ? AND (color_class1 LIKE ? OR color_class2 LIKE ?)",
            [pill.get("shape"), f"%{pill.get('color_primary')}%", f"%{pill.get('color_primary')}%"],
        ).fetchall()

    results = []
    for row in rows:
        imprint = _imprint_score(front, back, row) if has_imprint else 0.0
        if has_imprint and imprint < 0.6:
            continue
        checks = {
            "각인": (True if imprint >= 0.999 else "partial") if has_imprint else None,
            "모양": _shape_ok(pill.get("shape", ""), row["drug_shape"]),
            "색상": _color_ok(pill, row),
            "제형": _form_ok(pill.get("form", ""), row["form_code_name"]),
        }
        results.append({
            "item_seq": row["item_seq"],
            "item_name": row["item_name"],
            "entp_name": row["entp_name"],
            "item_image": row["item_image"],
            "print_front": row["print_front"],
            "print_back": row["print_back"],
            "drug_shape": row["drug_shape"],
            "color": " / ".join(filter(None, [row["color_class1"], row["color_class2"]])),
            "line": " / ".join(filter(None, [row["line_front"], row["line_back"]])),
            "size": "×".join(filter(None, [row["leng_long"], row["leng_short"], row["thick"]])),
            "form_code_name": row["form_code_name"],
            "class_name": row["class_name"],
            "etc_otc_name": row["etc_otc_name"],
            "score": _score(imprint, checks),
            "checks": checks,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


def stats(conn: sqlite3.Connection) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM pills").fetchone()[0]
    demo = conn.execute("SELECT COUNT(*) FROM pills WHERE item_seq LIKE 'DEMO%'").fetchone()[0]
    return {"total": n, "demo_only": n > 0 and n == demo}
