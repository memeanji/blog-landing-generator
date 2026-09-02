r"""블로그 랜딩 생성 — 단순 구조 재작성판.

    시트 조회 → 수동 로그인 → 기준글 수정화면 열기 → 새 글 탭 → 모바일 전환
    → 제목 → 컴포넌트 단위 복사/붙여넣기(+건별 검증) → 후처리 → 중앙정렬 → (--publish 시) 발행

실행 예
    .\.venv\Scripts\python.exe -m v2.run --list
    .\.venv\Scripts\python.exe -m v2.run --media gfa --deficiency "팔자 / 머니" --kind 검수용
    .\.venv\Scripts\python.exe -m v2.run --media gfa --deficiency "팔자 / 머니" --count 5 --publish

기본은 **1개 생성 + 발행 안 함(테스트 모드)**. --publish 를 붙여야 실제로 발행한다.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
import traceback

from .config import load_settings, resolve_headless
from .logger import Log
from . import (accounts, brands, browser, landing_sheet, session_store,
               sheets, source_view, writer)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="네이버 블로그 랜딩 생성")
    p.add_argument("--list", action="store_true", help="시트의 매체/결핍/랜딩 현황만 출력")
    # ★계정 — accounts.json 에 적어 둔 id/label/blog_id 아무거나. 주지 않으면 예전 그대로
    #   `playwright-profile` 하나를 쓴다(기존 CLI·자동화 동작 보존).
    p.add_argument("--brand", default="",
                   help="브랜드 (brands.json 의 id/label. 예: repurely / 닥터누센트). "
                        "비우면 기본 브랜드(리퓨어리) — 기준시트와 UTM 빌더가 한 세트로 바뀐다")
    p.add_argument("--account", default="",
                   help="사용할 네이버 계정 (accounts.json 의 id/label/blog_id). "
                        "세션은 sessions/<id>/ 에 계정별로 따로 보관된다")
    p.add_argument("--ref-tab",
                   help="기준랜딩 탭 이름. 생략하면 계정 설정(ref_tab) → 기본값 순으로 쓴다")
    p.add_argument("--media", help="매체 (gfa / 카모 / 메타 / 틱톡)")
    p.add_argument("--deficiency", help="결핍 (예: '팔자 / 머니')")
    p.add_argument("--kind", choices=list(sheets.KINDS), default="검수용",
                   help="검수용 / 실전용 (기본: 검수용)")
    p.add_argument("--count", type=int, default=1, help="생성 개수 (기본 1)")
    p.add_argument("--publish", action="store_true",
                   help="발행까지 진행(되돌릴 수 없음). 없으면 작성만 하고 멈춘다")
    p.add_argument("--dry-run", action="store_true",
                   help="시트 매칭만 확인하고 끝낸다(브라우저를 켜지 않는다)")
    p.add_argument("--events", action="store_true",
                   help="진행 상황을 `@@EVENT {json}` 한 줄로도 출력한다(GUI 용). "
                        "사람이 보는 로그는 그대로 나온다")
    p.add_argument("--url", help="시트를 무시하고 이 참고 URL 을 직접 사용")
    p.add_argument("--product-url",
                   help="본문 맨 아래에 넣을 제품 링크 URL "
                        "(예: https://repurely.com/surl/P/116). 기준글에 제품링크 이미지가 있으면 필수")
    p.add_argument("--blog-id", help="내 블로그 ID 직접 지정")
    p.add_argument("--relogin", action="store_true",
                   help="저장된 네이버 세션을 지우고 반드시 직접 로그인한다(계정 바꿀 때)")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None,
                   help="창을 띄우지 않고 돌린다. 기본: 검수용이면 headless, "
                        "실전용이면 창 띄움. 창을 보려면 --no-headless")
    p.add_argument("--sheet-media",
                   help="발행 URL 을 기록할 `랜딩` 탭 매체 (예: 카카오모먼트)")
    p.add_argument("--sheet-date",
                   help="발행 URL 을 기록할 `랜딩` 탭 날짜 (예: 821)")
    # ★같은 날짜에 그룹이 여럿이면(0826 = 올레놀샷_목주름 30행 + 레모니티_흑자 20행)
    #   매체+날짜만으로는 앞 그룹 빈칸에 뒷 그룹 랜딩이 들어간다(2026-08-26 사용자 지시).
    #   그래서 C열 제품_결핍 + utm_campaign 접두사로 한 번 더 좁힌다.
    p.add_argument("--sheet-campaign",
                   help="기록 대상 utm_campaign 접두사 (예: k_i_b_l_m_0826). "
                        "이 접두사로 시작하는 행에만 기록한다")
    p.add_argument("--sheet-product",
                   help="기록 대상 C열 제품_결핍 (예: 레모니티_흑자). 생략하면 "
                        "--deficiency 앞 단어(흑자/목/기미…)로 자동 대조한다. "
                        "끄려면 --sheet-product \"\" ")
    p.add_argument("--batch", type=int, default=0,
                   help="한 번에 열어 둘 탭 수. 예: 5 면 5개 작성→발행→다음 5개. "
                        "0(기본)이면 전부 열어 두고 마지막에 몰아서 발행")
    p.add_argument("--delay-min", type=int, default=DELAY_MIN,
                   help=f"발행 간격 최소 초(기본 {DELAY_MIN})")
    p.add_argument("--delay-max", type=int, default=DELAY_MAX,
                   help=f"발행 간격 최대 초(기본 {DELAY_MAX})")
    p.add_argument("--inspect", action="store_true",
                   help="기준글 구조만 분석하고 끝낸다(새 글을 만들지 않음)")
    p.add_argument("--keep-open", action="store_true", help="끝나도 브라우저를 닫지 않는다")
    p.add_argument("--hold", type=int, default=300,
                   help="발행하지 않는 모드에서 결과 확인용으로 창을 열어 둘 초(기본 300)")
    return p.parse_args(argv)


def cmd_list(settings, log) -> int:
    rows = sheets.load_rows(settings.service_account_json, settings.spreadsheet_id)
    log(f"[시트] `{sheets.SHEET_NAME}` 데이터 {len(rows)}행")
    media = None
    for r in rows:
        if r["media"] != media:
            media = r["media"]
            log(f"── {media} ──")
        mark = lambda k: ("○" if sheets.is_url(r[k]) else                  # noqa: E731
                          ("·" if not r[k] else f"준비중({r[k][:10]})"))
        log(f"   {r['row']:>3}행  {r['deficiency']:<16} 검수용 {mark('검수용'):<12} "
            f"실전용 {mark('실전용')}")
    return 0


def _suggest_for_account(settings, blog_id: str, log) -> None:
    """로그인한 계정으로 바로 쓸 수 있는 시트 조합을 알려준다."""
    try:
        rows = sheets.load_rows(settings.service_account_json, settings.spreadsheet_id)
    except Exception:                                          # noqa: BLE001
        return
    hits = [(r, k) for r in rows for k in sheets.KINDS
            if sheets.is_url(r[k]) and f"/{blog_id}/" in r[k]]
    if not hits:
        log(f"       ({blog_id} 계정이 소유한 기준글은 시트에 없습니다)")
        return
    log(f"       {blog_id} 계정으로 바로 쓸 수 있는 조합:")
    for r, k in hits[:12]:
        log(f"         --media {r['media']} --deficiency \"{r['deficiency']}\" --kind {k}")


# 발행 간격 — 사람이 올리는 것처럼 들쭉날쭉하게(초). 매 건 이 범위에서 랜덤으로 뽑는다.
DELAY_MIN, DELAY_MAX = 10, 50


async def build_ready(ctx, src, blog_id, no, total, log, out_dir,
                      product_url: str = ""):
    """새 글 1개를 READY 까지만 만든다(발행하지 않는다). 탭은 열어 둔 채 반환.

    ★탭은 글마다 새로 연다(writer.open_write). 같은 탭을 재사용하면 앞서 만든 글이
      통째로 날아간다(2026-08-20 사고).
    """
    log("")
    log(f"════ [{no}/{total}] 새 글 작성 ════")
    post = await writer.open_write(ctx, blog_id, log)

    await post.switch_to_mobile()
    await post.ensure_mobile()
    await post.type_title(src.title)

    # ★본문은 '한 번에' 복사/붙여넣기한다(2026-08-21 사용자 시연 방식).
    #   컴포넌트를 나눠 여러 번 복사하면 네이버가 [출처] 문구를 붙인다.
    await src.copy_all()
    await post.page.bring_to_front()
    await post.prepare_body_caret()

    # ★글마다 반드시 초기화한다. src 를 여러 글이 공유하므로 앞 글에서 올려둔 값이 남으면
    #   다음 글의 검증이 '이미지 4/5' 로 실패한다(2026-08-21 사고).
    src.extra_images = 0
    await post.paste_all(src)

    await post.verify_body(src)
    if product_url:
        await post.append_product_link(product_url)
        src.extra_images = 1
    await post.cleanup_promo()
    await post.center_all()
    await post.verify_body(src, check_texts=False)   # 후처리/정렬 뒤 최종 재검증
    await post.shot(f"v2_{no}_ready", out_dir)
    log(f"[{no}/{total}] 작성 완료(READY) — 탭을 열어 둡니다")
    log.event("post_ready", no=no, total=total)
    return post


async def publish_all(posts: list, log, lo: int = DELAY_MIN, hi: int = DELAY_MAX,
                      offset: int = 0, grand: int = 0) -> list[str]:
    """READY 상태로 열어 둔 글들을 **랜덤 간격으로** 차례차례 발행한다."""
    total = grand or len(posts)
    published = []
    for n, post in enumerate(posts, start=1):
        wait = random.randint(min(lo, hi), max(lo, hi))
        log("")
        log(f"════ [{offset + n}/{total}] 발행 — {wait}초 대기 후 ════")
        await asyncio.sleep(wait)
        url = await post.publish()
        log(f"[발행] 완료 — {url}")
        log.event("published", no=offset + n, total=total, url=url)
        published.append(url)
        try:
            await post.page.close()
        except Exception:                                      # noqa: BLE001
            pass
    return published


def _check_account(args, blog_id: str, log) -> None:
    """선택한 계정과 실제 로그인된 계정이 다르면 **작성 전에** 멈춘다.

    계정이 여러 개가 되면 이게 없을 때 엉뚱한 블로그에 글이 올라간다(되돌리기 어렵다).
    `--account` 를 쓰지 않으면 아무 것도 하지 않는다(기존 동작 그대로).
    """
    acc = getattr(args, "account_obj", None)
    if not acc or not acc.blog_id or not blog_id:
        return
    if blog_id.casefold() == acc.blog_id.casefold():
        log(f"[계정] 확인 완료 — 선택({acc.title}) = 로그인({blog_id})")
        return
    raise RuntimeError(
        f"선택한 계정({acc.title} / {acc.blog_id})과 실제 로그인된 계정({blog_id})이 "
        f"다릅니다. 잘못된 블로그에 글이 올라가는 것을 막기 위해 중단합니다. "
        f"→ `--account {acc.id} --relogin` 으로 그 계정에 직접 로그인하세요.")


def _apply_brand(args, log):
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
    log.event("stage", brand=brand.id, stage="brand_config", status="ok",
              brand_label=brand.title,
              reference_sheet=brand.reference_sheet_id,
              utm_sheet=brand.utm_sheet_id)
    return brand


def _apply_account(args, log) -> None:
    """계정 설정을 이번 실행에 반영한다(기준랜딩 탭 · 세션 현황 표시)."""
    acc = getattr(args, "account_obj", None)
    if acc:
        log(f"[계정] {acc.title} (id={acc.id}"
            + (f" · blog_id={acc.blog_id}" if acc.blog_id else "") + ")")
        info = session_store.describe(acc)
        log(f"[계정] 프로필 = {info['profile']}")
        log("[계정] 저장 세션 = " + (
            f"있음 (쿠키 {info['cookies']}개 · 저장 {info['saved_at']})"
            if info["state_exists"] else
            "없음 — 로그인 창이 한 번 뜹니다(로그인하면 다음부터는 창 없이 진행)"))
    brand = getattr(args, "brand_obj", None) or sheets.active_brand()
    tab = getattr(args, "ref_tab", None) or (acc.tab_for_brand(brand) if acc else "")
    if not tab and acc and acc.ref_tab:
        log(f"[시트] 계정 탭 {acc.ref_tab!r} 은 다른 브랜드의 탭이라 쓰지 않습니다 "
            f"— {brand.title} 기본 탭을 씁니다")
    if tab:
        sheets.set_tab(tab)
    log(f"[시트] 기준랜딩 탭 = {sheets.active_tab()!r} ({brand.reference_title})")
    log.event("stage", brand=brand.id, stage="reference_sheet_selected", status="ok",
              sheet=brand.reference_sheet_id, tab=sheets.active_tab())


def _sheet_product(args) -> str:
    """C열 제품_결핍 대조에 쓸 키워드.
    --sheet-product 를 주면 그 값, 안 주면 --deficiency 앞 단어(`흑자 / 머니` → `흑자`).
    빈 문자열이면 대조하지 않는다."""
    if args.sheet_product is not None:
        return args.sheet_product.strip()
    if not args.deficiency:
        return ""
    return args.deficiency.split("/")[0].strip()


async def main_async(args, settings, log) -> int:
    sheet_product = _sheet_product(args)
    if args.sheet_media and args.sheet_date:
        log(f"[시트] 기록 대상 필터 — 매체 {args.sheet_media} / 날짜 {args.sheet_date}"
            f" / 제품_결핍 ~{sheet_product or '(대조 안 함)'}"
            f" / 캠페인 {args.sheet_campaign or '(대조 안 함)'}")
    # 1. 시트 조회
    if args.url:
        ref_url = args.url
        log(f"[시트] 건너뜀 — URL 직접 지정: {ref_url}")
    else:
        if not args.media or not args.deficiency:
            log("[오류] --media 와 --deficiency 가 필요합니다(또는 --url).")
            return 2
        ref = sheets.find_reference(settings.service_account_json, settings.spreadsheet_id,
                                    args.media, args.deficiency, args.kind)
        ref_url = ref.url
        log(f"[시트] {ref.media} / {ref.deficiency} / {ref.kind} 매칭 (시트 {ref.row}행)")
        # ★제품 상세 URL 은 시트 값을 쓰고, --product-url 을 준 경우에만 그것으로 덮어쓴다.
        if ref.product_url and not args.product_url:
            args.product_url = ref.product_url
            log(f"[시트] 제품 상세 URL — {ref.product_url}")
        elif not ref.product_url:
            log("[시트] 제품 상세 URL 컬럼이 비어 있습니다(있으면 자동으로 씁니다)")

    # ★브라우저를 켜기 전에 '무엇을 · 어디에' 쓸지만 확인하고 끝내는 모드.
    if args.dry_run:
        log("")
        log(f"[dry-run] 참고 랜딩 = {ref_url}")
        log(f"[dry-run] 제품 링크 = {args.product_url or '(없음)'}")
        rows: list[int] = []
        if args.sheet_media and args.sheet_date:
            rows = landing_sheet.find_target_rows(
                settings.service_account_json, settings.spreadsheet_id,
                args.sheet_media, args.sheet_date, log, need=max(1, args.count),
                campaign=args.sheet_campaign or "", product=sheet_product)
        else:
            log("[dry-run] --sheet-media / --sheet-date 가 없어 기록 대상은 확인하지 않았습니다")
        log(f"[dry-run] 생성 예정 {max(1, args.count)}건 · "
            f"발행 {'예' if args.publish else '아니오'}")
        log("[dry-run] 브라우저를 켜지 않았습니다.")
        log.event("run_finished", ok=True, dry_run=True, made=0, published=[],
                  target_rows=rows, ref_url=ref_url)
        return 0

    pw = ctx = None
    published: list[str] = []
    failed = False
    try:
        # 2. 브라우저 + 수동 로그인
        # ★로그인이 필요할 때만 창을 띄우고, 그 뒤로는 headless 로 진행한다.
        pw, ctx, logged = await browser.open_session(settings, log, relogin=args.relogin)
        blog_id = args.blog_id or logged or await browser.resolve_blog_id(ctx, settings, log)
        log(f"[블로그] 새 글을 작성할 계정 = {blog_id}")
        _check_account(args, blog_id, log)

        # 3. 기준글 수정화면(읽기 전용으로만 사용)
        #    ★수정 화면은 그 글의 소유 계정으로 로그인해야 열린다.
        owner = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/", ref_url)
        owner = owner.group(1) if owner else ""
        # ★기준글을 '발행 화면'에서 읽으므로 더 이상 소유 계정으로 로그인할 필요가 없다.
        #   (수정 화면을 쓰던 시절의 제약이었다. 2026-08-21)
        if owner and owner != blog_id:
            log(f"[참고] 기준글 소유 계정({owner}) ≠ 로그인 계정({blog_id}) — "
                f"발행 화면에서 읽으므로 그대로 진행합니다. 새 글은 {blog_id} 에 작성됩니다.")
        # ★기록 대상 행을 **발행 전에** 확인한다 — 발행해 놓고 쓸 자리가 없으면 곤란하다.
        if args.publish and args.sheet_media and args.sheet_date:
            landing_sheet.find_target_rows(
                settings.service_account_json, settings.spreadsheet_id,
                args.sheet_media, args.sheet_date, log, need=max(1, args.count),
                campaign=args.sheet_campaign or "", product=sheet_product)

        src = await source_view.open_source(ctx, ref_url, log)
        await src.scan()
        if src.product_image and not args.product_url:
            raise RuntimeError(
                "기준글 맨 아래에 제품 링크 이미지가 있습니다. 붙여넣기로는 링크가 넘어오지 "
                "않으니 --product-url \"https://repurely.com/surl/P/000\" 로 제품 URL 을 "
                "지정하세요.")
        if args.inspect:
            log("[진단] 기준글 구조만 분석했습니다(새 글 없음).")
            return 0

        # 4~5. 작성 → 발행. --batch 만큼 끊어서 (탭을 너무 많이 열어 두지 않도록)
        total = max(1, args.count)
        log.event("plan", total=total, ref_url=ref_url, blog_id=blog_id)
        size = args.batch if args.batch and args.batch > 0 else total
        made = 0
        for start in range(0, total, size):
            group = list(range(start + 1, min(start + size, total) + 1))
            if size < total:
                log("")
                log(f"──── 배치 {start // size + 1} — {group[0]}~{group[-1]}번 "
                    f"(탭 {len(group)}개) ────")

            posts = []
            for no in group:
                try:
                    posts.append(await build_ready(ctx, src, blog_id, no, total,
                                                   log, settings.out_dir,
                                                   args.product_url or ""))
                except Exception as exc:                       # noqa: BLE001
                    log.event("post_failed", no=no, total=total,
                              error=f"{type(exc).__name__}: {exc}")
                    log(f"[{no}/{total}] ❌ 실패 — 안전을 위해 중단합니다(발행하지 않음).")
                    log(f"       이 배치에서 작성해 둔 {len(posts)}건도 발행하지 않습니다.")
                    if published:
                        log(f"       ※ 앞 배치에서 이미 발행된 {len(published)}건은 "
                            f"그대로 남아 있습니다: {published}")
                    raise
            made += len(posts)

            log("")
            log(f"[작성] {len(posts)}건 READY — 탭 {len(posts)}개가 열려 있습니다")

            if not args.publish:
                log("[발행] --publish 가 없어 발행하지 않습니다. 탭을 열어 둡니다.")
                continue
            published += await publish_all(posts, log, args.delay_min, args.delay_max,
                                           offset=start, grand=total)

        log("")
        log(f"[완료] 작성 {made}건 · 발행 {len(published)}건")
        for u in published:
            log(f"        {u}")

        # ★발행이 전부 끝난 뒤 한꺼번에 기록한다(중간에 쓰면 실패 시 어긋난다).
        if published and args.sheet_media and args.sheet_date:
            rep = landing_sheet.write_blog_links(
                settings.service_account_json, settings.spreadsheet_id,
                args.sheet_media, args.sheet_date, published, log,
                campaign=args.sheet_campaign or "", product=sheet_product)
            log(f"[시트] 기록 완료 — {rep['written']}건 (행 {rep['rows']})")
            log.event("sheet_written", written=rep["written"], rows=rep["rows"])
        elif published:
            log("[시트] --sheet-media / --sheet-date 가 없어 기록하지 않았습니다")
        log.event("run_finished", ok=True, made=made, published=published)
        args.__dict__["_done_ok"] = True   # ★끝까지 마쳤다는 표시
        return 0
    except Exception as exc:                                   # noqa: BLE001
        # ★대기(finally) 앞에서 먼저 남긴다 — 안 그러면 에러가 --hold 초 동안 안 보인다.
        failed = True
        log(f"[오류] {exc}")
        log(traceback.format_exc())
        log.event("run_finished", ok=False, error=f"{type(exc).__name__}: {exc}",
                  published=published)
        for i, pg in enumerate(list(ctx.pages) if ctx else []):
            try:
                shot = settings.out_dir / f"v2_error_{i}.png"
                await pg.screenshot(path=str(shot))
                log(f"[오류] 화면 저장: {shot}  (url={pg.url[:80]})")
            except Exception:                                  # noqa: BLE001
                pass
        return 1
    finally:
        if ctx is not None:
            if args.keep_open:
                log("[브라우저] --keep-open — 창을 계속 열어 둡니다. Ctrl+C 로 종료하세요.")
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
        # ★브랜드/계정을 못 찾으면 브라우저도 시트도 건드리기 전에 바로 멈춘다.
        brands.resolve(args.brand or "")
        # ★계정은 **기준랜딩 탭 → 키** 순으로 찾는다. 화면이 보낸 값이 이 PC 의
        #   계정 이름과 달라도(화면에는 계정 목록이 없다) 어긋나지 않는다.
        args.account_obj = accounts.resolve_for(
            args.account, getattr(args, "ref_tab", "") or "",
            args.brand or None) if (args.account
                                    or getattr(args, "ref_tab", "")) else None
    except Exception as exc:                                   # noqa: BLE001
        print(f"[오류] {exc}")
        return 2
    settings = resolve_headless(args, load_settings(account=args.account_obj))
    log = Log(settings.out_dir, events=args.events)
    log(f"[로그] {log.path}")
    try:
        settings.check()
        brand = _apply_brand(args, log)
        _apply_account(args, log)
        log.event("run_started", mode="review", brand=brand.id,
                  brand_label=brand.title,
                  reference_sheet=brand.reference_sheet_id,
                  utm_sheet=brand.utm_sheet_id,
                  account=args.account_obj.id
                  if args.account_obj else "", media=args.media or "",
                  deficiency=args.deficiency or "", kind=args.kind,
                  count=max(1, args.count), publish=bool(args.publish),
                  dry_run=bool(args.dry_run), log_path=str(log.path))
        if args.list:
            return cmd_list(settings, log)
        return asyncio.run(main_async(args, settings, log))
    except KeyboardInterrupt:
        log("[중단] 사용자가 중단했습니다.")
        log.event("run_finished", ok=False, error="사용자 중단")
        return 130
    except Exception as exc:                                   # noqa: BLE001
        # ★일을 다 마친 뒤 **브라우저를 닫는 과정**에서 나는 오류는 실패가 아니다.
        #   (사람이 창을 직접 닫으면 여기서 파이프 오류가 난다. 예전에는 이것 때문에
        #    발행·시트 기록까지 다 끝난 실행이 '실패' 로 기록됐다.)
        if getattr(args, "_done_ok", False):
            log(f"[정리] 창을 닫는 중 문제가 있었지만 작업은 끝났습니다 "
                f"({type(exc).__name__}). 결과에는 영향이 없습니다.")
            return 0
        log(f"[오류] {exc}")
        log(traceback.format_exc())
        log.event("run_finished", ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
