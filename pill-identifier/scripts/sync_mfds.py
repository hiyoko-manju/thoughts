"""식약처 '의약품 낱알식별 정보'를 로컬 SQLite(data/pills.db)로 가져온다.

방법 1) 공공데이터포털 Open API (활용신청 후 받은 일반 인증키 필요)
    DATA_GO_KR_KEY=발급키 python scripts/sync_mfds.py api

방법 2) 공공데이터포털/의약품안전나라에서 내려받은 CSV 파일
    python scripts/sync_mfds.py csv 낱알식별.csv

방법 3) 화면 확인용 데모 데이터 (가상의 약 — 실제 식별에 쓰면 안 됨)
    python scripts/sync_mfds.py demo
"""

import csv
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import pilldb  # noqa: E402

API_URL = "https://apis.data.go.kr/1471000/MdcinGrnIdntfcInfoService03/getMdcinGrnIdntfcInfoList03"
PAGE_SIZE = 100

# CSV 파일 데이터의 한글 컬럼명 → API 필드명
KOREAN_HEADERS = {
    "품목일련번호": "item_seq", "품목명": "item_name", "업소명": "entp_name",
    "성상": "chart", "큰제품이미지": "item_image", "표시앞": "print_front",
    "표시뒤": "print_back", "의약품제형": "drug_shape", "색상앞": "color_class1",
    "색상뒤": "color_class2", "분할선앞": "line_front", "분할선뒤": "line_back",
    "크기장축": "leng_long", "크기단축": "leng_short", "크기두께": "thick",
    "마크코드앞": "mark_code_front", "마크코드뒤": "mark_code_back",
    "제형코드명": "form_code_name", "분류명": "class_name",
    "전문일반구분": "etc_otc_name", "보험코드": "edi_code",
}


def fetch_page(client: httpx.Client, key: str, page: int) -> tuple[list[dict], int]:
    for attempt in range(4):
        try:
            r = client.get(API_URL, params={"serviceKey": key, "pageNo": page,
                                            "numOfRows": PAGE_SIZE, "type": "json"}, timeout=30)
            r.raise_for_status()
            body = r.json()["body"]
            items = body.get("items") or []
            if isinstance(items, dict):  # 일부 응답은 {"item": [...]} 형태
                items = items.get("item") or []
            if isinstance(items, dict):
                items = [items]
            return items, int(body.get("totalCount", 0))
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as e:
            wait = 2 ** (attempt + 1)
            print(f"  page {page} 실패 ({e}); {wait}s 후 재시도", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"page {page}를 가져오지 못했습니다.")


def sync_api() -> None:
    key = os.environ.get("DATA_GO_KR_KEY")
    if not key:
        raise SystemExit("DATA_GO_KR_KEY 환경변수에 공공데이터포털 인증키를 넣어 주세요.")
    with pilldb.connect() as conn, httpx.Client() as client:
        items, total = fetch_page(client, key, 1)
        pilldb.upsert(conn, items)
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        print(f"총 {total}건, {pages}페이지")
        for page in range(2, pages + 1):
            items, _ = fetch_page(client, key, page)
            pilldb.upsert(conn, items)
            if page % 20 == 0:
                print(f"  {page}/{pages}")
        conn.execute("DELETE FROM pills WHERE item_seq LIKE 'DEMO%'")
        conn.commit()
        print("완료:", pilldb.stats(conn))


def sync_csv(path: str) -> None:
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit("CSV 인코딩을 읽을 수 없습니다 (UTF-8 또는 CP949).")
    rows = [{KOREAN_HEADERS.get(k.strip(), k.strip().lower()): v for k, v in r.items()} for r in rows]
    with pilldb.connect() as conn:
        pilldb.upsert(conn, rows)
        conn.execute("DELETE FROM pills WHERE item_seq LIKE 'DEMO%'")
        conn.commit()
        print("완료:", pilldb.stats(conn))


def sync_demo() -> None:
    demo = Path(__file__).resolve().parent.parent / "data" / "demo_pills.json"
    with pilldb.connect() as conn:
        pilldb.upsert(conn, json.loads(demo.read_text(encoding="utf-8")))
        print("데모 데이터 적재:", pilldb.stats(conn))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "api":
        sync_api()
    elif cmd == "csv" and len(sys.argv) > 2:
        sync_csv(sys.argv[2])
    elif cmd == "demo":
        sync_demo()
    else:
        print(__doc__)
