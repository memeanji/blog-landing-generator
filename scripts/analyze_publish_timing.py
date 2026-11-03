r"""발행 1건당 단계별 소요시간 측정 — **기존 운영 로그만 읽는다**(브라우저 안 켬).

    .\.venv\Scripts\python.exe scripts\analyze_publish_timing.py
    .\.venv\Scripts\python.exe scripts\analyze_publish_timing.py --glob "out/v2_prod_20260825_*.log"

왜 이렇게 측정하는가
    `v2/logger.py` 는 모든 줄에 `HH:MM:SS` 를 붙인다. 발행 로그에는 단계마다 고정된
    문구가 찍히므로, 실제 운영 실행의 단계별 시간을 **코드를 건드리지 않고** 뽑을 수 있다.
    해상도는 1초다 — 1초 미만 단계는 0 으로 보이니 '구간 합계'로 판단해야 한다.
"""
from __future__ import annotations

import argparse
import re
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) (.*)$")

try:                                       # cp949 콘솔에서 한글이 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                          # noqa: BLE001
    pass


def _rows(path: Path) -> list[tuple[datetime, str]]:
    """(시각, 본문) 목록. 자정을 넘기면 날짜를 하루 올린다."""
    out: list[tuple[datetime, str]] = []
    base, prev = datetime(2000, 1, 1), None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.match(line)
        if not m:
            continue
        t = base.replace(hour=int(m[1]), minute=int(m[2]), second=int(m[3]))
        if prev is not None and t < prev:
            base += timedelta(days=1)
            t += timedelta(days=1)
        prev = t
        out.append((t, m[4]))
    return out


# 단계 경계 — 문구가 바뀌면 여기만 고친다(로그 문구를 코드가 긁는 유일한 자리).
START = re.compile(r"^════ \[(\d+)/(\d+)\] 실전용")
OPENED = re.compile(r"^\[(\d+)/(\d+)\] (?:검수용 수정 화면 · 모바일 미리보기 진입 완료"
                    r"|새 글 · 모바일 미리보기 준비 완료)")
SRC_BEG = re.compile(r"^\[(\d+)/(\d+)\] 실전용 참고글 준비 시작")
SRC_END = re.compile(r"^\[(\d+)/(\d+)\] 실전용 참고글 준비 완료")
TITLE = re.compile(r"^\[(\d+)/(\d+)\] 제목 복사 완료")
BODY = re.compile(r"^\[(\d+)/(\d+)\] 본문 복사 완료")
LINK = re.compile(r"^\[(\d+)/(\d+)\] 제품 링크 (?:생성|교체) 완료")
READY = re.compile(r"^\[(\d+)/(\d+)\] 작성 완료\(READY")
PUB_BEG = re.compile(r"^════ \[(\d+)/(\d+)\] 발행 — (\d+)초 대기 후")
PUB_END = re.compile(r"^\[(\d+)/(\d+)\] 댓글 허용 OFF 확인 · 발행 완료")
SHEET = re.compile(r"^\[(\d+)/(\d+)\] 시트 기록 완료")
# 세부(진입 안쪽) — 글 번호가 안 찍히는 줄들
POPUP_SEEN = re.compile(r"^\[새글\] '작성 중인 글' 팝업 감지")
POPUP_GONE = re.compile(r"^\[새글\] 팝업 닫힘 확인")
POPUP_NONE = re.compile(r"^\[새글\] 작성 중인 글 팝업 없음")
EDIT_GO = re.compile(r"^\[수정\] 진입:")
EDIT_OK = re.compile(r"^\[수정\] 진입 확인")

STEP_RX = ((START, "start"), (OPENED, "opened"), (SRC_BEG, "src_beg"),
           (SRC_END, "src_end"), (TITLE, "title"), (BODY, "body"),
           (LINK, "link"), (READY, "ready"), (PUB_END, "pub_end"),
           (SHEET, "sheet"))


def parse_run(path: Path) -> dict | None:
    rows = _rows(path)
    if not rows:
        return None
    posts: dict[int, dict] = {}
    detail: dict[str, list[float]] = {}
    open_beg: datetime | None = None

    for t, msg in rows:
        hit = False
        for rx, key in STEP_RX:
            m = rx.match(msg)
            if m:
                posts.setdefault(int(m[1]), {})[key] = t
                hit = True
                break
        if hit:
            continue
        m = PUB_BEG.match(msg)
        if m:
            p = posts.setdefault(int(m[1]), {})
            p["pub_beg"], p["wait_sec"] = t, float(m[3])
            continue
        if POPUP_SEEN.match(msg) or EDIT_GO.match(msg):
            open_beg = t
        elif (POPUP_GONE.match(msg) or EDIT_OK.match(msg)) and open_beg is not None:
            detail.setdefault("팝업 닫기 / 수정화면 진입 확인",
                              []).append((t - open_beg).total_seconds())
            open_beg = None
        elif POPUP_NONE.match(msg):
            detail.setdefault("'팝업 없음' 판정(고정 폴링 소진)", []).append(0.0)

    if not posts:
        return None
    steps: dict[str, list[float]] = {}

    def add(name: str, a, b) -> None:
        if a and b and b >= a:
            steps.setdefault(name, []).append((b - a).total_seconds())

    for no in sorted(posts):
        p = posts[no]
        add("(1) 글 화면 진입 + 모바일 전환", p.get("start"), p.get("opened"))
        if p.get("src_beg") and p.get("src_end"):
            add("(2a) 참고글 탭 준비(첫 글만)", p["src_beg"], p["src_end"])
            add("(2b) 제목 입력", p["src_end"], p.get("title"))
        else:
            add("(2) 제목 입력(참고글 탭 재사용)", p.get("opened"), p.get("title"))
        add("(3) 본문 복사 + 붙여넣기", p.get("title"), p.get("body"))
        add("(4) 제품링크 카드 생성", p.get("body"), p.get("link"))
        add("(5) 정렬 + 하단검증 + 스크린샷", p.get("link"), p.get("ready"))
        add("[작성 합계 (1)~(5)]", p.get("start"), p.get("ready"))
        if p.get("wait_sec") is not None:
            steps.setdefault("(6) 발행 전 고정 대기(랜덤)", []).append(p["wait_sec"])
            if p.get("pub_beg") and p.get("pub_end"):
                real = (p["pub_end"] - p["pub_beg"]).total_seconds() - p["wait_sec"]
                if real >= 0:
                    steps.setdefault("(7) 발행 클릭 -> 발행 완료", []).append(real)
        add("(8) 구글시트 기록", p.get("pub_end"), p.get("sheet"))

    ordered = [p for _, p in sorted(posts.items())]
    t0 = ordered[0].get("start")
    tN = next((p.get("sheet") or p.get("pub_end") or p.get("ready")
               for p in reversed(ordered)), None)
    return {"file": path.name, "posts": len(posts), "steps": steps, "detail": detail,
            "published": sum(1 for p in posts.values() if p.get("pub_end")),
            "span": (tN - t0).total_seconds() if t0 and tN else None}


def fmt(vals: list[float]) -> str:
    return (f"{st.mean(vals):8.1f}{st.median(vals):9.1f}{max(vals):8.1f}"
            f"{len(vals):6d}")


ORDER = ["(1) 글 화면 진입 + 모바일 전환", "(2a) 참고글 탭 준비(첫 글만)",
         "(2b) 제목 입력", "(2) 제목 입력(참고글 탭 재사용)", "(3) 본문 복사 + 붙여넣기",
         "(4) 제품링크 카드 생성", "(5) 정렬 + 하단검증 + 스크린샷",
         "[작성 합계 (1)~(5)]", "(6) 발행 전 고정 대기(랜덤)",
         "(7) 발행 클릭 -> 발행 완료", "(8) 구글시트 기록"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="out/v2_prod_*.log")
    ap.add_argument("--min-posts", type=int, default=5,
                    help="이 건수 이상 발행한 실행만 집계(짧은 시험 실행 제외)")
    a = ap.parse_args(argv)

    runs = [r for r in (parse_run(p) for p in sorted(ROOT.glob(a.glob))) if r]
    runs = [r for r in runs if r["published"] >= a.min_posts]
    if not runs:
        print("집계할 실행이 없습니다.")
        return 1

    print("=" * 76)
    print(f" 발행 단계별 소요시간 — 운영 로그 {len(runs)}개 실행 (단위: 초, 해상도 1초)")
    print("=" * 76)
    for r in runs:
        print(f"  - {r['file']}  작성 {r['posts']}건 / 발행 {r['published']}건"
              + (f" / 전체 {r['span']:.0f}초" if r["span"] else ""))

    agg: dict[str, list[float]] = {}
    for r in runs:
        for k, v in r["steps"].items():
            agg.setdefault(k, []).extend(v)

    print("")
    print(f"{'단계':<32}{'평균':>8}{'중앙값':>9}{'최대':>8}{'표본':>6}")
    print("-" * 76)
    for k in ORDER:
        if k in agg:
            print(f"{k:<32}{fmt(agg[k])}")
    for k in sorted(set(agg) - set(ORDER)):
        print(f"{k:<32}{fmt(agg[k])}")

    det: dict[str, list[float]] = {}
    for r in runs:
        for k, v in r["detail"].items():
            det.setdefault(k, []).extend(v)
    if det:
        print("")
        print("세부(진입 안쪽)")
        for k, v in det.items():
            print(f"{'  ' + k:<32}{fmt(v)}")

    # 체감 처리율 — 실행 전체 구간 / 발행 건수.
    #   ★단계 합으로 1건을 재면 안 된다: 탭 5~6개를 몰아 작성한 뒤 그 배치를 발행하므로
    #     한 건의 start->시트기록 구간에는 **같은 배치의 다른 건**이 섞여 있다.
    rate = [r["span"] / r["published"] for r in runs
            if r.get("span") and r["published"]]
    if rate:
        print("")
        print(f"체감 1건당 = {st.mean(rate):.1f}초(평균) · {st.median(rate):.1f}초(중앙값) "
              f"· 최대 {max(rate):.1f}초   [실행 전체구간 / 발행건수, 표본 {len(rate)}회]")
        print(f"10건 배치 1회 = 약 {st.mean(rate) * 10 / 60:.1f}분 "
              f"(로그인·시트조회 포함)")
    sums = {k: st.mean(v) for k, v in agg.items() if k in ORDER}
    if sums:
        print("")
        print("단계 평균의 합(순수 1건 처리) = "
              f"{sum(v for k, v in sums.items() if not k.startswith('[')):.1f}초"
              " — (2a) 첫 글 전용 단계 포함")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
