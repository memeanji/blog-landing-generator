"""참고 랜딩 URL에서 '무엇을 말하는 글인지'만 뽑아내는 추출기.

원문 문장을 그대로 쓰지 않기 위해, 여기서는 **문장이 아니라 짧은 키워드/구(句)** 위주로 모은다.
`raw_text`는 초안이 원문을 베끼지 않았는지 검증(draft.check_overlap)하는 용도로만 보관하고,
초안 작성에는 절대 넣지 않는다.

수집 대상(사용자 지정 6요소):
  핵심 제품/서비스 · 주요 타깃 · 메인 소구 · 문제 제기 방식 · 후기/경험형 서술 포인트 · 제품 소개 및 CTA 흐름
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

# ── 분류 힌트 ────────────────────────────────────────────────────────────
_PROBLEM_HINTS = ("고민", "걱정", "불편", "신경", "스트레스", "속상", "칙칙", "예민",
                  "푸석", "처짐", "주름", "잡티", "건조", "자꾸", "점점", "티가")
_EXPERIENCE_HINTS = ("후기", "사용", "써보", "발라", "바르", "느낌", "변화", "처음",
                     "주째", "개월", "아침", "저녁", "루틴", "직접", "리뷰")
_PRODUCT_HINTS = ("성분", "함유", "제형", "구성", "용량", "사용법", "패드", "앰플",
                  "크림", "세럼", "정품", "정기", "패키지", "본품")
_AUDIENCE_HINTS = ("대", "여성", "남성", "주부", "엄마", "직장인", "분들", "님")
_CTA_HINTS = ("구매", "주문", "신청", "바로가기", "자세히", "할인", "혜택", "받기",
              "확인", "보러", "상담")

# 초안에 절대 실어 나르면 안 되는 표현(의학적 확정·과장)
BANNED_EXPRESSIONS = ("치료", "완치", "개선효과", "효능", "의학적", "임상적으로 입증",
                      "부작용 없", "100%", "보장", "즉시 효과", "영구")

# 한 프레임(document)에서 본문 블록을 순서대로 뽑는 스크립트.
_EXTRACT_JS = r"""
() => {
  const BLOCK = 'h1,h2,h3,h4,h5,h6,p,li,blockquote,figcaption,td,th,dd,dt,div,section';
  const SKIP  = 'nav,header,footer,script,style,noscript,aside,iframe,form,select';
  const root  = document.querySelector('main,article,#content,#container,.content,.se-main-container')
                || document.body;
  if (!root) return {title:'', headings:[], blocks:[], ctas:[], body:'', lines:[]};

  const lines = [];
  const seen = new Set();
  root.querySelectorAll(BLOCK).forEach(el => {
    if (el.closest(SKIP)) return;
    if (el.querySelector(BLOCK)) return;              // leaf 블록만(부모/자식 중복 방지)
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length < 2) return;
    const key = t.slice(0, 80);
    if (seen.has(key)) return;                        // 같은 문구 반복 제거
    seen.add(key);
    // 링크 카드(se-oglink) 안의 블록이면 그 링크 주소를 같이 들고 간다.
    // 이 주소가 나중에 실전용에서 UTM 랜딩으로 교체되는 자리다.
    const a = el.closest('a');
    lines.push({
      tag: el.tagName.toLowerCase(),
      text: t,
      href: (a && a.href) ? a.href : ''
    });
  });

  const pick = (sel) => Array.from(document.querySelectorAll(sel))
    .map(e => (e.innerText || e.textContent || '').trim()).filter(t => t);
  const metaEl = (sel) => {
    const e = document.querySelector(sel);
    return e ? (e.innerText || '').replace(/\s+/g, ' ').trim() : '';
  };
  // 본문 안의 모든 링크(주소 포함) — 순서 유지, 중복 제거
  const seenHref = new Set();
  const links = [];
  root.querySelectorAll('a[href]').forEach(a => {
    const h = a.href;
    if (!h || seenHref.has(h)) return;
    if (/^javascript:/i.test(h)) return;
    seenHref.add(h);
    links.push({ href: h, text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) });
  });

  return {
    title: document.title || '',
    post_title: metaEl('.se-title-text,.pcol1,.itemSubjectBoldfont'),
    author: metaEl('.nick,.blog_author,.se_author,strong.nick'),
    links: links,
    headings: pick('h1,h2,h3,h4,strong,b'),
    blocks: pick('p,li,dd,dt,span,div'),
    ctas: pick('a,button'),
    body: (document.body && document.body.innerText) || '',
    lines: lines
  };
}
"""


_SPLIT = re.compile(r"[.!?\n·|]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class LandingBrief:
    """랜딩에서 뽑아낸 뼈대. 문장이 아니라 짧은 구 위주."""

    url: str
    page_title: str = ""
    product: str = ""
    audience: str = ""
    appeals: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    experiences: list[str] = field(default_factory=list)
    product_points: list[str] = field(default_factory=list)
    ctas: list[str] = field(default_factory=list)
    raw_text: str = ""              # 재작성 모드의 중복 검증 전용.
    # 원문을 순서 그대로 담은 블록들. (tag, text, href) — 기본(그대로 옮기기) 모드가 이걸 쓴다.
    #   href 는 링크 카드 안의 블록일 때만 채워진다.
    content_blocks: list[tuple[str, str, str]] = field(default_factory=list)
    post_title: str = ""            # 블로그 글 제목(출처 문구용)
    author: str = ""                # 작성자 닉네임(출처 문구용)
    # 본문 안 링크 (href, text). 마지막 제품 링크가 실전용에서 UTM 으로 교체될 자리.
    product_links: list[tuple[str, str]] = field(default_factory=list)

    def citation(self) -> str:
        """네이버가 '복사'할 때 자동으로 붙이는 출처 문구를 그대로 만든다.

        DOM 에는 없고 클립보드에만 붙는 문구라, 텍스트 모드에서는 직접 만들어 넣는다.
        """
        title = (self.post_title or self.page_title or "").strip()
        title = re.sub(r"\s*[:|-]\s*네이버\s*블로그\s*$", "", title).strip()
        if not (title and self.author):
            return ""
        return f"[출처] {title}|작성자 {self.author}"

    def content_text(self, with_citation: bool = True) -> str:
        """원문 본문을 순서 그대로 이어붙인다.

        · 소제목(h태그) 앞뒤에는 빈 줄
        · 링크 카드는 마지막 블록 뒤에 **실제 주소 한 줄**을 붙인다
          (실전용 전환 때 이 줄을 UTM 랜딩으로 바꾸면 된다)
        · 맨 끝에 네이버 출처 문구
        """
        out: list[str] = []
        blocks = self.content_blocks
        for i, item in enumerate(blocks):
            tag, text, href = (item + ("",))[:3] if len(item) < 3 else item
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                out.append("")          # 소제목 앞 여백
                out.append(text)
                out.append("")
            else:
                out.append(text)
            # 같은 href 를 가진 마지막 블록이면 주소를 한 줄 덧붙인다
            if href:
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                nxt_href = (nxt[2] if nxt and len(nxt) > 2 else "")
                if nxt_href != href:
                    out.append(href)
        if with_citation:
            cite = self.citation()
            if cite:
                out.append("")
                out.append(cite)
        merged = "\n".join(out)
        while "\n\n\n" in merged:
            merged = merged.replace("\n\n\n", "\n\n")
        return merged.strip()

    def summary_lines(self) -> list[str]:
        """로그로 남길 추출 결과 요약."""
        def j(items: list[str]) -> str:
            return " / ".join(items) if items else "(없음)"

        return [
            f"핵심 제품·서비스 : {self.product or '(추출 실패)'}",
            f"주요 타깃        : {self.audience or '(명시 없음)'}",
            f"메인 소구        : {j(self.appeals)}",
            f"문제 제기        : {j(self.problems)}",
            f"경험·후기 포인트 : {j(self.experiences)}",
            f"제품 소개 포인트 : {j(self.product_points)}",
            f"CTA 흐름         : {j(self.ctas)}",
        ]


def _clean(text: str) -> str:
    return _SPACES.sub(" ", (text or "").replace("​", "")).strip()


def _phrases(texts: list[str], hints: tuple[str, ...], limit: int, max_len: int = 22) -> list[str]:
    """힌트 단어가 들어간 짧은 구만 추린다(긴 문장은 통째로 베낄 위험이 있어 제외)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for piece in _SPLIT.split(t):
            p = _clean(piece)
            if not (2 <= len(p) <= max_len):
                continue
            if not any(h in p for h in hints):
                continue
            key = p.replace(" ", "")
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
            if len(out) >= limit:
                return out
    return out


class ReferenceExtractor:
    """참고 랜딩 1건을 읽어 LandingBrief 로 만든다. 읽기 전용(클릭·입력 없음)."""

    def __init__(self, enabled: bool, headless: bool, user_data_dir: Path, log: LogFn) -> None:
        self.enabled = enabled
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.log = log

    def extract(self, url: str) -> LandingBrief:
        if not self.enabled:
            raise RuntimeError("ENABLE_EXTERNAL_ACTIONS=true 여야 참고 랜딩을 읽을 수 있습니다.")
        if not re.match(r"^https?://", url.strip(), re.I):
            raise ValueError(f"참고 URL 형식이 올바르지 않습니다: {url}")
        return asyncio.run(self._extract(url.strip()))

    async def _extract(self, url: str) -> LandingBrief:
        from playwright.async_api import async_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir), headless=self.headless
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                self.log(f"[참고 랜딩] 접속: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6_000)
                except Exception:  # noqa: BLE001  (무한 폴링 광고 등으로 idle 안 될 수 있음)
                    pass
                await page.wait_for_timeout(600)

                # ★네이버 블로그 글은 본문이 #mainFrame iframe 안에 있다.
                #   최상위 document 만 보면 0자가 나오므로 모든 프레임에서 뽑아보고,
                #   본문 블록이 가장 많은 프레임을 채택한다(일반 랜딩은 최상위가 뽑힘).
                data = await self._extract_best_frame(page)
                brief = self._build(url, data)
                self.log(f"[참고 랜딩] 추출 완료 (본문 {len(brief.raw_text):,}자)")
                for line in brief.summary_lines():
                    self.log("   " + line)
                return brief
            finally:
                await context.close()

    async def _extract_best_frame(self, page) -> dict:
        """최상위 + 모든 iframe 에서 추출해 본문 블록이 가장 많은 결과를 고른다.

        네이버 블로그 글(검수용 랜딩)이 #mainFrame 안에 있어서 필요하다.
        페이지 제목은 최상위 document.title 을 우선한다.
        """
        best: dict = {}
        best_n = -1
        scopes = [("최상위", page)] + [
            (f"frame:{(f.name or f.url or '')[:40]}", f) for f in page.frames if f != page.main_frame
        ]
        for label, scope in scopes:
            try:
                data = await scope.evaluate(_EXTRACT_JS)
            except Exception as exc:  # noqa: BLE001  (교차출처 프레임 등)
                self.log(f"   [{label}] 추출 건너뜀: {type(exc).__name__}")
                continue
            n = len(data.get("lines") or [])
            self.log(f"   [{label}] 본문 블록 {n}개")
            if n > best_n:
                best, best_n = data, n
        if best and not (best.get("title") or "").strip():
            try:
                best["title"] = await page.title()
            except Exception:  # noqa: BLE001
                pass
        return best or {}

    def _build(self, url: str, data: dict) -> LandingBrief:
        title = _clean(data.get("title", ""))
        headings = [_clean(h) for h in (data.get("headings") or [])]
        headings = [h for h in headings if 2 <= len(h) <= 40]
        blocks = [_clean(b) for b in (data.get("blocks") or []) if _clean(b)]
        # div/span 은 부모까지 통째로 잡혀 중복이 심하므로 짧은 것만 남긴다
        blocks = [b for b in blocks if len(b) <= 200][:600]
        cta_raw = [_clean(c) for c in (data.get("ctas") or [])]
        raw_text = _clean(data.get("body", ""))

        product = ""
        for cand in ([title] + headings):
            c = _clean(cand)
            if 2 <= len(c) <= 30:
                product = c
                break

        audience = ""
        for a in _phrases(headings + blocks, _AUDIENCE_HINTS, limit=1, max_len=18):
            audience = a
            break

        return LandingBrief(
            url=url,
            page_title=title,
            product=product,
            audience=audience,
            appeals=[h for h in headings if 4 <= len(h) <= 30][:6],
            problems=_phrases(headings + blocks, _PROBLEM_HINTS, limit=6),
            experiences=_phrases(blocks, _EXPERIENCE_HINTS, limit=6),
            product_points=_phrases(headings + blocks, _PRODUCT_HINTS, limit=6),
            ctas=[c for c in cta_raw if c and len(c) <= 20 and any(h in c for h in _CTA_HINTS)][:5],
            raw_text=raw_text,
            content_blocks=[
                (str(l.get("tag", "p")), _clean(l.get("text", "")), str(l.get("href", "") or ""))
                for l in (data.get("lines") or [])
                if _clean(l.get("text", ""))
            ],
            post_title=_clean(data.get("post_title", "")),
            author=_clean(data.get("author", "")),
            product_links=[
                (str(l.get("href", "")), _clean(l.get("text", "")))
                for l in (data.get("links") or [])
                if str(l.get("href", ""))
            ],
        )
