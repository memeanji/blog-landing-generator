r"""실전용 블로그 랜딩 생성 — **검수용(`v2/run.py`)과 완전히 분리된 별도 실행 경로**.

`v2/run.py`(검수용)는 이 파일에서 import 하지 않는다. 공통 저수준 모듈
(`browser` · `writer` · `source_view` · `sheets` · `landing_sheet`)만 **그대로 재사용**하며,
그 모듈들의 동작은 바꾸지 않는다(검수용 결과가 달라지면 안 되기 때문).

★새 글을 만드는 게 아니다. **이미 발행된 검수용 글을 수정해서 내용을 갈아끼운다.**
  그래서 글 주소(logNo)는 그대로 유지된다.

흐름 (한 행마다)
    랜딩 시트: 매체 + 날짜 + K(검수용 블로그 URL)가 있는 행을 **위에서부터 순서대로**
      → 그 행의 J(링크) = 그 행 전용 제품 링크(UTM 이 행마다 다르다. 절대 섞이면 안 된다)
    1. 검수용 글을 **수정 화면**으로 연다 → 모바일 미리보기
    2. 기존 제목 + 기존 본문을 전부 지운다
       (본문 복사는 기본이 '이미지 클릭 → Ctrl+A' 통째 복사 — 이미지 그룹/정렬이 보존된다)
    3. 실전용 참고글도 **수정 화면 + 모바일 미리보기**로 열어 제목/본문을 한 번에 복사
    4. 검수용 수정 화면으로 돌아와 제목 입력 → 본문 붙여넣기
    4. 하단 제품 링크를 **그 행의 J 링크**로 생성(oglink 카드, 표시는 도메인·href 는 전체 UTM)
    5. 제품 카드만 중앙정렬(본문 정렬은 원본 그대로) → 댓글 허용 OFF 확인 → 발행
       → 10~40초 랜덤 대기 → 다음 행

실행
    # 무엇을 어떤 링크로 만들지 시트만 보고 확인(브라우저 안 켬)
    .\.venv\Scripts\python.exe -m v2.run_production --media 카모 --deficiency "흑자 / 머니(연습)" --dry-run

    # 실제 실행 (발행까지)
    .\.venv\Scripts\python.exe -m v2.run_production --media 카모 --deficiency "흑자 / 머니(연습)" --publish
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
import traceback
from datetime import datetime

from .config import load_settings, resolve_headless
from .logger import Log
from . import (accounts, brands, browser, edit_post, landing_sheet,
               session_store, sheets, writer)

# 발행 간격 — 사람이 올리는 것처럼. 매 건 이 범위에서 새로 뽑는다.
DELAY_MIN, DELAY_MAX = 10, 40

# 실전용 참고글은 이미지가 수십 장이라 업로드가 오래 걸린다(검수용은 4장이라 60초로 충분).
IMG_WAIT_MS = 240_000

# 새 글의 제목/본문을 어디서 가져올지
#   ref    = 참고용 랜딩 시트의 **실전용 블로그랜딩** 참고글 (긴 후기형 콘텐츠)
#   review = 랜딩 시트 K열의 **검수용 블로그 글** (짧은 고지형 콘텐츠)
CONTENT_SOURCES = ("ref", "review")


def stage(log, brand, name: str, status: str = "ok", reason: str = "", **fields) -> None:
    """단계 하나를 사람/기계 양쪽에 남긴다.

        [단계] brand=doctor_nuscent stage=utm_sheet_access status=failed
               reason=permission_denied

    어디서 실패했는지 로그만 보고 바로 알 수 있게 하는 것이 목적이다.
    """
    bid = getattr(brand, "id", "") or str(brand or "")
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    log(f"[단계] brand={bid} stage={name} status={status}"
        + (f" reason={reason}" if reason else "")
        + (f" {extra}" if extra else ""))
    log.event("stage", brand=bid, stage=name, status=status,
              reason=reason or "", **fields)


def stage_failed(log, brand, name: str, exc: Exception, **fields) -> str:
    """예외 → reason 코드. 메시지에 이미 reason 이 있으면 그대로 쓴다."""
    text = f"{exc}"
    reason = ""
    for token in text.split():
        if token.startswith("reason="):
            reason = token[len("reason="):]
            break
    if not reason:
        reason = {"RuntimeError": "failed"}.get(type(exc).__name__,
                                                type(exc).__name__)
    stage(log, brand, name, "failed", reason, **fields)
    return reason


# ★행 단위 오류 vs 공통 오류
#   · 행 단위 : 그 글에서만 생긴 문제(제품 카드 못 지움 · 검증 실패 · 이미지 안 올라옴 …)
#               → 그 건만 실패로 두고 다음 건을 계속한다(--on-error skip).
#   · 공통    : 로그인 만료 · 브라우저 종료 · 시트 접근 불가처럼 **이후 모든 건에 영향**을
#               주는 문제 → 남은 건을 줄줄이 실패시키지 말고 배치를 멈춘다.
#                 (--on-error skip 이어도 멈춘다. 멈춘 뒤 재로그인/복구하고 이어서 한다)
COMMON_ERROR_MARKS = (
    # 브라우저·컨텍스트가 죽은 경우
    "target page, context or browser has been closed", "targetclosed",
    "browser has been closed", "browser closed", "connection closed",
    "websocket", "browser.newcontext", "playwright", "eventloop is closed",
    # 로그인/세션
    "로그인", "세션", "login", "nid_", "2단계 인증", "새로운 기기",
    # 구글 시트(기준시트 · UTM 빌더)
    "stage=utm_sheet_access", "stage=reference_sheet_access", "stage=row_match",
    "permission_denied", "apierror", "gspread", "google_temporary_error",
    "quota", "rate limit",
    # 계정 오발행 방지 가드
    "선택한 계정",
)


def is_common_error(exc: Exception) -> bool:
    """이후 모든 건에 영향을 주는 오류인가(= 배치를 멈춰야 하는가)."""
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(mark.casefold() in text for mark in COMMON_ERROR_MARKS)


def apply_brand(args, log):
    """이번 실행의 브랜드를 고정한다 — 기준시트 + UTM 빌더가 **한 세트로** 바뀐다."""
    brand = brands.resolve(getattr(args, "brand", "") or "")
    brand.check()
    brand.require_ready()          # ★준비 중(ready=false) 브랜드는 여기서 멈춘다
    sheets.set_brand(brand)
    landing_sheet.set_brand(brand)
    args.brand_obj = brand
    log(f"[브랜드] {brand.title} (brand={brand.id})")
    log(f"[브랜드] 기준시트   = {brand.reference_title} ({brand.reference_sheet_id})")
    log(f"[브랜드] UTM 빌더   = {brand.utm_title} ({brand.utm_sheet_id})")
    stage(log, brand, "brand_config", "ok", "",
          reference_sheet=brand.reference_sheet_id, utm_sheet=brand.utm_sheet_id)
    return brand


def today_tag() -> str:
    """오늘 날짜를 시트 표기(예: 821)로."""
    now = datetime.now()
    return f"{now.month}{now.day:02d}"


def _check_account(args, blog_id: str, log) -> None:
    """선택한 계정과 실제 로그인된 계정이 다르면 **작성 전에** 멈춘다."""
    acc = getattr(args, "account_obj", None)
    if not acc or not acc.blog_id or not blog_id:
        return
    if blog_id.casefold() == acc.blog_id.casefold():
        log(f"[계정] 확인 완료 — 선택({acc.title}) = 로그인({blog_id})")
        return
    raise RuntimeError(
        f"선택한 계정({acc.title} / {acc.blog_id})과 실제 로그인된 계정({blog_id})이 "
        f"다릅니다. 잘못된 블로그의 글을 수정하는 것을 막기 위해 중단합니다. "
        f"→ `--account {acc.id} --relogin` 으로 그 계정에 직접 로그인하세요.")


def _show_account(args, log) -> None:
    acc = getattr(args, "account_obj", None)
    if not acc:
        return
    log(f"[계정] {acc.title} (id={acc.id}"
        + (f" · blog_id={acc.blog_id}" if acc.blog_id else "") + ")")
    info = session_store.describe(acc)
    log(f"[계정] 프로필 = {info['profile']}")
    log("[계정] 저장 세션 = " + (
        f"있음 (쿠키 {info['cookies']}개 · 저장 {info['saved_at']})"
        if info["state_exists"] else
        "없음 — 로그인 창이 한 번 뜹니다(로그인하면 다음부터는 창 없이 진행)"))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="실전용 블로그 랜딩 생성(검수용과 별도)")
    p.add_argument("--brand", default="",
                   help="브랜드 (brands.json 의 id/label. 예: repurely / 닥터누센트). "
                        "비우면 기본 브랜드(리퓨어리) — 기준시트와 UTM 빌더가 한 세트로 바뀐다")
    p.add_argument("--on-error", choices=("abort", "skip"), default="abort",
                   help="한 건이 실패했을 때. abort(기본)=그 배치를 통째로 멈춘다 / "
                        "skip=그 건만 실패 처리하고 나머지는 계속한다")
    p.add_argument("--account", default="",
                   help="사용할 네이버 계정 (accounts.json 의 id/label/blog_id). "
                        "세션은 sessions/<id>/ 에 계정별로 따로 보관된다")
    p.add_argument("--events", action="store_true",
                   help="진행 상황을 `@@EVENT {json}` 한 줄로도 출력한다(GUI 용)")
    p.add_argument("--media", required=True, help="매체 (카모 / gfa / 메타 / 틱톡)")
    p.add_argument("--deficiency", required=True, help="결핍 (예: '흑자 / 머니(연습)')")
    p.add_argument("--date", help=f"랜딩 시트 날짜. 기본은 오늘({today_tag()})")
    p.add_argument("--ref-tab",
                   help="기준랜딩 탭 이름(계정별 탭). 기본 '스마일 현미 기준랜딩'")
    p.add_argument("--ref-kind", choices=("실전용", "검수용"), default="실전용",
                   help="참고용 랜딩 시트에서 어느 열의 참고글을 쓸지 "
                        "(실전용 블로그랜딩 / 검수용 블로그랜딩). 기본 실전용")
    p.add_argument("--content-from", choices=CONTENT_SOURCES, default="ref",
                   help="새 글의 제목/본문 출처. ref=실전용 참고글(기본) / review=검수용 글")
    p.add_argument("--count", type=int, default=0,
                   help="처리할 건수. 0(기본)이면 시트에 있는 검수용 URL 전부")
    p.add_argument("--start", type=int, default=1,
                   help="몇 번째부터 처리할지(1부터). 중간에 끊겼을 때 이어서 돌리는 용도")
    p.add_argument("--publish", action="store_true",
                   help="발행까지 진행(되돌릴 수 없다). 없으면 READY 까지만")
    p.add_argument("--dry-run", action="store_true",
                   help="시트 매칭만 확인하고 끝낸다(브라우저를 켜지 않는다)")
    p.add_argument("--blog-id", help="내 블로그 ID 직접 지정")
    p.add_argument("--relogin", action="store_true", help="세션을 지우고 직접 로그인")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None,
                   help="창을 띄우지 않고 돌린다. 기본: 검수용이면 headless, "
                        "실전용이면 창 띄움. 창을 보려면 --no-headless")
    p.add_argument("--delay-min", type=int, default=DELAY_MIN,
                   help=f"발행 간격 최소 초(기본 {DELAY_MIN})")
    p.add_argument("--delay-max", type=int, default=DELAY_MAX,
                   help=f"발행 간격 최대 초(기본 {DELAY_MAX})")
    p.add_argument("--hold", type=int, default=300,
                   help="발행하지 않는 모드에서 창을 열어 둘 초(기본 300)")
    p.add_argument("--keep-open", action="store_true", help="끝나도 창을 닫지 않는다")
    p.add_argument("--no-sheet", action="store_true",
                   help="시트에 실전용 결과를 기록하지 않는다")
    p.add_argument("--chunk-imgs", type=int, default=6,
                   help="구간 분할 방식일 때 한 구간당 목표 이미지 수(기본 6)")
    p.add_argument("--mode", choices=("convert", "create"), default="convert",
                   help="convert=이미 있는 검수용 글을 실전용으로 수정(기본) / "
                        "create=실전용 콘텐츠로 **새 글을 만들어** 블로그 링크 열에 기록")
    p.add_argument("--rows", default="",
                   help="이 시트 행 번호만 처리한다(쉼표 구분, 예: 812,815,820). "
                        "실패한 건만 다시 돌릴 때 쓴다. 주면 --count/--start 는 무시한다")
    p.add_argument("--campaign",
                   help="utm_campaign 접두어로 대상 행을 좁힌다 (예: g_i_b_o_l_0824). "
                        "하루에 그룹이 여러 개일 때 엉뚱한 그룹에 쓰는 것을 막는다")
    p.add_argument("--batch", type=int, default=0,
                   help="한 번에 열어 둘 탭 수. 0(기본)이면 5~6개 사이에서 임의로 끊는다")
    p.add_argument("--copy-mode", choices=("selectall", "chunk"), default="selectall",
                   help="본문 복사 방식. selectall=이미지 클릭 후 Ctrl+A 로 통째 복사(기본, "
                        "이미지 그룹·가로배치·정렬 보존) / chunk=구간 분할 복사")
    p.add_argument("--ref-copy-from", choices=("edit", "view"), default="edit",
                   help="실전용 참고글을 어디서 복사할지. edit=수정 화면(기본) / "
                        "view=발행 화면(수정 화면에서 이미지가 안 넘어올 때)")
    return p.parse_args(argv)


def plan(args, settings, log) -> tuple[list[dict], str]:
    """시트만 읽어 '무엇을 · 어떤 제품 링크로' 만들지 계획을 세운다."""
    date = args.date or today_tag()
    acc = getattr(args, "account_obj", None)
    brand = getattr(args, "brand_obj", None) or apply_brand(args, log)
    on_missing = "drop" if getattr(args, "on_error", "abort") == "skip" else "raise"

    tab = getattr(args, "ref_tab", None) or (acc.tab_for_brand(brand) if acc else "")
    if not tab and acc and acc.ref_tab:
        log(f"[시트] 계정 탭 {acc.ref_tab!r} 은 다른 브랜드의 탭이라 쓰지 않습니다 "
            f"— {brand.title} 기본 탭을 씁니다")
    if tab:
        sheets.set_tab(tab)
    log(f"[시트] 기준랜딩 탭 = {sheets.active_tab()!r} ({brand.reference_title})")
    stage(log, brand, "reference_sheet_selected", "ok", "",
          sheet=brand.reference_sheet_id, tab=sheets.active_tab())
    stage(log, brand, "utm_sheet_selected", "ok", "", sheet=brand.utm_sheet_id)

    # 1) 대상 행 — UTM 빌더에서 (접근 실패면 여기서 원인이 드러난다)
    try:
        if args.mode == "create":
            # 아직 안 만든 행(블로그 링크 비어 있음)에 **새 글을 만들어** 기록한다.
            rows = landing_sheet.find_pending_rows(
                settings.service_account_json, settings.spreadsheet_id, args.media,
                date, log, campaign=args.campaign or "", note=args.deficiency or "",
                on_missing=on_missing)
            if not rows:
                raise RuntimeError(f"[시트] 매체 {args.media} / 날짜 {date} 에 "
                                   f"`블로그 링크`가 비어 있는 행이 없습니다.")
        else:
            rows = landing_sheet.find_published_rows(
                settings.service_account_json, settings.spreadsheet_id, args.media,
                date, log, note=args.deficiency or "", campaign=args.campaign or "",
                on_missing=on_missing)
            if not rows:
                raise RuntimeError(f"[시트] 매체 {args.media} / 날짜 {date} 에 블로그 URL 이 "
                                   f"있는 행이 없습니다. 먼저 랜딩을 만들어야 합니다.")
    except Exception as exc:                                   # noqa: BLE001
        name = ("utm_sheet_access" if "utm_sheet_access" in f"{exc}" else "row_match")
        stage_failed(log, brand, name, exc, media=args.media, date=date)
        raise
    stage(log, brand, "row_match", "ok", "", matched=len(rows),
          rows=",".join(str(r["row"]) for r in rows[:20]))

    # 2) 기준시트 — 매체+결핍 → 실전용 참고 URL
    try:
        ref = sheets.find_reference(settings.service_account_json,
                                    settings.spreadsheet_id,
                                    args.media, args.deficiency, args.ref_kind)
    except Exception as exc:                                   # noqa: BLE001
        stage_failed(log, brand, "reference_lookup", exc,
                     media=args.media, deficiency=args.deficiency)
        raise
    log(f"[시트] {args.ref_kind} 참고글 — {ref.url} (참고용 랜딩 {ref.row}행)")

    # ★제품 링크 출처는 참고글 종류에 따라 다르다.
    #   · 검수용 랜딩  → 참고용 랜딩 시트의 **검수용 제품 링크**(UTM 없는 순수 제품 URL)
    #   · 실전용 랜딩  → UTM 빌더 시트 그 행의 **링크**(행마다 UTM 이 다르다)
    if args.ref_kind == "검수용":
        if not ref.product_url:
            raise RuntimeError(
                f"[시트] 참고용 랜딩 {ref.row}행({ref.media}/{ref.deficiency})의 "
                f"'검수용 제품 링크' 가 비어 있습니다. 그 칸을 채워 주세요.")
        log(f"[제품링크] 검수용 — 참고용 랜딩 {ref.row}행의 검수용 제품 링크를 씁니다")
        log(f"           {ref.product_url}")
        for r in rows:
            r["product_url"] = ref.product_url
        stage(log, brand, "product_url_lookup", "ok", "", source="reference_sheet",
              product_url=ref.product_url)
    else:
        log("[제품링크] 실전용 — UTM 빌더 시트의 행별 '링크' 를 씁니다(행마다 다름)")
        stage(log, brand, "product_url_lookup", "ok", "", source="utm_sheet",
              rows=len(rows))

    # ★행을 직접 지정하면(실패 건 재실행) 그 행만 처리한다 — count/start 는 무시.
    want_rows = [int(x) for x in re.findall(r"\d+", args.rows or "")]
    if want_rows:
        picked = [r for r in rows if int(r["row"]) in want_rows]
        missing = sorted(set(want_rows) - {int(r["row"]) for r in picked})
        log(f"[시트] --rows 지정 — {len(picked)}행만 처리합니다 "
            f"{[r['row'] for r in picked]}"
            + (f" (조건에 맞지 않아 빠진 행: {missing})" if missing else ""))
        stage(log, brand, "row_filter", "ok", "", picked=len(picked),
              missing=",".join(str(m) for m in missing))
        if not picked:
            raise RuntimeError(f"[시트] --rows {args.rows} 에 해당하는 행이 없습니다.")
        return picked, ref.url

    rows = rows[max(0, args.start - 1):]
    if args.count and args.count > 0:
        rows = rows[:args.count]
    return rows, ref.url


def result_columns(args, settings, log) -> dict:
    """발행 전에 결과 기록 열을 확인한다(없으면 여기서 멈춘다)."""
    if args.no_sheet:
        log("[시트] --no-sheet — 결과를 기록하지 않습니다")
        return {}
    return landing_sheet.find_result_columns(
        settings.service_account_json, settings.spreadsheet_id, log, media=args.media)


async def convert_one(ctx, get_src, item, no, total, log, out_dir,
                      chunk_imgs: int = 6,
                      copy_mode: str = "selectall",
                      brand=None) -> "writer.NewPost":
    """검수용 글 1개를 **수정 화면에서** 실전용 내용으로 갈아끼운다(발행은 하지 않음)."""
    tag = f"[{no}/{total}]"
    log("")
    log(f"════ {tag} 실전용 전환 (랜딩 {item['row']}행 · 순번 {item['seq']}) ════")
    log(f"{tag} 검수용 URL   : {item['blog_url']}")
    log(f"{tag} 제품 링크    : {item['product_url']}")

    # 1~3. 검수용 글 수정 화면 → 모바일 미리보기
    post, blog_id, log_no = await edit_post.open_for_edit(ctx, item["blog_url"], log)
    await post.switch_to_mobile()
    await post.ensure_mobile()
    log(f"{tag} 검수용 수정 화면 · 모바일 미리보기 진입 완료")

    # 4. 기존 제목 + 본문 전부 삭제 (본문 먼저, 그 다음 제목)
    await edit_post.clear_body(post, log)
    await edit_post.clear_title(post, log)
    log(f"{tag} 기존 제목/본문 삭제 완료")

    # 5~9. 실전용 참고글을 수정 화면 + 모바일로 열어 제목/본문 복사
    log(f"{tag} 실전용 참고글 준비 시작 — get_src() 호출")
    src = await get_src()
    log(f"{tag} 실전용 참고글 준비 완료 — 제목 {src.title[:30]!r} · "
        f"컴포넌트 {src.want_comps}개 · 이미지 {src.want_imgs}장")

    # 10~11. 검수용 수정 화면으로 돌아와 제목 입력
    await post.page.bring_to_front()
    await post.type_title(src.title)
    log(f"{tag} 제목 복사 완료 — {src.title!r}")

    # 12. 본문 붙여넣기
    src.extra_images = 0        # ★글마다 초기화(공유 객체)
    if copy_mode == "selectall":
        # ★이미지 클릭 → Ctrl+A → 복사. 이미지 2장 가로배치·그룹·개별 정렬이 그대로 따라온다.
        #   하단 기존 제품 카드까지 딸려오므로 붙여넣은 뒤 그 카드를 지운다.
        if getattr(src, "from_view", False):
            expect = await edit_post.copy_whole_range(src, log, label="실전용 참고글")
        else:
            expect = await edit_post.copy_whole_body(src, log, label="실전용 참고글")
        await edit_post.paste_whole_body(post, expect, log, tag=tag,
                                         img_timeout_ms=IMG_WAIT_MS)
        stage(log, brand, "product_link_find", "ok", "", row=item["row"])
        removed = await edit_post.remove_pasted_card(post, log, tag=tag,
                                                     product_url=item['product_url'])
        stage(log, brand, "product_link_remove", "ok",
              "" if removed else "nothing_to_remove", row=item["row"])
        st = await post.stats()
        src.extra_images = st["imgs"] - src.want_imgs   # verify_body 기준 맞추기
        log(f"{tag} 본문 복사 완료(전체 선택 방식) — 이미지 {st['imgs']}장 · {st['chars']}자")
    else:
        await edit_post.paste_in_chunks(post, src, log, tag=tag,
                                        max_imgs=chunk_imgs, img_timeout_ms=IMG_WAIT_MS)
        await post.verify_body(src)
        log(f"{tag} 본문 복사 완료(구간 분할) — 이미지 {src.want_imgs}장 · {src.want_chars}자")

    # 13. 하단 제품 링크 — **이 행의 J 링크**로
    await post.append_product_link(item["product_url"])
    src.extra_images += 1
    stage(log, brand, "product_link_insert", "ok", "", row=item["row"],
          product_url=item["product_url"])
    log(f"{tag} 제품 링크 교체 완료")

    # 14. 정렬 — ★전체 중앙정렬은 하지 않는다.
    #     참고글을 복사할 때 각 컴포넌트의 정렬/서식이 이미 따라온다. 여기서 전체를 다시
    #     가운데로 밀면 원본 정렬을 덮어써서 깨진다(2026-08-21 사용자 확인).
    #     새로 만든 제품 카드 하나만 가운데로 옮긴다.
    await edit_post.center_product_card(post, item["product_url"], log, tag=tag)
    log(f"{tag} 본문 정렬은 원본 그대로 유지(전체 중앙정렬 안 함)")

    # 15. 하단 최종 검증 — 제품 카드 1개만 · 벌거벗은 URL 없음
    await edit_post.verify_product_tail(post, item["product_url"], log, tag=tag)
    stage(log, brand, "product_link_verify", "ok", "", row=item["row"])

    await post.verify_body(src, check_texts=False)
    await post.shot(f"v2_prod_{no}_ready", out_dir)
    log(f"{tag} 작성 완료(READY · logNo={log_no})")
    log.event("post_ready", no=no, total=total)
    return post


async def create_one(ctx, get_src, blog_id, item, no, total, log, out_dir,
                     brand=None) -> "writer.NewPost":
    """실전용 콘텐츠로 **새 글**을 만든다(발행은 하지 않음).

    검수용 글을 수정하는 convert 모드와 달리 새 글쓰기 탭을 연다.
    본문 복사/제품링크/정렬 처리는 convert 모드와 **완전히 같은 함수**를 쓴다.
    """
    tag = f"[{no}/{total}]"
    log("")
    log(f"════ {tag} 실전용 새 글 (시트 {item['row']}행 · 순번 {item['seq']} · "
        f"{item.get('campaign', '')}) ════")
    log(f"{tag} 제품 링크    : {item['product_url']}")

    post = await writer.open_write(ctx, blog_id, log)
    await post.switch_to_mobile()
    await post.ensure_mobile()
    log(f"{tag} 새 글 · 모바일 미리보기 준비 완료")

    src = await get_src()
    await post.page.bring_to_front()
    await post.type_title(src.title)
    log(f"{tag} 제목 복사 완료 — {src.title!r}")

    src.extra_images = 0
    if getattr(src, "from_view", False):
        expect = await edit_post.copy_whole_range(src, log, label="실전용 참고글")
    else:
        expect = await edit_post.copy_whole_body(src, log, label="실전용 참고글")
    await edit_post.paste_whole_body(post, expect, log, tag=tag,
                                     img_timeout_ms=IMG_WAIT_MS)
    stage(log, brand, "product_link_find", "ok", "", row=item["row"])
    removed = await edit_post.remove_pasted_card(post, log, tag=tag,
                                                 product_url=item["product_url"])
    stage(log, brand, "product_link_remove", "ok",
          "" if removed else "nothing_to_remove", row=item["row"])
    st = await post.stats()
    src.extra_images = st["imgs"] - src.want_imgs
    log(f"{tag} 본문 복사 완료 — 이미지 {st['imgs']}장 · {st['chars']}자")

    await post.append_product_link(item["product_url"])
    src.extra_images += 1
    stage(log, brand, "product_link_insert", "ok", "", row=item["row"],
          product_url=item["product_url"])
    log(f"{tag} 제품 링크 생성 완료")

    await edit_post.center_product_card(post, item["product_url"], log, tag=tag)
    log(f"{tag} 본문 정렬은 원본 그대로 유지(전체 중앙정렬 안 함)")
    await edit_post.verify_product_tail(post, item["product_url"], log, tag=tag)
    stage(log, brand, "product_link_verify", "ok", "", row=item["row"])

    await post.shot(f"v2_prod_new_{no}_ready", out_dir)
    log(f"{tag} 작성 완료(READY)")
    log.event("post_ready", no=no, total=total)
    return post


async def close_stray_pages(ctx, keep_posts, keep_srcs, log) -> int:
    """실패한 건이 열어 둔 탭을 닫는다(--on-error skip 전용).

    READY 로 잡아 둔 글 탭과 참고글 탭은 **절대 닫지 않는다**. 그 둘과 첫 탭을 뺀
    나머지만 닫으므로, 실패 건 때문에 탭이 계속 쌓이지 않는다.
    """
    keep = set()
    for obj in list(keep_posts) + list(keep_srcs):
        pg = getattr(obj, "page", None)
        if pg is not None:
            keep.add(id(pg))
    pages = list(getattr(ctx, "pages", []) or [])
    closed = 0
    for pg in pages[1:]:                       # 첫 탭(빈 탭)은 남긴다
        if id(pg) in keep:
            continue
        try:
            await pg.close()
            closed += 1
        except Exception:                                      # noqa: BLE001
            pass
    if closed:
        log(f"[정리] 실패 건이 열어 둔 탭 {closed}개를 닫았습니다")
    return closed


async def main_async(args, settings, log) -> int:
    brand = getattr(args, "brand_obj", None) or apply_brand(args, log)
    failed_rows: list[int] = []
    items, ref_url = plan(args, settings, log)
    total = len(items)

    log("")
    log(f"[계획] 브랜드 {brand.title}(brand={brand.id}) · "
        f"기준시트 {brand.reference_title} · UTM 빌더 {brand.utm_title}")
    log(f"[계획] 실전용 {total}건 — 제목/본문 출처 = "
        f"{args.ref_kind + ' 참고글' if args.content_from == 'ref' else '기존 글(행마다 다름)'}")
    if args.mode == "create":
        log("[계획] ★새 글을 만들어 `블로그 링크` 열에 기록합니다.")
    else:
        log("[계획] ★새 글을 만들지 않습니다. 아래 글들을 '수정'해서 내용을 갈아끼웁니다.")
    for n, it in enumerate(items, start=1):
        content = ref_url if args.content_from == "ref" else it.get("blog_url", ref_url)
        log(f"   [{n}/{total}] 시트 {it['row']}행(순번 {it['seq']}"
            f"{' · ' + it['campaign'] if it.get('campaign') else ''})")
        if args.mode == "convert":
            log(f"          수정 대상: {it['blog_url']}")
        log(f"          내용 원본: {content}")
        log(f"          제품링크 : {it['product_url']}")
    try:
        cols = result_columns(args, settings, log)
    except Exception as exc:                                   # noqa: BLE001
        stage_failed(log, brand, "result_columns", exc)
        raise
    log.event("plan", total=total, ref_url=ref_url, brand=brand.id,
              brand_label=brand.title,
              reference_sheet=brand.reference_sheet_id,
              utm_sheet=brand.utm_sheet_id,
              rows=[it["row"] for it in items])

    if args.dry_run:
        log("")
        log("[dry-run] 시트 매칭만 확인했습니다. 브라우저를 켜지 않았습니다.")
        log.event("run_finished", ok=True, dry_run=True, total=total, published=[],
                  brand=brand.id, failed_rows=failed_rows,
                  rows=[it["row"] for it in items])
        return 0

    pw = ctx = None
    published: list[str] = []
    done: list[dict] = []
    failed = False
    try:
        # ★로그인이 필요할 때만 창을 띄우고, 그 뒤로는 headless 로 진행한다.
        pw, ctx, logged = await browser.open_session(settings, log, relogin=args.relogin)
        blog_id = args.blog_id or logged or await browser.resolve_blog_id(ctx, settings, log)
        log(f"[블로그] 새 글을 작성할 계정 = {blog_id}")
        _check_account(args, blog_id, log)

        # 복사 원본(실전용 참고글)은 **처음 필요할 때** 수정 화면으로 연다.
        #   ref 모드는 13건 내내 같은 글이므로 탭을 한 번만 열고 계속 재사용한다.
        cache: dict = {}

        def make_get_src(content_url: str):
            async def _get():
                if content_url not in cache:
                    # ★참고글 소유 계정 ≠ 로그인 계정이면 수정 화면이 안 열린다(소유자 전용).
                    #   그때는 발행 화면 복사로 자동 전환한다(소유권 불필요).
                    owner = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/", content_url)
                    owner = owner.group(1) if owner else ""
                    use_view = args.ref_copy_from == "view"
                    if owner and blog_id and owner != blog_id and not use_view:
                        log(f"[실전용 참고글] 소유 계정({owner}) ≠ 로그인 계정({blog_id}) — "
                            f"수정 화면은 소유자만 열립니다. 발행 화면 복사로 전환합니다.")
                        use_view = True
                    if use_view:
                        # 수정 화면에서 이미지가 안 넘어올 때의 대비 경로
                        #   (검수용에서 검증된 '발행 화면 복사'와 동일하다)
                        from . import source_view
                        log("[실전용 참고글] 발행 화면에서 복사합니다")
                        src_obj = await source_view.open_source(ctx, content_url, log)
                        await src_obj.scan()
                        src_obj.from_view = True     # 발행 화면 = Range 복사 사용
                    else:
                        src_obj, _ = await edit_post.open_source_edit(
                            ctx, content_url, log, label="실전용 참고글")
                    cache[content_url] = src_obj
                return cache[content_url]
            return _get

        # ── 배치 처리 ────────────────────────────────────────────────
        #   탭을 5~6개(기본, 임의) 열어 전부 작성한 뒤 그 배치를 몰아서 발행한다.
        idx = 0
        while idx < total:
            size = args.batch if args.batch and args.batch > 0 else random.choice((5, 6))
            group = items[idx:idx + size]
            log("")
            log(f"──── 배치 {idx // max(1, size) + 1} — {idx + 1}~{idx + len(group)}번 "
                f"(탭 {len(group)}개 동시) ────")

            ready = []
            for k, item in enumerate(group):
                n = idx + k + 1
                content_url = ref_url if args.content_from == "ref" else item["blog_url"]
                get_src = make_get_src(content_url)
                try:
                    if args.mode == "create":
                        post = await create_one(ctx, get_src, blog_id, item, n, total,
                                                log, settings.out_dir, brand=brand)
                    else:
                        post = await convert_one(ctx, get_src, item, n, total,
                                                 log, settings.out_dir, args.chunk_imgs,
                                                 args.copy_mode, brand=brand)
                except Exception as exc:                       # noqa: BLE001
                    log(f"[{n}/{total}] ❌ 실패 — 이 글은 발행하지 않습니다.")
                    common = is_common_error(exc)
                    reason = stage_failed(log, brand, "post_build", exc,
                                          row=item["row"], no=n,
                                          scope="common" if common else "row")
                    log.event("post_failed", no=n, total=total, row=item["row"],
                              seq=item.get("seq", ""), campaign=item.get("campaign", ""),
                              blog_url=item.get("blog_url", ""),
                              product_url=item.get("product_url", ""),
                              stage="post_build", scope="common" if common else "row",
                              error=f"{type(exc).__name__}: {exc}"[:300], reason=reason)
                    failed_rows.append(item["row"])
                    # ★행 단위 오류 + --on-error skip → 이 건만 실패로 두고 계속한다.
                    #   공통 오류(로그인 만료·브라우저 종료·시트 접근 불가)면 남은 건을
                    #   줄줄이 실패시키지 않고 **여기서 멈춘다**(복구 후 이어서 한다).
                    if common:
                        log("       ⛔ 공통 오류입니다 — 남은 건을 계속하지 않고 "
                            "여기서 멈춥니다(재로그인/복구 후 이어서 실행하세요).")
                        log.event("run_blocked", reason=reason, row=item["row"],
                                  error=f"{type(exc).__name__}: {exc}"[:300])
                        raise
                    if args.on_error == "skip":
                        log("       (--on-error skip) 이 건만 건너뛰고 나머지를 계속합니다.")
                        await close_stray_pages(
                            ctx, [p for _, _, p in ready], list(cache.values()), log)
                        continue
                    log(f"       이 배치에서 작성해 둔 {len(ready)}건도 발행하지 않습니다.")
                    if published:
                        log(f"       ※ 앞 배치에서 발행된 {len(published)}건은 그대로 남아 "
                            f"있습니다: {[d['row'] for d in done]}")
                    raise
                ready.append((n, item, post))

            log("")
            log(f"[작성] 배치 {len(ready)}건 READY — 탭 {len(ready)}개가 열려 있습니다")

            if not args.publish:
                log("[발행] --publish 가 없어 발행하지 않습니다. 탭을 열어 둡니다.")
                idx += size
                continue

            for n, item, post in ready:
                wait = random.randint(min(args.delay_min, args.delay_max),
                                      max(args.delay_min, args.delay_max))
                log("")
                log(f"════ [{n}/{total}] 발행 — {wait}초 대기 후 ════")
                await asyncio.sleep(wait)
                url = await post.publish()      # 댓글 허용 OFF 확인이 publish 안에 있다
                log(f"[{n}/{total}] 댓글 허용 OFF 확인 · 발행 완료: {url}")
                log.event("published", no=n, total=total, url=url, row=item["row"],
                          seq=item.get("seq", ""), campaign=item.get("campaign", ""))
                published.append(url)
                done.append({"row": item["row"],
                             "url": url or item.get("blog_url", ""),
                             "product_url": item["product_url"]})

                # ★발행 + 제품링크 교체가 모두 끝난 행만 즉시 '완료' 표시한다.
                #   (중간에 끊겨도 성공한 행만 남는다)
                if cols:
                    try:
                        if args.mode == "create":
                            # ★검수용 랜딩은 아직 제품 링크를 실전 UTM 으로 바꾼 게 아니므로
                            #   '블로그 최하단 제품 링크 변경 여부'(K)에 O 를 남기지 않는다.
                            #   블로그 링크 열만 기록한다(2026-08-24 사용자 지시).
                            landing_sheet.write_blog_link_row(
                                settings.service_account_json, settings.spreadsheet_id,
                                item["row"], url, cols, log,
                                mark_changed=(args.ref_kind != "검수용"))
                        else:
                            landing_sheet.mark_row_done(
                                settings.service_account_json, settings.spreadsheet_id,
                                item["row"], url or item["blog_url"], cols, log)
                        log(f"[{n}/{total}] 시트 기록 완료 ({item['row']}행)")
                        stage(log, brand, "sheet_mark_done", "ok", "",
                              row=item["row"])
                    except Exception as exc:                   # noqa: BLE001
                        log(f"[{n}/{total}] ⚠ 시트 기록 실패({type(exc).__name__}: {exc}) "
                            f"— 발행은 끝났습니다. {item['row']}행을 직접 확인하세요")
                        stage_failed(log, brand, "sheet_mark_done", exc,
                                     row=item["row"])
                try:
                    await post.page.close()
                except Exception:                              # noqa: BLE001
                    pass

            idx += size

        log("")
        log(f"[완료] 실전용 전환 {total}건 · 발행 {len(published)}건")
        for u in published:
            log(f"        {u}")

        if done:
            log(f"[시트] 완료 표시된 행: {[d['row'] for d in done]}")
        if failed_rows:
            log(f"[완료] ⚠ 실패해서 건너뛴 행: {failed_rows} "
                f"(--on-error skip). 그 행들은 시트에 기록하지 않았습니다.")
        log.event("run_finished", ok=True, total=total, published=published,
                  brand=brand.id, failed_rows=failed_rows,
                  rows=[d["row"] for d in done])
        return 0 if not failed_rows else 3
    except Exception as exc:                                   # noqa: BLE001
        failed = True
        log(f"[오류] {exc}")
        log(traceback.format_exc())
        log.event("run_finished", ok=False, error=f"{type(exc).__name__}: {exc}",
                  brand=brand.id, failed_rows=failed_rows, published=published)
        for i, pg in enumerate(list(ctx.pages) if ctx else []):
            try:
                shot = settings.out_dir / f"v2_prod_error_{i}.png"
                await pg.screenshot(path=str(shot))
                log(f"[오류] 화면 저장: {shot}  (url={pg.url[:80]})")
            except Exception:                                  # noqa: BLE001
                pass
        return 1
    finally:
        if ctx is not None:
            if args.keep_open:
                log("[브라우저] --keep-open — Ctrl+C 로 종료하세요.")
                try:
                    while True:
                        await asyncio.sleep(3)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    pass
            elif not args.publish or failed:
                hold = min(args.hold, 120) if failed else args.hold
                log(f"[브라우저] 확인용으로 {hold}초 뒤 자동으로 닫습니다.")
                try:
                    await asyncio.sleep(hold)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    pass
            try:
                await ctx.close()
            except Exception:                                  # noqa: BLE001
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:                                  # noqa: BLE001
                pass


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        # ★브랜드/계정을 못 찾으면 시트도 브라우저도 건드리기 전에 멈춘다.
        brands.resolve(args.brand or "")
        args.account_obj = accounts.resolve(args.account) if args.account else None
    except Exception as exc:                                   # noqa: BLE001
        print(f"[오류] {exc}")
        return 2
    settings = resolve_headless(args, load_settings(account=args.account_obj))
    log = Log(settings.out_dir, tag="v2_prod", events=args.events)
    log(f"[로그] {log.path}")
    try:
        settings.check()
        brand = apply_brand(args, log)
        _show_account(args, log)
        log.event("run_started", mode="production", brand=brand.id,
                  brand_label=brand.title,
                  reference_sheet=brand.reference_sheet_id,
                  utm_sheet=brand.utm_sheet_id,
                  on_error=args.on_error,
                  account=args.account_obj.id if args.account_obj else "",
                  media=args.media, deficiency=args.deficiency, ref_kind=args.ref_kind,
                  mode_flag=args.mode, count=args.count, publish=bool(args.publish),
                  dry_run=bool(args.dry_run), log_path=str(log.path))
        return asyncio.run(main_async(args, settings, log))
    except KeyboardInterrupt:
        log("[중단] 사용자가 중단했습니다.")
        log.event("run_finished", ok=False, error="사용자 중단")
        return 130
    except Exception as exc:                                   # noqa: BLE001
        log(f"[오류] {exc}")
        log(traceback.format_exc())
        # ★UI(큐)가 '왜 실패했는지' 를 그대로 받아 볼 수 있게 이벤트로도 남긴다.
        log.event("run_finished", ok=False,
                  brand=brands.brand_id(getattr(args, "brand_obj", None))
                  or (args.brand or ""),
                  error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
