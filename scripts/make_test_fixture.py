r"""테스트·로컬 UI 전용 **합성 fixture** 생성기 (2026-11-04)

    .\.venv\Scripts\python.exe scripts\make_test_fixture.py --dir <폴더>
    .\.venv\Scripts\python.exe scripts\make_test_fixture.py --dir <폴더> --scenarios

무엇을 만드나 — 전부 **가짜 데이터**다. 운영 파일을 복사하지 않는다.
    brands.json           [테스트] 브랜드 1개(시트 ID 는 존재하지 않는 값)
    accounts.json         [테스트] 계정 5개 — 시나리오별로 이름이 다르다
    out/tabs_*.json       기준랜딩 탭 목록 **캐시** (이게 있으면 구글 시트를 읽지 않는다)
    out/catalog_*.json    매체·결핍 목록 캐시 (같은 이유)
    queue/agents/*.json   '살아 있는' 가짜 PC 1대 (실제 Agent 가 아니다)
    --scenarios 를 주면 queue/{pending,running,held} 에 가짜 작업까지 만든다

왜 필요한가
    ① AppTest(화면 테스트)가 기준시트를 못 읽으면 `st.stop()` 으로 끝나 화면
       아래쪽을 검증할 수 없었다(2026-11-04 확인). 캐시만 있으면 **오프라인**으로
       끝까지 그려진다.
    ② 로컬에서 UI 를 눌러 볼 때 운영 계정·운영 큐가 절대 보이지 않게 한다.

★절대 하지 않는 것 — 구글/네이버/Supabase 통신, 운영 파일 읽기·쓰기.
★`catalog.load_tabs/load` 는 캐시가 있으면 시트를 읽지 않는다(TTL 없음) —
  이 fixture 가 성립하는 근거다.
★여기서 만든 계정·브랜드는 이름에 **[테스트]** 가 붙어 운영 데이터와 구분된다.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

#  ★브랜드 id 는 **내장 기본값과 같은 id** 를 쓴다(2026-11-04).
#    `v2/brands.py` 는 `DEFAULTS`(리퓨어리·닥터누센트)를 항상 깔고 파일 내용을
#    그 위에 덮는다. 새 id 를 추가하면 목록 **맨 앞이 리퓨어리** 가 되어 화면이
#    "기준시트 ID 를 알 수 없습니다" 로 멈춘다(실측 2026-11-04).
#    같은 id 를 덮어써야 목록에 테스트 브랜드만 남는다.
BRAND_ID = "repurely"
BRAND_LABEL = "[테스트] 가짜브랜드1(리퓨어리 자리)"
BRAND2_ID = "doctor_nuscent"
BRAND2_LABEL = "[테스트] 가짜브랜드2(닥터누센트 자리)"
TABS = ["[테스트] 정상계정 기준랜딩", "[테스트] 대기배치 기준랜딩",
        "[테스트] 실행중 기준랜딩", "[테스트] 보류500 기준랜딩",
        "[테스트] 로그인없음 기준랜딩"]
#  (세션폴더 id, 라벨, login_id, blog_id, 탭)
ACCOUNTS = [
    ("t_normal", "[테스트] 정상계정", "testnormal", "testblog_normal", TABS[0]),
    ("t_waiting", "[테스트] 대기배치", "testwaiting@naver.com",
     "testblog_waiting", TABS[1]),
    ("t_running", "[테스트] 실행중", "testrunning", "testblog_running", TABS[2]),
    ("t_held500", "[테스트] 보류500건", "testheld500", "testblog_held", TABS[3]),
    ("t_noid", "[테스트] login_id없음", "", "testblog_noid", TABS[4]),
]
MEDIA = ["카모", "메타", "틱톡"]
DEFS = ["흑자 / 머니(연습)", "피부톤 / 잡티(연습)", "탈모 / 두피(연습)"]
DEVICE = "PC-TEST-FAKE"


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9가-힣]+", "_", (text or "").strip()).strip("_") \
        or "default"


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def build(root: Path, scenarios: bool = False) -> dict:
    root.mkdir(parents=True, exist_ok=True)

    def brand_row(bid: str, label: str) -> dict:
        return {
            "id": bid, "label": label,
            #  ★존재하지 않는 시트 ID — 실수로 통신하면 즉시 실패한다
            #    (조용히 운영 시트를 읽는 일이 생길 수 없다)
            "reference_sheet_id": "TEST_FIXTURE_NO_SUCH_SHEET",
            "reference_sheet_label": f"{label} 기준시트(가짜)",
            "reference_tab": TABS[0],
            "utm_sheet_id": "TEST_FIXTURE_NO_SUCH_SHEET",
            "utm_sheet_label": f"{label} UTM 빌더(가짜)",
            "utm_tab_pattern": "{media} 블로그 랜딩 UTM 빌더",
            "utm_media_tabs": {}, "headers": {},
            "enabled": True, "ready": True, "status_note": "",
            "note": "테스트 전용 가짜 브랜드 — 운영 데이터가 아니다",
        }

    write(root / "brands.json", {"brands": [brand_row(BRAND_ID, BRAND_LABEL),
                                            brand_row(BRAND2_ID, BRAND2_LABEL)]})

    write(root / "accounts.json", {"accounts": [
        {"id": aid, "label": label, "login_id": lid, "blog_id": bid,
         "ref_tab": tab, "brand": BRAND_ID, "media": "", "enabled": True,
         "note": "테스트 전용 가짜 계정"}
        for aid, label, lid, bid, tab in ACCOUNTS]})

    #  기준랜딩 탭 목록 · 매체/결핍 카탈로그 **캐시** — 브랜드마다 만든다.
    #    ★이 캐시가 있으면 `catalog.load_tabs/load` 가 구글 시트를 읽지 않는다
    #      (TTL 없음) → 오프라인으로 화면이 끝까지 그려진다.
    for bid, blabel in ((BRAND_ID, BRAND_LABEL), (BRAND2_ID, BRAND2_LABEL)):
        write(root / "out" / f"tabs_{_slug(bid)}.json",
              {"brand": bid, "brand_label": blabel, "tabs": TABS,
               "cached_at": datetime.now().isoformat(timespec="seconds"),
               "from_cache": False})
        for tab in TABS:
            items = {m: [{"row": 10 + i, "deficiency": d,
                          "검수용": True, "실전용": True,
                          "제품URL": {"검수용": "https://example.invalid/test",
                                      "실전용": "https://example.invalid/test"}}
                         for i, d in enumerate(DEFS)] for m in MEDIA}
            write(root / "out" / f"catalog_{_slug(bid)}_{_slug(tab)}.json",
                  {"tab": tab, "brand": bid, "brand_label": blabel,
                   "reference_sheet": "TEST_FIXTURE_NO_SUCH_SHEET",
                   "cached_at": datetime.now().isoformat(timespec="seconds"),
                   "media": list(items), "items": items, "from_cache": False})

    #  '살아 있는' 가짜 PC — 실제 Agent 가 아니므로 작업을 가져가지 않는다
    write(root / "queue" / "agents" / f"{DEVICE}.json",
          {"agent": DEVICE, "host": "테스트PC(가짜)", "label": "테스트PC(가짜)",
           "state": "idle", "version": "1.3.0",
           "at": datetime.now().isoformat(timespec="seconds")})

    made = {"brands": 2, "accounts": len(ACCOUNTS), "tabs": len(TABS),
            "catalogs": len(TABS) * 2, "agents": 1, "jobs": 0}
    if scenarios:
        made["jobs"] = _jobs(root)
    return made


def _job(root: Path, state: str, no: int, acc, lid: str, title: str,
         minutes: int = 0, kind: str = "run", extra=None,
         device: str = DEVICE) -> None:
    at = (datetime.now() - timedelta(minutes=minutes)).isoformat(
        timespec="seconds")
    jid = f"TEST_{state}_{no:04d}"
    write(root / "queue" / state / f"{jid}.json", {
        "id": jid, "kind": kind, "brand": BRAND_ID,
        "title": f"[테스트] {title}",
        "status": "done" if state == "finished" else state,
        "created_at": at, "created_by": "fixture", "target_agent": device,
        "agent": device if state in ("running", "finished") else "",
        "job": {"flow": "production", "brand": BRAND_ID, "account": acc,
                "login_id": lid, "media": MEDIA[0], "deficiency": DEFS[0],
                "count": 10, "extra": list(extra or [])},
        "total": 10, "made": 3 if state == "running" else 0,
        "published": [], "error": "", "command": "",
        "started_at": at if state in ("running", "finished") else "",
        "finished_at": "", "exit_code": None,
    })


def _jobs(root: Path) -> int:
    """시나리오용 가짜 작업 — **로컬 큐 파일**이다. 운영 큐와 무관하다."""
    for state in ("pending", "running", "held", "finished", "logs", "cancel",
                  "plans", "pairings"):
        (root / "queue" / state).mkdir(parents=True, exist_ok=True)
    n = 0
    #  ② 대기 배치가 있는 계정
    #    ★대기(pending) 작업도 **다른 PC** 에 둔다 — 같은 PC 에 대기·실행 작업이
    #      하나라도 있으면 기존 중복실행 가드(`running_job`)가 모든 계정의
    #      [Dry-run]·[실전 실행] 을 먼저 막아, 정작 확인하려는
    #      '보류 중 계정의 신규 발행 차단' 문구를 볼 수 없다(실측 2026-11-04).
    _job(root, "pending", 1, "t_waiting", "testwaiting@naver.com",
         "대기 중인 10건 배치(다른 PC)", minutes=3, device="PC-TEST-OTHER")
    n += 1
    #  ③ 실행 중인 배치가 있는 계정
    #    ★다른 PC(device)에 올려 둔다 — 같은 PC 에 '도는 작업' 이 있으면 화면의
    #      기존 가드("이 PC 에서 다른 작업이 도는 중입니다")가 먼저 걸려
    #      보류·차단 화면을 눌러 볼 수 없다(실측 2026-11-04).
    _job(root, "running", 1, "t_running", "testrunning",
         "실행 중인 10건 배치(3건 발행됨 · 다른 PC)", minutes=5,
         device="PC-TEST-OTHER")
    n += 1
    #  ④ 보류 500건 초과 계정 — 조회 창 문제 재현용
    for i in range(1, 512):
        _job(root, "held", i, "t_held500", "testheld500",
             f"보류된 배치 {i}", minutes=600 + i)
        n += 1
    #  ⑥ 일부만 보류된 상황 — 같은 계정에 held + pending 이 섞인 상태
    _job(root, "held", 900, "t_noid", "", "일부만 보류된 배치(보류됨)",
         minutes=20)
    _job(root, "pending", 900, "t_noid", "", "일부만 보류된 배치(남은 것·다른 PC)",
         minutes=19, device="PC-TEST-OTHER")
    n += 2
    #  ★'로그아웃 작업 대기' 는 **미리 만들어 두지 않는다.**
    #    같은 PC 에 대기 작업이 있으면 기존 가드가 먼저 걸려 다른 화면을 못 본다.
    #    화면에서 [선택 계정 로그아웃] 을 누르면 그 상태가 **실제로** 만들어진다.
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="테스트 전용 합성 fixture 생성")
    ap.add_argument("--dir", required=True, help="만들 폴더(BLOG_DATA_DIR 로 쓴다)")
    ap.add_argument("--scenarios", action="store_true",
                    help="가짜 작업(대기·실행중·보류 500건 초과 등)까지 만든다")
    args = ap.parse_args()
    root = Path(args.dir).resolve()
    made = build(root, scenarios=args.scenarios)
    print(f"[fixture] {root}")
    for k, v in made.items():
        print(f"  {k:10s} {v}")
    print("  ★전부 가짜 데이터입니다(이름에 [테스트] 가 붙습니다). "
          "운영 파일을 복사하지 않았습니다.")
    print(f"  쓰는 법: BLOG_DATA_DIR={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
