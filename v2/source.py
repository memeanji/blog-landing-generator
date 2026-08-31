"""기준글(참고 랜딩) 읽기 — '수정' 화면의 스마트에디터 구조를 그대로 읽고 컴포넌트 단위로 복사한다.

★원본 글은 절대 수정/저장하지 않는다. 이 모듈은 읽기(scan)와 선택/복사(copy)만 한다.
★실측 확정(2026-08-20)
  · 수정 화면은 URL 직접 진입: blog.naver.com/{id}?Redirect=Update&logNo={no}
  · 실제 에디터 내용은 about:blank 중첩 iframe 안 → frame.url 로 거르면 못 찾는다(browser.find_editor_frame).
  · Ctrl+C 는 브라우저 창이 OS 포커스를 가져야만 시스템 클립보드에 닿는다.
    → document.execCommand('copy') 로 복사한다(렌더러 실행, 창 포커스 무관).
  · Ctrl+A 금지 — activeElement 가 body 면 페이지 전체(제목·메뉴)가 잡힌다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import browser

PLACEHOLDER_TEXTS = ("추가할 컴포넌트를 선택하세요", "컴포넌트를 선택하세요")
MIN_TEXT_CHARS = 2          # 이보다 짧고 이미지도 없으면 본문 컴포넌트로 보지 않는다


@dataclass
class Component:
    idx: int
    kind: str          # text | image | mixed
    chars: int
    imgs: int
    head: str
    cls: str
    links: list = None          # [{href, text, hasImg}] — 이미지에 걸린 제품 링크 등

    @property
    def image_link(self) -> str:
        """이미지에 걸려 있는 하이퍼링크(제품 링크). 없으면 빈 문자열."""
        for l in (self.links or []):
            if l.get("hasImg"):
                return l.get("href", "")
        return ""


def parse_post_url(post_url: str) -> tuple[str, str]:
    """blog.naver.com/{id}/{logNo} → (블로그ID, 글번호)."""
    m = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d+)", post_url or "")
    if not m:
        m = re.search(r"blogId=([A-Za-z0-9_\-]+).*?logNo=(\d+)", post_url or "", re.I)
    if not m:
        raise RuntimeError(f"참고 URL 에서 블로그ID/글번호를 읽지 못했습니다: {post_url!r}")
    return m.group(1), m.group(2)


def edit_url(post_url: str) -> str:
    """blog.naver.com/{id}/{logNo} → 수정 화면 URL."""
    blog_id, log_no = parse_post_url(post_url)
    return (f"https://blog.naver.com/{blog_id}?Redirect=Update&"
            f"widgetTypeCall=true&noTrackingCode=true&directAccess=false&logNo={log_no}")


# ── 컴포넌트 판별 ──────────────────────────────────────────────────────
#   ★index 로 판단하지 않는다. 각 컴포넌트의 글자수/이미지수/종류만 본다.
#     제외: 제목(.se-documentTitle) · 자리표시자 · 글자도 이미지도 없는 빈 컴포넌트
SCAN_JS = r"""(opt) => {
     document.querySelectorAll('[data-v2-idx]').forEach(e => e.removeAttribute('data-v2-idx'));

     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content')
               || document.body;

     // 최상위 컴포넌트만(중첩된 se-component 는 부모만 잡는다)
     const comps = Array.from(root.querySelectorAll('.se-component')).filter(el => {
       const par = el.parentElement && el.parentElement.closest('.se-component');
       return !par || !root.contains(par);
     });

     // ★제목은 제목 컴포넌트 '안의 문단'에서만 읽는다.
     //   .se-documentTitle 통째로 innerText 를 읽으면 뷰어 화면에서
     //   '민 지 ・ 2시간 전 URL 복사 통계 …' 같은 주변 UI 가 섞여 들어온다.
     //   같은 제목이 .se-text-paragraph 와 .se-title-text 양쪽에 잡혀 두 번 들어가던 문제가 있어
     //   제목 컴포넌트 안쪽을 먼저 쓰고, 중복 문구는 걸러낸다.
     let titleParas = Array.from(document.querySelectorAll('.se-documentTitle .se-text-paragraph'));
     if (!titleParas.length) {
       titleParas = Array.from(document.querySelectorAll(
           '.se-documentTitle .se-title-text, .se-title-text'));
     }
     const seenTitle = [];
     titleParas.forEach(e => {
       const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
       if (t && seenTitle.indexOf(t) < 0) seenTitle.push(t);
     });
     const titleTxt = seenTitle.join(' ').trim();

     const info = [];
     let idx = 0;
     comps.forEach(el => {
       const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
       const imgs = el.querySelectorAll('img').length;
       const cls = (el.className || '').toString().replace(/\s+/g, ' ').trim();
       const isTitle = el.classList.contains('se-documentTitle')
                    || !!el.closest('.se-documentTitle')
                    || !!el.querySelector('.se-documentTitle');
       const isPh = /se-component-add|se-placeholder/i.test(cls)
                 || opt.ph.some(t => txt.indexOf(t) >= 0);
       // ★링크가 걸린 이미지(제품 링크)는 복사해 봐야 링크가 안 넘어온다.
       //   에디터 내부 모델에만 주소가 있어서 DOM 에는 배지(span.se-image-link-icon)만 보인다.
       //   → 복사하지 않고, 나중에 본문 맨 아래에 URL 을 타이핑해 카드로 다시 만든다.
       const isProductLink = !!el.querySelector('.se-image-link-icon');
       const keep = !isTitle && !isPh && !isProductLink
                 && (imgs > 0 || txt.length >= opt.minChars);
       let kind = 'text';
       if (imgs > 0) kind = (txt.length >= 10 ? 'mixed' : 'image');
       const why = isTitle ? '제목'
                 : (isPh ? '자리표시자'
                 : (isProductLink ? '제품링크(URL로 재생성)'
                 : (keep ? '' : '내용없음')));
       // 이미지에 걸린 하이퍼링크는 붙여넣기로 안 넘어올 수 있어 따로 뽑아 둔다.
       const links = Array.from(el.querySelectorAll('a[href]')).map(a => ({
         href: a.getAttribute('href') || '',
         text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 30),
         hasImg: !!a.querySelector('img'),
       })).filter(l => l.href && !/^#|^javascript:/i.test(l.href));
       if (keep) {
         el.setAttribute('data-v2-idx', String(idx));
         info.push({idx: idx, kind: kind, chars: txt.length, imgs: imgs,
                    head: txt.slice(0, 40), cls: cls.slice(0, 46), links: links,
                    html: imgs > 0 ? (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 700) : ''});
         idx += 1;
       } else {
         info.push({idx: -1, kind: why, chars: txt.length, imgs: imgs,
                    head: txt.slice(0, 40), cls: cls.slice(0, 46), links: links, html: ''});
       }
     });
     const productLinks = comps.filter(el => el.querySelector('.se-image-link-icon')).length;
     return {title: titleTxt, total: comps.length, items: info,
             productLinks: productLinks};
   }"""


COPY_JS = r"""(idx) => {
     const el = document.querySelector('[data-v2-idx="' + idx + '"]');
     if (!el) return {ok: false, why: 'data-v2-idx=' + idx + ' 컴포넌트가 사라짐'};
     el.scrollIntoView({block: 'center'});
     const r = document.createRange();
     r.setStartBefore(el);
     r.setEndAfter(el);
     const s = window.getSelection();
     s.removeAllRanges();
     s.addRange(r);
     const holder = document.createElement('div');
     holder.appendChild(r.cloneContents());
     const txt = (s.toString() || '').replace(/\s+/g, ' ').trim();
     const imgs = holder.querySelectorAll('img').length;
     let copied = false, err = '';
     try { copied = document.execCommand('copy'); } catch (e) { err = String(e); }
     return {ok: true, copied: copied, err: err, chars: txt.length, imgs: imgs, text: txt};
   }"""


class SourcePost:
    def __init__(self, page, frame, log) -> None:
        self.page = page
        self.frame = frame
        self.log = log
        self.title = ""
        self.components: list[Component] = []
        self.product_link_count = 0           # 제품링크 이미지 개수(복사 제외, URL 로 재생성)

    async def _refresh_frame(self):
        self.frame = await browser.fresh(self.page, self.frame, self.log, "기준글")
        return self.frame

    LINK_PROBE_JS = r"""() => {
         const out = [];
         document.querySelectorAll('[data-v2-idx]').forEach(el => {
           if (!el.querySelector('img')) return;
           const idx = el.getAttribute('data-v2-idx');
           const img = el.querySelector('img');
           const attrs = [];
           el.querySelectorAll('*').forEach(e => {
             Array.from(e.attributes || []).forEach(a => {
               if (/link|href|url/i.test(a.name) || /link/i.test(a.value.slice(0, 40))) {
                 attrs.push(e.tagName.toLowerCase() + '[' + a.name + ']=' + a.value.slice(0, 160));
               }
             });
           });
           out.push({
             idx: idx,
             anchors: el.querySelectorAll('a').length,
             imgSrc: (img ? (img.getAttribute('src') || '') : '').slice(0, 100),
             imgAttrs: img ? Array.from(img.attributes).map(a => a.name + '=' + a.value.slice(0, 90)) : [],
             linkAttrs: attrs.slice(0, 12),
             text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
           });
         });
         return out;
       }"""

    async def probe_links(self) -> list:
        """이미지 컴포넌트에서 링크가 어떤 형태로 저장돼 있는지 그대로 찍는다."""
        fr = await self._refresh_frame()
        rows = await fr.evaluate(self.LINK_PROBE_JS)
        for r in rows:
            self.log(f"   ── 이미지 컴포넌트 #{r['idx']} · a태그 {r['anchors']}개 · "
                     f"text={r['text']!r}")
            self.log(f"      img.src={r['imgSrc']!r}")
            for a in r["imgAttrs"]:
                self.log(f"      img {a}")
            for a in r["linkAttrs"]:
                self.log(f"      link속성 {a}")
        return rows

    async def dump_image_html(self) -> None:
        """이미지 컴포넌트의 실제 HTML 을 그대로 남긴다(링크가 어디 붙어 있는지 확인용)."""
        for it in getattr(self, "_scan_raw", []):
            if it.get("html"):
                self.log(f"   ── 컴포넌트 #{it['idx']} ({it['cls']}) ──")
                self.log(f"      {it['html']}")

    async def scan(self) -> None:
        fr = await self._refresh_frame()
        res = await fr.evaluate(SCAN_JS, {"minChars": MIN_TEXT_CHARS,
                                          "ph": list(PLACEHOLDER_TEXTS)})
        self.title = res["title"]
        self.components = [
            Component(idx=it["idx"], kind=it["kind"], chars=it["chars"],
                      imgs=it["imgs"], head=it["head"], cls=it["cls"],
                      links=it.get("links") or [])
            for it in res["items"] if it["idx"] >= 0
        ]
        for it in res["items"]:
            mark = (f"#{it['idx']:<3}{it['kind']}" if it["idx"] >= 0
                    else f"제외({it['kind']})")
            self.log(f"      · {mark:<16} {it['chars']:>4}자 이미지 {it['imgs']}개 "
                     f"{it['head']!r}")
            for l in (it.get("links") or []):
                self.log(f"            🔗 href={l['href'][:80]!r} "
                         f"text={l['text']!r} 이미지링크={l['hasImg']}")
        if not self.title:
            raise RuntimeError("기준글 제목(.se-documentTitle)을 읽지 못했습니다.")
        if not self.components:
            raise RuntimeError("기준글에서 본문 컴포넌트를 하나도 찾지 못했습니다.")
        self.log(f"[기준글] 제목 추출 완료 — {self.title!r}")
        self.log(f"[기준글] 본문 컴포넌트 {len(self.components)}개 / "
                 f"이미지 {self.total_images}개 / 글자 {self.total_chars}자")
        self._scan_raw = res["items"]
        self.product_link_count = res.get("productLinks", 0)
        if self.product_link_count:
            self.log(f"[기준글] 제품링크 이미지 {self.product_link_count}개 — 복사하지 않고 "
                     "본문 맨 아래에 URL 을 입력해 카드로 다시 만듭니다")

    @property
    def total_images(self) -> int:
        return sum(c.imgs for c in self.components)

    @property
    def total_chars(self) -> int:
        return sum(c.chars for c in self.components)

    async def focus_once(self) -> None:
        """DOM 포커스 확보용 클릭 — 텍스트 컴포넌트에만 한다(이미지를 누르면 이미지가 선택된다).
        이 클릭이 만든 캐럿은 뒤이은 Range 선택이 덮어쓴다. 원본 내용은 바뀌지 않는다."""
        fr = await self._refresh_frame()
        target = next((c for c in self.components if c.kind == "text"), None)
        if target is None:
            self.log("[기준글] ⚠ 텍스트 컴포넌트가 없어 포커스 클릭을 건너뜁니다")
            return
        try:
            await fr.locator(f'[data-v2-idx="{target.idx}"]').first.click(
                timeout=5000, position={"x": 20, "y": 8})
            await self.page.wait_for_timeout(250)
            self.log(f"[기준글] 포커스 클릭 완료 (컴포넌트 #{target.idx})")
        except Exception as exc:                               # noqa: BLE001
            self.log(f"[기준글] ⚠ 포커스 클릭 실패({type(exc).__name__}) — 그대로 진행")

    async def copy(self, comp: Component) -> dict:
        """컴포넌트 1개를 Range 로 선택해 클립보드에 넣는다. 기대값을 돌려준다."""
        # 백그라운드 탭에서는 클립보드 쓰기가 막힐 수 있어 복사 직전 앞으로 가져온다.
        await self.page.bring_to_front()
        fr = await self._refresh_frame()
        res = await fr.evaluate(COPY_JS, comp.idx)
        if not res.get("ok"):
            raise RuntimeError(f"컴포넌트 #{comp.idx} 선택 실패 — {res.get('why')}")
        if res["imgs"] != comp.imgs:
            raise RuntimeError(f"컴포넌트 #{comp.idx} 선택 검증 실패 — "
                               f"이미지 {res['imgs']}개 ≠ 예상 {comp.imgs}개")
        if not res.get("copied"):
            raise RuntimeError(f"컴포넌트 #{comp.idx} execCommand('copy') 실패 "
                               f"({res.get('err') or '반환 false'})")
        return res


# 발행 화면에서 이미지 링크를 읽는다 — 수정 화면에는 <a href> 가 없고
#   에디터 내부 모델에만 들어 있어서(배지 span.se-image-link-icon 만 보인다) DOM 으로는 못 읽는다.
VIEW_LINKS_JS = r"""() => {
     const imgs = Array.from(document.querySelectorAll(
         '.se-main-container img.se-image-resource, .se-main-container .se-image img,'
       + ' .se-component.se-image img'));
     return imgs.map(img => {
       const a = img.closest('a');
       if (!a) return {href: '', rawHref: '', attrs: []};
       const attrs = Array.from(a.attributes || [])
         .map(at => at.name + '=' + (at.value || '').slice(0, 400));
       let href = a.getAttribute('href') || '';
       if (!href || href === '#') {
         // ★href 가 '#' 이면 실제 주소는 data-* / onclick 에 들어 있다.
         for (const at of a.attributes) {
           if (at.name === 'href') continue;
           const m = (at.value || '').match(/https?:\/\/[^"'\\s)\]]+/);
           if (m && !/pstatic\.net|blogfiles|dthumb|static\.naver/.test(m[0])) {
             href = m[0].replace(/\u002F/g, '/').replace(/&amp;/g, '&');
             break;
           }
         }
       }
       return {href: href, rawHref: a.getAttribute('href') || '', attrs: attrs};
     });
   }"""


async def fetch_image_links(ctx, post_url: str, log) -> list[str]:
    """발행된 기준글을 열어 이미지 순서대로 걸린 링크 URL 을 읽는다(읽기 전용)."""
    page = await ctx.new_page()
    try:
        await page.goto(post_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        best: list = []
        for fr in [page.main_frame] + list(page.frames):
            try:
                rows = await fr.evaluate(VIEW_LINKS_JS)
            except Exception:                                  # noqa: BLE001
                continue
            if len(rows) > len(best):
                best = rows
        links = [r["href"] for r in best]
        log(f"[기준글] 발행 화면에서 이미지 {len(links)}개 확인 · "
            f"링크 걸린 이미지 {sum(1 for h in links if h)}개")
        for i, r in enumerate(best):
            if r["href"]:
                log(f"      이미지#{i} 🔗 {r['href'][:110]}")
            elif r.get("attrs"):
                log(f"      이미지#{i} ⚠ a태그는 있는데 주소를 못 찾음(href={r['rawHref']!r}) — 속성:")
                for a in r["attrs"]:
                    log(f"            {a}")
        return links
    finally:
        await page.close()


async def _verify_edit_screen(page, blog_id: str, log_no: str, log) -> None:
    """정말 그 글의 수정 화면인지 확인한다.

    ★남의 글이면 네이버가 조용히 **내 블로그 목록 화면**으로 돌려보낸다.
      그대로 읽으면 엉뚱한 글의 제목/본문을 기준글로 삼게 된다(2026-08-20 실측).
    """
    urls = [page.url or ""] + [f.url or "" for f in page.frames]
    joined = " ".join(urls)
    ok_form = re.search(r"PostUpdateForm|postwrite", joined, re.I)
    ok_log = log_no in joined
    ok_owner = re.search(rf"blogId={re.escape(blog_id)}|/{re.escape(blog_id)}[/?]",
                         joined, re.I)
    if ok_form and ok_log and ok_owner:
        log(f"[기준글] 수정 화면 확인 ✅ (blogId={blog_id} logNo={log_no})")
        return

    log("[기준글] ❌ 수정 화면이 아닙니다. 실제로 열린 주소:")
    for u in urls[:8]:
        if u:
            log(f"         {u[:110]}")
    hint = ""
    if re.search(r"PostList|BlogHome", joined, re.I) and not ok_owner:
        hint = (f" — 네이버가 내 블로그로 돌려보냈습니다. 기준글 소유 계정({blog_id})으로 "
                "로그인해야 수정 화면이 열립니다.")
    raise RuntimeError(
        f"기준글 수정 화면 진입 실패(form={bool(ok_form)} logNo={ok_log} "
        f"owner={bool(ok_owner)}){hint}")


async def open_source(ctx, url: str, log) -> SourcePost:
    """참고글 수정 화면을 새 탭으로 연다(원본은 읽기만 한다)."""
    blog_id, log_no = parse_post_url(url)
    target = edit_url(url)
    log(f"[기준글] URL: {url}")
    log(f"[기준글] 수정 화면 진입: {target[:100]}")
    page = await ctx.new_page()
    await page.goto(target, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    if "nidlogin" in (page.url or ""):
        raise RuntimeError("기준글 수정 화면이 로그인 페이지로 튕겼습니다. 로그인 상태를 확인하세요.")
    await _verify_edit_screen(page, blog_id, log_no, log)
    frame = await browser.find_editor_frame(page, log, "기준글", timeout_sec=40)
    return SourcePost(page, frame, log)
