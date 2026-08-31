"""실행 진입점.

  python main.py                                   → 기존 Tkinter GUI
  python main.py --list                            → 시트의 매체/결핍 조합과 검수용 랜딩 준비 현황
  python main.py --media GFA --deficiency "팔자 / 머니"
        → 시트에서 검수용 참고 URL 조회 → 랜딩 원문 그대로 → 네이버 새 글에 제목/본문 입력
  옵션
    --paste     창 두 개로 구간 복사→붙여넣기(서식·이미지 유지). 손으로 하던 방식과 동일
    --dry-run   시트 조회 + 원문 추출까지만. 네이버에 접속하지 않음
    --rewrite   원문 복제 대신 의미·구조만 참고해 새로 작성
    --title     글 제목(생략 시 랜딩 제목)
    --out       추출한 원문을 텍스트 파일로 저장
    --url       [디버그] 시트를 건너뛰고 URL 직접 지정

★ 어떤 모드에서도 발행/예약/저장 버튼은 누르지 않는다. 기존 글도 건드리지 않는다.
★ 검수용 칸이 비었거나 URL이 아닌 값(계정명 등)이면 **다른 URL로 대체하지 않고 중단**한다.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from app.config import load_settings
from app.services.draft import compose, copy_source, verify
from app.services.reference import ReferenceExtractor

BASE_DIR = Path(__file__).resolve().parent


# 진단/실행 로그를 파일로도 남긴다(콘솔 출력을 복사해 옮기지 않아도 되도록).
LOG_FILE: Path | None = None


def _console_log(message: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    print(line, flush=True)
    if LOG_FILE is not None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001  (로그 실패가 본 작업을 막지 않게)
            pass


def _console_wait(message: str) -> None:
    """콘솔 모드의 '계속' 입력 — 로그인/2차 인증을 사람이 끝낼 때까지 기다린다."""
    print(f"\n>>> {message}")
    try:
        input(">>> 완료했으면 Enter 를 누르세요... ")
    except EOFError:
        raise RuntimeError("대화형 입력이 불가능한 환경입니다. 콘솔에서 직접 실행하세요.")


def _new_sheets_client(settings):
    from app.services.sheets import SheetsClient

    return SheetsClient(
        credential_path=settings.google_service_account_json,
        reference_spreadsheet_id=settings.reference_spreadsheet_id,
        enabled=settings.enable_external_actions,
        log=_console_log,
        readonly=True,          # 이 프로그램은 시트를 읽기만 한다
    )


def show_list() -> int:
    from app.services.sheets import is_landing_url

    settings = load_settings()
    rows = _new_sheets_client(settings).list_combinations_raw()
    print(f"\n{'매체':<8}{'결핍':<16}{'검수용':<10}{'실전용'}")
    print("-" * 48)
    ready = 0
    for media, deficiency, review, prod in rows:
        ok = is_landing_url(review)
        ready += 1 if ok else 0
        mark = "랜딩 있음" if ok else ("준비중" if not review.strip() else f"준비중({review[:8]})")
        pmark = "있음" if is_landing_url(prod) else "-"
        print(f"{media:<8}{deficiency:<16}{mark:<10}{pmark}")
    print("-" * 48)
    print(f"총 {len(rows)}조합 · 검수용 랜딩 준비 완료 {ready}건\n")
    return 0


def _resolve_url(settings, media: str, deficiency: str) -> str | None:
    """시트에서 검수용 URL을 찾는다. 쓸 수 없으면 사유를 남기고 None."""
    lookup = _new_sheets_client(settings).lookup_reference(media, deficiency, reference_kind="검수용")

    if not lookup.usable:
        if not lookup.combination_found:
            _console_log(f"중단: 시트에 '{media} / {deficiency}' 조합이 없습니다.")
            _console_log("      --list 로 사용 가능한 조합을 확인하세요.")
        else:
            _console_log(f"중단: {lookup.row_number}행 '{lookup.media} / {lookup.deficiency}' — {lookup.blocked_reason}")
            if lookup.production_url:
                _console_log("      실전용 칸에는 값이 있지만 임의 대체는 하지 않습니다.")
        return None

    _console_log(f"시트 조회 완료 ({lookup.row_number}행): {lookup.media} / {lookup.deficiency}")
    _console_log(f"검수용 참고 URL: {lookup.url}")
    return lookup.url


def run_probe_align() -> int:
    """사용자가 직접 가운데 정렬한 결과로 selector를 역추적."""
    global LOG_FILE
    from app.services.browser import BrowserAutomation

    settings = load_settings()
    if not settings.enable_external_actions:
        _console_log("ENABLE_EXTERNAL_ACTIONS=false 입니다.")
        return 2
    out_dir = BASE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = out_dir / f"align_{datetime.now():%Y%m%d_%H%M%S}.log"
    print(f"로그: {LOG_FILE}", flush=True)
    BrowserAutomation(
        enabled=settings.enable_external_actions,
        headless=settings.playwright_headless,
        user_data_dir=settings.playwright_user_data_dir,
        naver_blog_home_url=settings.naver_blog_home_url,
        log=_console_log,
    ).probe_align(wait_for_continue=_console_wait)
    return 0


def run_probe() -> int:
    """에디터 DOM 구조만 찍는다. 시트도 랜딩도 건드리지 않는다."""
    global LOG_FILE
    from app.services.browser import BrowserAutomation

    settings = load_settings()
    out_dir = BASE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = out_dir / f"probe_{datetime.now():%Y%m%d_%H%M%S}.log"
    print(f"진단 로그 파일: {LOG_FILE}", flush=True)
    if not settings.enable_external_actions:
        _console_log("ENABLE_EXTERNAL_ACTIONS=false 입니다.")
        return 2
    browser = BrowserAutomation(
        enabled=settings.enable_external_actions,
        headless=settings.playwright_headless,
        user_data_dir=settings.playwright_user_data_dir,
        naver_blog_home_url=settings.naver_blog_home_url,
        log=_console_log,
    )
    browser.probe_editor(wait_for_continue=_console_wait)
    return 0


def run_input_test() -> int:
    """입력 전략을 자동으로 시험해 '실제로 되는 방법' 하나를 찾는다."""
    global LOG_FILE
    from app.services.browser import BrowserAutomation

    settings = load_settings()
    if not settings.enable_external_actions:
        _console_log("ENABLE_EXTERNAL_ACTIONS=false 입니다.")
        return 2
    out_dir = BASE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = out_dir / f"inputtest_{datetime.now():%Y%m%d_%H%M%S}.log"
    shots = out_dir / f"shots_{datetime.now():%Y%m%d_%H%M%S}"
    print(f"로그: {LOG_FILE}", flush=True)
    print(f"스크린샷: {shots}", flush=True)

    browser = BrowserAutomation(
        enabled=settings.enable_external_actions,
        headless=settings.playwright_headless,
        user_data_dir=settings.playwright_user_data_dir,
        naver_blog_home_url=settings.naver_blog_home_url,
        log=_console_log,
    )
    winner = browser.test_input(wait_for_continue=_console_wait, shot_dir=shots)
    return 0 if (winner.get("body") or winner.get("title")) else 4


def run_analyze_draft() -> int:
    """임시저장 글을 불러와 제목/본문 DOM 을 역추적한다."""
    global LOG_FILE
    from app.services.browser import BrowserAutomation

    settings = load_settings()
    if not settings.enable_external_actions:
        _console_log("ENABLE_EXTERNAL_ACTIONS=false 입니다.")
        return 2
    out_dir = BASE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = out_dir / f"draft_{datetime.now():%Y%m%d_%H%M%S}.log"
    shots = out_dir / f"draftshots_{datetime.now():%Y%m%d_%H%M%S}"
    print(f"로그: {LOG_FILE}", flush=True)

    browser = BrowserAutomation(
        enabled=settings.enable_external_actions,
        headless=settings.playwright_headless,
        user_data_dir=settings.playwright_user_data_dir,
        naver_blog_home_url=settings.naver_blog_home_url,
        log=_console_log,
    )
    browser.analyze_draft(wait_for_continue=_console_wait, shot_dir=shots)
    return 0


def run_cli(args) -> int:
    global LOG_FILE
    settings = load_settings()
    out_dir = BASE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = out_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    print(f"로그: {LOG_FILE}", flush=True)
    _console_log(f"외부 작업 허용(ENABLE_EXTERNAL_ACTIONS): {settings.enable_external_actions}")
    if not settings.enable_external_actions:
        _console_log("ENABLE_EXTERNAL_ACTIONS=false 입니다. .env 에서 true 로 바꿔야 실행됩니다.")
        return 2

    # ── ① 참고 URL 결정 (시트 조회가 기본, --url 은 디버그용) ──
    if args.url:
        url = args.url.strip()
        _console_log(f"[디버그] 시트를 건너뛰고 지정한 URL을 사용합니다: {url}")
    else:
        if not (args.media and args.deficiency):
            _console_log("중단: --media 와 --deficiency 를 모두 지정하세요. (--list 로 조합 확인)")
            return 2
        url = _resolve_url(settings, args.media.strip(), args.deficiency.strip())
        if not url:
            return 3

    # ── ② 클립보드 모드: 브라우저 안에서 바로 복사→붙여넣기 ──
    if args.paste and not args.dry_run:
        from app.services.browser import BrowserAutomation

        browser = BrowserAutomation(
            enabled=settings.enable_external_actions,
            headless=settings.playwright_headless,
            user_data_dir=settings.playwright_user_data_dir,
            naver_blog_home_url=settings.naver_blog_home_url,
            log=_console_log,
        )
        _console_log("클립보드 모드 — 랜딩 구간을 복사해 에디터에 붙여넣습니다(서식·이미지 유지).")
        n = max(1, int(args.bulk or 1))
        if n > 1:
            _console_log(f"대량 모드 — 같은 랜딩으로 글 {n}개 연속 작성(발행 없음)")
        if args.publish:
            _console_log("★ --publish 지정됨 — READY 후 순차 발행하고 시트에 URL을 기록합니다")
        def _save_published_urls(urls: list) -> str:
            """발행 주소를 '랜딩' 시트에 기록. 브라우저 정리 **전에** 호출된다."""
            from app.services.sheets import BlogLinkWriter, SheetsClient

            for i, u in enumerate(urls, 1):
                _console_log(f"   [{i}] {u}")
            writer_client = SheetsClient(
                credential_path=settings.google_service_account_json,
                reference_spreadsheet_id=settings.reference_spreadsheet_id,
                enabled=True, log=_console_log, readonly=False)
            rep = BlogLinkWriter(writer_client, _console_log).append_blog_links(urls)
            return f"{rep['written']}건 기록 (행 {rep['rows']})"

        res = browser.paste_from_landing(
            landing_url=url,
            title=(args.title or "").strip(),
            wait_for_continue=_console_wait,
            bulk=n,
            publish=bool(args.publish),
            edit_copy=bool(args.edit_copy),
            capture_align=bool(args.capture_align),
            on_published=_save_published_urls if args.publish else None,
        )

        # 발행 URL 시트 기록
        #   ★ 방식 원복(2026-08-20 사용자 요청): **발행 버튼을 눌러 글이 만들어진 그 순간**
        #     주소를 하나씩 받아 두었다가(_published_urls), 전부 끝난 뒤 한꺼번에 기록한다.
        #     RSS 재수집은 쓰지 않는다 — 발행 직후 RSS 반영이 늦어 '1/5건'처럼 모자란
        #     목록으로 덮어써 버렸다(2026-08-20 실측).
        urls = list(getattr(browser, "_published_urls", []) or [])
        if args.publish and getattr(browser, "_sheet_saved", False):
            _console_log("")
            _console_log(f"── 시트 기록 — 발행 직후 이미 저장 완료 ✅ ({len(urls)}건) ──")
        elif args.publish:
            # 정리 단계보다 먼저 저장하는 경로가 실패했을 때만 여기서 한 번 더 시도한다.
            _console_log("")
            _console_log(f"── 시트 기록 (재시도 · 주소 {len(urls)}건) ──")
            for i, u in enumerate(urls, 1):
                _console_log(f"   [{i}] {u}")
            if urls:
                try:
                    from app.services.sheets import BlogLinkWriter, SheetsClient

                    writer_client = SheetsClient(
                        credential_path=settings.google_service_account_json,
                        reference_spreadsheet_id=settings.reference_spreadsheet_id,
                        enabled=True, log=_console_log, readonly=False)
                    w = BlogLinkWriter(writer_client, _console_log)
                    rep = w.append_blog_links(urls)
                    _console_log(f"   시트 기록: {rep['written']}건 (행 {rep['rows']})")
                except Exception as exc:  # noqa: BLE001
                    _console_log(f"   시트 기록 실패: {type(exc).__name__}: {exc}")
            else:
                _console_log("   기록할 발행 URL이 없습니다")
        _console_log("")
        _console_log("── 최종 입력 내역 ──")
        _console_log(f"에디터 URL : {res.page_url}")
        _console_log(f"제목       : {res.title}  ({'성공' if res.title_filled else '실패'})")
        _console_log(f"본문       : {res.body}  ({'성공' if res.body_filled else '실패'})")
        _console_log("발행/예약/저장은 수행하지 않았습니다.")
        return 0 if res.body_filled else 4

    # ── ③ 랜딩 원문 추출 ──
    extractor = ReferenceExtractor(
        enabled=settings.enable_external_actions,
        headless=settings.playwright_headless,
        user_data_dir=settings.playwright_user_data_dir,
        log=_console_log,
    )
    brief = extractor.extract(url)

    if args.rewrite:
        draft = compose(brief)
        ok, problems = verify(draft, brief)
    else:
        draft = copy_source(brief)
        has_body = bool(draft.body.strip())
        ok, problems = has_body, ([] if has_body else ["본문을 한 줄도 추출하지 못했습니다."])
        _console_log(f"원문 그대로 모드 — 랜딩 블록 {len(brief.content_blocks)}개를 순서대로 옮깁니다.")

    if args.title:
        draft = type(draft)(title=args.title.strip(), body=draft.body, source_url=draft.source_url)

    _console_log("")
    _console_log("=" * 64)
    _console_log(f"제목: {draft.title}")
    _console_log("=" * 64)
    for para in draft.paragraphs:
        for line in para.split("\n"):
            print("    " + line)
        print()
    _console_log("=" * 64)
    _console_log(f"본문 {len(draft.body)}자 · 문단 {len(draft.paragraphs)}개")
    if not ok:
        for p in problems:
            _console_log(f"검증 실패: {p}")
        _console_log("초안을 입력하지 않고 중단합니다.")
        return 3

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{draft.title}\n\n{draft.body}\n", encoding="utf-8")
        _console_log(f"원문 저장: {path}")

    if args.dry_run:
        _console_log("--dry-run 이므로 네이버 블로그에는 접속하지 않고 종료합니다.")
        return 0

    # ── ④ 네이버 블로그 에디터에 입력(초안까지) ──
    from app.services.browser import BrowserAutomation

    browser = BrowserAutomation(
        enabled=settings.enable_external_actions,
        headless=settings.playwright_headless,
        user_data_dir=settings.playwright_user_data_dir,
        naver_blog_home_url=settings.naver_blog_home_url,
        log=_console_log,
    )
    result = browser.write_draft(draft.title, draft.body, wait_for_continue=_console_wait)

    _console_log("")
    _console_log("── 최종 입력 내역 ──")
    _console_log(f"에디터 URL : {result.page_url}")
    _console_log(f"제목       : {result.title}")
    _console_log(f"제목 입력  : {'성공' if result.title_filled else '실패'}")
    _console_log(f"본문 입력  : {'성공' if result.body_filled else '실패'} ({len(result.body)}자)")
    _console_log("발행/예약/저장은 수행하지 않았습니다. 네이버 화면에서 직접 확인 후 발행하세요.")
    return 0 if (result.title_filled and result.body_filled) else 4


def main() -> None:
    p = argparse.ArgumentParser(description="매체+결핍으로 검수용 랜딩을 찾아 네이버 블로그 초안 작성")
    p.add_argument("--media", help="매체 (GFA / 카카오모먼트 / 메타 / 틱톡)")
    p.add_argument("--deficiency", help="결핍명 (예: '팔자 / 머니')")
    p.add_argument("--list", action="store_true", help="시트의 조합과 검수용 랜딩 준비 현황 출력")
    p.add_argument("--paste", action="store_true", help="구간 복사→붙여넣기(서식·이미지 유지)")
    p.add_argument("--bulk", type=int, default=1,
                   help="같은 참고 랜딩으로 글 N개 연속 작성(로그인 1회)")
    p.add_argument("--capture-align", action="store_true",
                   help="[진단] paste·후처리 후 멈춰서 직접 정렬하는 클릭을 기록")
    p.add_argument("--edit-copy", action="store_true",
                   help="기준 글의 '수정' 화면에서 제목·본문을 통째로 복사(출처 안 붙음) + 모바일 미리보기")
    p.add_argument("--publish", action="store_true",
                   help="★READY 탭을 순차 발행하고 URL을 시트에 기록(되돌릴 수 없음). "
                        "지정하지 않으면 READY 상태로만 두고 끝난다")
    p.add_argument("--rewrite", action="store_true", help="원문 복제 대신 새로 작성")
    p.add_argument("--dry-run", action="store_true", help="시트 조회 + 원문 추출까지만")
    p.add_argument("--title", help="글 제목(생략 시 랜딩 제목)")
    p.add_argument("--out", help="추출한 원문을 저장할 텍스트 파일 경로")
    p.add_argument("--url", help="[디버그] 시트를 건너뛰고 URL 직접 지정")
    p.add_argument("--probe-align", action="store_true",
                   help="[진단] 직접 가운데 정렬하면 그 selector를 역추적(저장·발행 없음)")
    p.add_argument("--probe-editor", action="store_true",
                   help="[진단] 에디터까지만 열고 DOM 구조를 출력(입력·저장·발행 없음)")
    p.add_argument("--test-input", action="store_true",
                   help="[진단] 입력 전략을 자동으로 돌려 실제로 되는 방법을 확정(저장·발행 없음)")
    p.add_argument("--analyze-draft", action="store_true",
                   help="[진단] 임시저장 글을 불러와 제목/본문 DOM 구조를 역추적(저장·발행 없음)")
    args = p.parse_args()

    if args.list:
        sys.exit(show_list())
    if args.probe_align:
        sys.exit(run_probe_align())
    if args.probe_editor:
        sys.exit(run_probe())
    if args.test_input:
        sys.exit(run_input_test())
    if args.analyze_draft:
        sys.exit(run_analyze_draft())
    if args.media or args.deficiency or args.url:
        sys.exit(run_cli(args))

    from app.gui import BlogLandingApp

    BlogLandingApp().mainloop()


if __name__ == "__main__":
    main()
