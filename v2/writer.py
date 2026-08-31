"""새 글 작성 — 모바일 전환 → 제목 → 컴포넌트별 붙여넣기 → 후처리 → 중앙정렬 → 발행.

★실측 확정(2026-08-20, 기존 프로젝트에서 확인된 것만 사용)
  · 새 글 주소: blog.naver.com/{id}/postwrite  (page.url 이 postwrite 면 main_frame 이 곧 에디터)
  · 제목/본문은 locator.fill() / execCommand insertText 로 안 들어간다(거짓 성공).
    → placeholder 를 클릭해 캐럿을 잡고 keyboard.type / Ctrl+V 를 쓴다.
  · Ctrl+V(붙여넣기)는 정상 동작한다(Ctrl+C 만 OS 창 포커스를 탄다).
  · '작성 중인 글이 있습니다' 팝업은 최대 10초 폴링 후 '취소'. 0초 판정 금지.
  · 정렬은 2단계: 정렬 드롭다운 열기 → '가운데 정렬' 항목 클릭.
  · 문단 삭제는 <p> 자체만. 상위 se-component 로 올라가면 본문이 통째로 날아간다.
"""
from __future__ import annotations

import asyncio
import re

from . import browser

POPUP_MARKS = ("작성 중인 글이 있습니다", "이어서 작성하시겠습니까")

# 후처리로 지울 홍보 문구 (startsWith 판정). ★[출처] 는 더 이상 다루지 않는다.
PROMO_PREFIXES = (
    "Re:purely | 올레놀샷",
    "Re:purely의 올레놀샷",
    "Re:purely |",
    "리퓨얼리 |",
)
PROMO_EXACT_RE = r"^(re:?\s?purely|리퓨얼리)$"
URL_TEXT_RE = r"^(https?://\S+|www\.\S+|\S+\.(com|co\.kr|net|kr)(/\S*)?)$"

# 제품 링크/이미지 바로 아래에 남는 라벨 텍스트. 문단 전체가 이 단어 하나일 때만 지운다.
LINK_LABELS = ("링크", "link", "링크 바로가기", "바로가기 링크")

UI_NOISE = ("추가할 컴포넌트를 선택하세요", "컴포넌트를 선택하세요")


# ── 본문 통계 ──────────────────────────────────────────────────────────
BODY_ROOT_JS = """
  const root = document.querySelector('.se-main-container') || document.querySelector('.se-content');
"""

# 본문 영역 안에 **제목 컴포넌트**가 들어왔는지 본다(문자열 포함 여부와는 다른 문제다).
TITLE_COMP_IN_BODY_JS = r"""(title) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false};
     const norm = t => (t || '').replace(/[\u200b\s]+/g, ' ').trim();
     // ① 제목 전용 컴포넌트가 본문으로 **더** 복사된 경우
     //   ★수정 화면 에디터는 본문 root(.se-content) **안에 원래 제목이 하나 있다.**
     //     그걸 '복사됨' 으로 세면 정상인 글이 전부 실패한다(2026-08-24 실측).
     //     정상 제목 영역 1개는 검사에서 빼고, **본문 영역에 들어온 추가 제목**만 생다.
     const allTitleEls = Array.from(document.querySelectorAll('.se-documentTitle'));
     const canonical = allTitleEls[0] || null;   // 문서의 정상 제목 영역 1개 = 검사 제외
     // 본문 영역 = root 안에서 정상 제목 영역을 제외한 부분.
     const extraInBody = Array.from(root.querySelectorAll('.se-documentTitle'))
       .filter(el => el !== canonical && !(canonical && canonical.contains(el)));
     let extraTitles, how;
     if (canonical) {
       extraTitles = extraInBody.length;         // ← 주 경로: 본문 영역에 들어온 추가 제목만
       how = 'body-scan';
     } else {
       // 구조상 정상 제목 영역을 못 찾을 때의 fallback(전체 2개 이상)
       extraTitles = Math.max(0, allTitleEls.length - 1);
       how = 'fallback-count';
     }
     const allTitles = allTitleEls.length;
     const inBody = extraInBody.length;
     // ② 본문 첫 컴포넌트의 글이 제목과 '통째로 같은' 경우
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true))
       .filter(c => norm(c.innerText) || c.querySelector('img'));
     const first = comps[0] ? norm(comps[0].innerText) : '';
     const want = norm(title);
     return {ok: true, titleComp: extraTitles, how: how,
             allTitles: allTitles, extraInBody: inBody,
             rootHasCanonical: !!(canonical && root.contains(canonical)),
             firstEqualsTitle: !!want && first === want,
             first: first.slice(0, 60)};
   }"""


STATS_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '.se-main-container 를 찾지 못함'};
     const norm = t => (t || '').replace(/\s+/g, ' ').trim();
     const isTitle = el => el.classList.contains('se-documentTitle')
                        || !!el.closest('.se-documentTitle');
     const comps = Array.from(root.querySelectorAll('.se-component')).filter(el => {
       const par = el.parentElement && el.parentElement.closest('.se-component');
       if (par && root.contains(par)) return false;
       return !isTitle(el);
     });
     const items = comps.map(el => ({
       chars: norm(el.innerText).length,
       imgs: el.querySelectorAll('img').length,
       head: norm(el.innerText).slice(0, 30),
       cls: (el.className || '').toString().replace(/\s+/g, ' ').slice(0, 40),
     }));
     const text = comps.map(el => norm(el.innerText)).filter(Boolean).join('\n');
     const titleEl = document.querySelector('.se-documentTitle');
     return {ok: true, comps: comps.length,
             chars: norm(text).length,
             imgs: comps.reduce((a, el) => a + el.querySelectorAll('img').length, 0),
             text: text,
             tail: norm(text).slice(-60),
             title: titleEl ? norm(titleEl.innerText) : '',
             items: items};
   }"""

CARET_STATE_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     const s = window.getSelection();
     let inBody = false;
     if (root && s && s.anchorNode) {
       const node = s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement;
       inBody = !!(node && root.contains(node) && !node.closest('.se-documentTitle'));
     }
     return {focused: document.hasFocus(), inBody: inBody,
             ranges: s ? s.rangeCount : 0};
   }"""

# 캐럿을 붙일 자리를 표시한다(클릭 대상). 이미지 컴포넌트는 클릭하면 이미지가 선택되므로 피한다.
MARK_CARET_JS = r"""() => {
     document.querySelectorAll('[data-v2-caret]').forEach(e => e.removeAttribute('data-v2-caret'));
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '본문 root 없음'};
     const paras = Array.from(root.querySelectorAll('.se-text-paragraph'))
       .filter(p => !p.closest('.se-documentTitle'));
     if (paras.length) {
       const last = paras[paras.length - 1];
       last.setAttribute('data-v2-caret', '1');
       return {ok: true, mode: 'paragraph', count: paras.length};
     }
     const ph = Array.from(document.querySelectorAll("[class*='se-placeholder']"))
       .filter(e => !e.closest('.se-documentTitle'))
       .filter(e => {
         const r = e.getBoundingClientRect();
         return r.width > 30 && r.height > 8;
       });
     if (ph.length) {
       ph[0].setAttribute('data-v2-caret', '1');
       return {ok: true, mode: 'placeholder', count: ph.length};
     }
     return {ok: false, why: '클릭할 문단/placeholder 를 찾지 못함'};
   }"""

# 클릭으로 잡은 캐럿을 본문 맨 끝으로 옮긴다(클릭은 포커스용, 위치는 Range 가 확정).
CARET_TO_END_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '본문 root 없음'};
     const isTitle = el => el.classList.contains('se-documentTitle')
                        || !!el.closest('.se-documentTitle');
     const comps = Array.from(root.querySelectorAll('.se-component')).filter(el => {
       const par = el.parentElement && el.parentElement.closest('.se-component');
       if (par && root.contains(par)) return false;
       return !isTitle(el);
     });
     if (!comps.length) return {ok: true, mode: 'empty'};
     const last = comps[comps.length - 1];
     const paras = Array.from(last.querySelectorAll('.se-text-paragraph'));
     const target = paras.length ? paras[paras.length - 1] : last;
     const r = document.createRange();
     r.selectNodeContents(target);
     r.collapse(false);
     const s = window.getSelection();
     s.removeAllRanges();
     s.addRange(r);
     return {ok: true, mode: paras.length ? 'paragraph' : 'component'};
   }"""

TITLE_SPOT_JS = r"""() => {
     document.querySelectorAll('[data-v2-title]').forEach(e => e.removeAttribute('data-v2-title'));
     const vis = el => {
       const r = el.getBoundingClientRect();
       const st = getComputedStyle(el);
       return r.width > 30 && r.height > 8 && st.display !== 'none' && st.visibility !== 'hidden';
     };
     // ① 제목 컴포넌트 안의 placeholder 우선
     let cand = Array.from(document.querySelectorAll(
         ".se-documentTitle [class*='se-placeholder'], .se-documentTitle .se-text-paragraph"))
       .filter(vis)[0];
     // ② 없으면 화면에서 가장 위에 있는 placeholder
     if (!cand) {
       cand = Array.from(document.querySelectorAll("[class*='se-placeholder']"))
         .filter(vis)
         .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y)[0];
     }
     if (!cand) return {ok: false, why: '제목 입력 자리를 찾지 못함'};
     cand.setAttribute('data-v2-title', '1');
     const r = cand.getBoundingClientRect();
     return {ok: true, cls: (cand.className || '').toString().slice(0, 50),
             box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]};
   }"""

# 모바일/디바이스 전환 후보 수집 — 선택자를 추측하지 않고 화면에서 찾는다.
# ★화면 전환 버튼은 PC → 태블릿 → 모바일 **순환 토글**이다(2026-08-21 실측).
#   switch_to_mobile() 이 한 번만 눌러 '태블릿 화면'에서 멈추는 일이 있었다.
#   현재 상태는 버튼 class(vpc/vtablet/vmobile)와 옆 라벨(se-utils-text)에 그대로 나온다.
MODE_STATE_JS = r"""() => {
     const btn = document.querySelector("[class*='__mode-button']");
     const txt = Array.from(document.querySelectorAll("[class*='se-utils-text']"))
       .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
       .filter(t => /화면/.test(t));
     return {cls: btn ? (btn.className || '').toString() : '', txt: txt};
   }"""


DEVICE_CANDS_JS = r"""() => {
     document.querySelectorAll('[data-v2-dev]').forEach(e => e.removeAttribute('data-v2-dev'));
     const out = [];
     let i = 0;
     document.querySelectorAll("button,a,[role='button'],label,li,span").forEach(el => {
       const lab = ((el.getAttribute('aria-label') || '') + ' '
                  + (el.getAttribute('title') || '') + ' '
                  + (el.getAttribute('data-name') || '') + ' '
                  + (el.getAttribute('data-log') || '') + ' '
                  + (el.className || '').toString() + ' '
                  + (el.innerText || '')).replace(/\s+/g, ' ').trim();
       if (!/모바일|mobile|device|디바이스/i.test(lab)) return;
       const r = el.getBoundingClientRect();
       if (r.width < 8 || r.height < 8) return;
       if (el.querySelector('button,[role="button"]')) return;      // 컨테이너 제외
       el.setAttribute('data-v2-dev', String(i));
       out.push({i: i, tag: el.tagName.toLowerCase(), lab: lab.slice(0, 80),
                 x: Math.round(r.x), y: Math.round(r.y)});
       i += 1;
     });
     return out;
   }"""

DEVICE_STATE_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     const w = root ? Math.round(root.getBoundingClientRect().width) : -1;
     const marks = Array.from(document.querySelectorAll(
         "[class*='mobile'],[class*='device']"))
       .filter(e => /is-selected|active|on\b/.test((e.className || '').toString()))
       .map(e => (e.className || '').toString().slice(0, 60))
       .slice(0, 5);
     return {width: w, marks: marks};
   }"""

ALIGN_STATS_JS = r"""() => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false};
     document.querySelectorAll('[data-v2-img]').forEach(e => e.removeAttribute('data-v2-img'));
     const secs = Array.from(root.querySelectorAll("[class*='se-section']"))
       .filter(s => !s.closest('.se-documentTitle'))
       .filter(s => s.querySelector('img'));
     let i = 0;
     const imgs = secs.map(s => {
       s.setAttribute('data-v2-img', String(i));
       const centered = /se-section-align-center/.test((s.className || '').toString());
       const row = {i: i, centered: centered,
                    cls: (s.className || '').toString().replace(/\s+/g, ' ').slice(0, 50)};
       i += 1;
       return row;
     });
     const paras = Array.from(root.querySelectorAll('.se-text-paragraph'))
       .filter(p => !p.closest('.se-documentTitle'));
     const paraCentered = paras.filter(
       p => /align-center/.test((p.className || '').toString())).length;
     return {ok: true, imgs: imgs, imgTotal: imgs.length,
             imgCentered: imgs.filter(x => x.centered).length,
             paraTotal: paras.length, paraCentered: paraCentered};
   }"""

TAIL_DUMP_JS = r"""(n) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return [];
     const isTitle = el => el.classList.contains('se-documentTitle')
                        || !!el.closest('.se-documentTitle');
     const comps = Array.from(root.querySelectorAll('.se-component')).filter(el => {
       const par = el.parentElement && el.parentElement.closest('.se-component');
       if (par && root.contains(par)) return false;
       return !isTitle(el);
     });
     return comps.slice(-n).map(el => ({
       cls: (el.className || '').toString().replace(/\s+/g, ' ').slice(0, 60),
       text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60),
       html: (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 320),
     }));
   }"""


CLEANUP_JS = r"""(opt) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {ok: false, why: '본문 root 없음'};
     const norm = t => (t || '').replace(/\s+/g, ' ').trim();
     const exactRe = new RegExp(opt.exactRe, 'i');
     const urlRe = new RegExp(opt.urlRe, 'i');
     const isLabel = t => opt.labels.some(x => t.toLowerCase() === x.toLowerCase());
     const removed = [], skipped = [];

     const paras = Array.from(root.querySelectorAll('p.se-text-paragraph, .se-text-paragraph'))
       .filter(p => p.tagName === 'P')
       .filter(p => !p.closest('.se-documentTitle'));

     paras.forEach(p => {
       const t = norm(p.innerText);
       if (!t) return;
       if (p.querySelector('img')) return;                       // ★제품 이미지 보호

       let hit = '';
       // ★[출처] 규칙 삭제 — 본문을 한 번에 복사하면 네이버가 출처 문구를 붙이지 않는다.
       //   (컴포넌트를 여러 번 나눠 복사할 때만 붙었다. 2026-08-21 사용자 시연으로 확정)
       if (opt.promo.some(x => t.indexOf(x) === 0)) hit = '홍보문구';
       else if (exactRe.test(t)) hit = 'repurely';
       else if (urlRe.test(t) && /repurely|blog\.naver|http/i.test(t)) hit = 'URL텍스트';
       else if (isLabel(t)) hit = '링크라벨';
       if (!hit) return;

       // ★광고 표시(법적 표기)는 절대 건드리지 않는다.
       //   '본 콘텐츠의 광고주는 REPURELY 작성자는 … 입니다' 가 DOM 에서는 문단으로 쪼개져 있어
       //   'REPURELY' 문단만 지우면 '광고주는  작성자는 …' 로 문장이 깨진다(2026-08-20 실측).
       const compTxt = norm((p.closest('.se-component') || p).innerText);
       if (/광고주|본 콘텐츠의/.test(compTxt)) {
         skipped.push({hit: hit, why: '광고표시', text: t.slice(0, 50)});
         return;
       }

       // ★실제 제품으로 가는 링크는 보호한다.
       //   - 링크카드(oglink) 안이면 건드리지 않는다
       //   - 링크 텍스트가 주소/홍보문구가 아니라 사람이 읽는 문구면(=버튼) 남긴다
       const card = p.closest("[class*='oglink'], [class*='se-module-oglink']");
       if (card) { skipped.push({hit: hit, why: '링크카드', text: t.slice(0, 50)}); return; }
       const anchors = Array.from(p.querySelectorAll('a'));
       const meaningful = anchors.some(a => {
         const at = norm(a.innerText);
         return at && !urlRe.test(at) && !exactRe.test(at) && !isLabel(at)
                && !opt.promo.some(x => at.indexOf(x) === 0);
       });
       if (meaningful) { skipped.push({hit: hit, why: '링크문구', text: t.slice(0, 50)}); return; }

       // ★<p> 자체만 지운다. 상위 se-component 로 올라가면 본문이 통째로 날아간다.
       //   무엇을 지웠는지 확인할 수 있게 HTML 도 함께 남긴다(링크가 딸려 나가지 않았는지 확인용).
       removed.push({hit: hit, text: t.slice(0, 60),
                     html: (p.outerHTML || '').replace(/\s+/g, ' ').slice(0, 180),
                     hrefs: anchors.map(a => a.getAttribute('href') || '').join(' | ').slice(0, 120)});
       if (!opt.dryRun) p.remove();
     });
     return {ok: true, removed: removed, skipped: skipped};
   }"""


# 제로폭 공백(​ 등)은 네이버 에디터가 문단에 심어 두는 문자다.
#   눈에 안 보이는데 문자열 비교만 어긋나게 하므로 비교 전에 지운다.
_ZERO_WIDTH = re.compile(r"[​‌‍﻿ ]")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", _ZERO_WIDTH.sub(" ", t or "")).strip()


FIND_URL_P_JS = r"""(opt) => {
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {found: false, why: 'root 없음'};
     const ps = Array.from(root.querySelectorAll('p.se-text-paragraph, .se-text-paragraph'))
       .filter(el => el.tagName === 'P')
       .filter(el => !el.closest('.se-documentTitle'));
     for (const el of ps) {
       if (el.querySelector('img')) continue;
       const comp = el.closest('.se-component');
       if (comp && comp.querySelector('img')) continue;       // ★카드 안쪽은 건드리지 않는다
       const t = (el.innerText || '').replace(/\s/g, '');
       if (!/^https?:\/\//.test(t)) continue;
       el.scrollIntoView({block: 'center'});
       const r = el.getBoundingClientRect();
       if (r.width < 4 || r.height < 4) continue;
       return {found: true, text: t.slice(0, 70),
               x: Math.round(r.x + Math.min(30, r.width / 2)),
               y: Math.round(r.y + Math.min(10, r.height / 2)),
               w: Math.round(r.width), h: Math.round(r.height)};
     }
     return {found: false};
   }"""


FIND_BLANK_P_JS = r"""(host) => {
     // 제품 카드 **바로 위**에 남은 빈 문단 한 줄을 찾는다.
     // URL 을 붙여넣기 전에 Enter 를 눌러 만든 문단이, URL 텍스트만 지우면 빈 줄로 남는다.
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {found: false, why: 'root 없음'};
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     const isCard = c => {
       if (!c.querySelector('img')) return false;
       const cls = (c.className || '').toString();
       if (/oglink|se-link/.test(cls)) return true;
       const txt = (c.innerText || '').replace(/\s+/g, ' ').trim();
       return !!host && txt.indexOf(host) >= 0;
     };
     let end = comps.length;
     for (let i = comps.length - 1; i >= 0; i--) { if (isCard(comps[i])) { end = i; break; } }
     const isBlank = el => !el.querySelector('img')
       && (el.innerText || '').replace(/[\s\u200b\u00a0]/g, '') === '';
     for (let i = end - 1; i >= 0; i--) {
       const c = comps[i];
       if (c.querySelector('img')) break;              // 이미지 컴포넌트면 손대지 않는다
       const ps = Array.from(c.querySelectorAll('p.se-text-paragraph'))
         .filter(el => el.tagName === 'P');
       if (!ps.length) break;
       const last = ps[ps.length - 1];
       if (!isBlank(last)) break;                      // 빈 문단이 아니면 지울 게 없다
       last.scrollIntoView({block: 'center'});
       const r = last.getBoundingClientRect();
       if (r.width < 4 || r.height < 4) break;
       return {found: true, only: ps.length === 1,
               x: Math.round(r.x + Math.min(20, r.width / 2)),
               y: Math.round(r.y + Math.min(10, r.height / 2))};
     }
     return {found: false};
   }"""


CARD_STATE_JS = r"""(host) => {
     // ★글쓰기 화면의 본문 root 는 .se-content 다. .se-main-container 로만 찾으면
     //   root=null 이라 카드가 화면에 있어도 0개로 센다(2026-08-21 실측).
     const root = document.querySelector('.se-main-container')
               || document.querySelector('.se-content');
     if (!root) return {cards: 0, hrefs: [], imgs: 0, tail: [], why: 'root 없음'};
     const comps = Array.from(root.querySelectorAll('.se-component'))
       .filter(c => !c.closest('.se-documentTitle'))
       .filter(c => (c.parentElement ? !c.parentElement.closest('.se-component') : true));
     const hrefsOf = c => Array.from(c.querySelectorAll('a[href]'))
       .map(a => a.getAttribute('href') || '').filter(Boolean);
     // ★에디터 안에서는 링크카드에 <a href> 가 DOM 으로 붙지 않는다(링크는 에디터 모델에만
     //   들어 있다). href 로만 판정하면 카드가 화면에 멀쩡히 있는데 0개로 센다(2026-08-21 실측:
     //   .se-component se-oglink se-l-large_image 140자 이미지1개 links=[]).
     const isCard = c => {
       if (!c.querySelector('img')) return false;
       const cls = (c.className || '').toString();
       if (/oglink|se-link/.test(cls)) return true;
       if (hrefsOf(c).some(h => host && h.indexOf(host) >= 0)) return true;
       const txt = (c.innerText || '').replace(/\s+/g, ' ').trim();
       return !!host && txt.indexOf(host) >= 0;
     };
     const hit = comps.filter(isCard);
     const tail = comps.slice(-4).map(c => ({
       cls: (c.className || '').toString().replace(/\s+/g, ' ').slice(0, 45),
       chars: (c.innerText || '').replace(/\s+/g, ' ').trim().length,
       imgs: c.querySelectorAll('img').length,
       hrefs: hrefsOf(c).slice(0, 2).map(h => h.slice(0, 60))}));
     return {cards: hit.length,
             imgs: hit.reduce((n, c) => n + c.querySelectorAll('img').length, 0),
             hrefs: hit.flatMap(hrefsOf).slice(0, 3),
             cls: hit.map(c => (c.className || '').toString().slice(0, 45)).slice(0, 2),
             tail: tail};
   }"""


TO_CLIP_JS = r"""(u) => {
     const ta = document.createElement('textarea');
     ta.value = u;
     ta.style.position = 'fixed';
     ta.style.opacity = '0';
     document.body.appendChild(ta);
     ta.select();
     let ok = false;
     try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
     ta.remove();
     return ok;
   }"""


class NewPost:
    def __init__(self, page, frame, log) -> None:
        self.page = page
        self.frame = frame
        self.log = log

    # ── 공통 ────────────────────────────────────────────────────────
    async def _fr(self):
        self.frame = await browser.fresh(self.page, self.frame, self.log, "새글")
        return self.frame

    async def stats(self) -> dict:
        fr = await self._fr()
        st = await fr.evaluate(STATS_JS)
        if not st.get("ok"):
            raise RuntimeError(f"[새글] 본문 통계 실패 — {st.get('why')}")
        return st

    async def shot(self, name: str, out_dir) -> None:
        try:
            path = str(out_dir / f"{name}.png")
            await self.page.screenshot(path=path, full_page=False)
            self.log(f"[스크린샷] {path}")
        except Exception as exc:                               # noqa: BLE001
            self.log(f"[스크린샷] 실패({type(exc).__name__})")

    # ── 3. 모바일 화면 전환 ─────────────────────────────────────────
    async def switch_to_mobile(self) -> bool:
        fr = await self._fr()
        before = await fr.evaluate(DEVICE_STATE_JS)
        cands = await fr.evaluate(DEVICE_CANDS_JS)
        if not cands:
            self.log("[새글] ⚠ 모바일 전환 버튼 후보를 찾지 못했습니다(PC 화면 그대로 진행)")
            return False
        for c in cands[:10]:
            self.log(f"      후보 <{c['tag']}> @({c['x']},{c['y']}) {c['lab'][:60]!r}")

        picked = [c for c in cands if re.search(r"모바일|mobile", c["lab"], re.I)]
        for c in picked[:5]:
            try:
                await fr.locator(f'[data-v2-dev="{c["i"]}"]').first.click(timeout=2500)
                await self.page.wait_for_timeout(900)
                after = await fr.evaluate(DEVICE_STATE_JS)
                if after["width"] != before["width"] or after["marks"] != before["marks"]:
                    self.log(f"[새글] 모바일 화면 전환 완료 — 본문 폭 "
                             f"{before['width']}px → {after['width']}px ({c['lab'][:40]!r})")
                    return True
                self.log(f"      클릭했지만 변화 없음: {c['lab'][:40]!r}")
            except Exception as exc:                           # noqa: BLE001
                self.log(f"      클릭 실패({c['lab'][:30]!r}): {type(exc).__name__}")
        self.log("[새글] ⚠ 모바일 전환을 확인하지 못했습니다(PC 화면 그대로 진행)")
        return False

    async def ensure_mobile(self, max_clicks: int = 3) -> bool:
        """'모바일 화면'이 될 때까지 토글을 더 누른다(태블릿에서 멈추는 것 방지)."""
        fr = await self._fr()
        st = await fr.evaluate(MODE_STATE_JS)
        is_mobile = lambda x: ("vmobile" in (x.get("cls") or "")          # noqa: E731
                               or any("모바일" in t for t in (x.get("txt") or [])))
        self.log(f"[모바일] 현재 상태 — cls={st['cls'][:40]!r} 라벨={st['txt']}")
        for n in range(max_clicks):
            if is_mobile(st):
                self.log("[모바일] 모바일 화면 확정 ✅")
                return True
            try:
                await fr.locator("[class*='__mode-button']").first.click(timeout=2500)
            except Exception as exc:                           # noqa: BLE001
                self.log(f"[모바일] 토글 클릭 실패: {type(exc).__name__}")
                return False
            await self.page.wait_for_timeout(900)
            fr = await self._fr()
            st = await fr.evaluate(MODE_STATE_JS)
            self.log(f"[모바일] {n + 1}번째 추가 클릭 → cls={st['cls'][:40]!r} 라벨={st['txt']}")
        if not is_mobile(st):
            self.log("[모바일] ⚠ 모바일 화면을 확정하지 못했습니다.")
            return False
        return True

    # ── 3. 제목 입력 ────────────────────────────────────────────────
    async def type_title(self, title: str) -> None:
        fr = await self._fr()
        spot = await fr.evaluate(TITLE_SPOT_JS)
        if not spot.get("ok"):
            await browser.dump_frames(self.page, self.log, "새글")
            raise RuntimeError(f"[새글] 제목 입력 자리를 찾지 못했습니다 — {spot.get('why')}")
        self.log(f"[새글] 제목 자리 클릭 — class={spot['cls']!r} box={spot['box']}")
        await fr.locator('[data-v2-title]').first.click(timeout=8000)
        await self.page.wait_for_timeout(400)
        await self.page.keyboard.type(title, delay=12)
        await self.page.wait_for_timeout(600)

        got = _norm((await self.stats())["title"])
        if _norm(title) not in got and got not in _norm(title):
            raise RuntimeError(f"[새글] 제목 입력 검증 실패 — 화면 제목={got!r} / 기대={title!r}")
        self.log(f"[새글] 제목 입력 완료 — {got!r}")

    # ── 4. 본문 컴포넌트 붙여넣기 ───────────────────────────────────
    async def prepare_body_caret(self) -> None:
        """본문에 캐럿을 놓는다(클릭=포커스, Range=위치)."""
        fr = await self._fr()
        mark = await fr.evaluate(MARK_CARET_JS)
        if not mark.get("ok"):
            await browser.dump_frames(self.page, self.log, "새글")
            raise RuntimeError(f"[새글] 본문 클릭 자리를 찾지 못했습니다 — {mark.get('why')}")
        await fr.locator('[data-v2-caret]').first.click(timeout=8000,
                                                        position={"x": 20, "y": 8})
        await self.page.wait_for_timeout(350)
        fr = await self._fr()
        end = await fr.evaluate(CARET_TO_END_JS)
        state = await fr.evaluate(CARET_STATE_JS)
        self.log(f"[새글] 본문 캐럿 준비 — 클릭={mark['mode']} 이동={end.get('mode')} "
                 f"focus={state['focused']} inBody={state['inBody']}")

    async def ensure_caret(self) -> None:
        fr = await self._fr()
        state = await fr.evaluate(CARET_STATE_JS)
        if state["focused"] and state["inBody"]:
            await fr.evaluate(CARET_TO_END_JS)
            return
        self.log(f"      캐럿 이탈 감지(focus={state['focused']} inBody={state['inBody']}) — 다시 잡습니다")
        await self.prepare_body_caret()

    async def paste_all(self, src, img_timeout_ms: int = 60_000) -> dict:
        """기준글 본문 전체를 **한 번에** 붙여넣고 검증한다 (2026-08-21 사용자 시연 방식).

        컴포넌트를 나눠 9번 붙이던 방식과 달리 Ctrl+V 한 번이다. 이미지가 서버에 올라가는
        데 시간이 걸리므로 이미지 개수가 기대치에 닿을 때까지 기다린다.
        """
        before = await self.stats()
        await self.ensure_caret()
        await self.page.keyboard.press("Control+V")
        await self.page.wait_for_timeout(1500)

        want_imgs = src.want_imgs
        waited, after = 0, await self.stats()
        while after["imgs"] < before["imgs"] + want_imgs and waited < img_timeout_ms:
            await self.page.wait_for_timeout(1000)
            waited += 1000
            after = await self.stats()
            if waited % 10_000 == 0:
                self.log(f"      이미지 업로드 대기 {waited // 1000}초 — "
                         f"{after['imgs'] - before['imgs']}/{want_imgs}개")

        d_chars = after["chars"] - before["chars"]
        d_imgs = after["imgs"] - before["imgs"]
        self.log(f"[붙여넣기] +{d_chars}자 +{d_imgs}img "
                 f"(기대 {src.want_chars}자 / {want_imgs}img · {waited // 1000}초 대기)")

        if d_imgs != want_imgs:
            raise RuntimeError(f"[붙여넣기] 이미지 {d_imgs}개 ≠ 기대 {want_imgs}개 "
                               f"— 발행하지 않습니다")
        if d_chars < max(1, int(src.want_chars * 0.8)):
            raise RuntimeError(f"[붙여넣기] 글자 {d_chars}자 < 기대 {src.want_chars}자의 80% "
                               f"— 발행하지 않습니다")
        return after

    async def paste_component(self, no: int, total: int, comp, expected: dict) -> None:
        """컴포넌트 1개를 붙여넣고 그 자리에서 검증한다. 실패하면 예외."""
        before = await self.stats()
        await self.ensure_caret()
        await self.page.keyboard.press("Control+V")
        await self.page.wait_for_timeout(900 if comp.imgs else 500)

        after = await self.stats()
        # 이미지가 붙는 데 시간이 걸린다 — 최대 12초 기다린다.
        waited = 0
        while comp.imgs and after["imgs"] < before["imgs"] + comp.imgs and waited < 12000:
            await self.page.wait_for_timeout(700)
            waited += 700
            after = await self.stats()

        d_chars = after["chars"] - before["chars"]
        d_imgs = after["imgs"] - before["imgs"]

        why = ""
        if d_imgs != comp.imgs:
            why = f"이미지 증가 {d_imgs}개 ≠ 기대 {comp.imgs}개"
        elif comp.chars and d_chars < max(1, int(comp.chars * 0.5)):
            why = f"글자 증가 {d_chars}자 < 기대 {comp.chars}자의 50%"
        elif comp.chars == 0 and comp.imgs == 0:
            why = "빈 컴포넌트"
        if why:
            raise RuntimeError(
                f"[복사] {no}/{total} 검증 실패 — {why}\n"
                f"       기준글 컴포넌트: kind={comp.kind} {comp.chars}자 이미지 {comp.imgs}개 "
                f"{comp.head!r}\n"
                f"       새글 본문: {before['chars']}자/{before['imgs']}img → "
                f"{after['chars']}자/{after['imgs']}img")

        # 순서 검증 — 방금 붙인 내용이 본문 '맨 끝'에 있어야 한다.
        if comp.chars >= 10:
            tail_want = _norm(expected.get("text", ""))[-20:]
            if tail_want and tail_want not in _norm(after["text"])[-160:]:
                raise RuntimeError(
                    f"[복사] {no}/{total} 순서 검증 실패 — 붙여넣은 내용이 본문 끝에 없습니다.\n"
                    f"       기대 꼬리={tail_want!r}\n"
                    f"       실제 꼬리={_norm(after['text'])[-80:]!r}")

        self.log(f"[복사] {no}/{total} {comp.kind} 완료 "
                 f"(+{d_chars}자 +{d_imgs}img · 누적 {after['chars']}자/{after['imgs']}img)")

    # ── 4. 전체 검증 ────────────────────────────────────────────────
    async def verify_body(self, src, check_texts: bool = True) -> None:
        st = await self.stats()
        problems = []
        if st["imgs"] != src.total_images:
            problems.append(f"이미지 {st['imgs']}/{src.total_images}")
        else:
            self.log(f"[검증] 이미지 {st['imgs']}/{src.total_images}")

        body = _norm(st["text"])
        title = _norm(src.title)
        # ★'본문 글자에 제목 문자열이 있다'는 것만으로는 실패로 보지 않는다.
        #   원본 본문에 원래 같은 문구가 있는 글이 있다(2026-08-24 실측: gfa 팔자/현미).
        #   실패로 볼 것은 **제목 컴포넌트가 본문으로 복사된 경우**뿐이다.
        fr = await self._fr()
        tc = await fr.evaluate(TITLE_COMP_IN_BODY_JS, src.title or "")
        heads = [_norm(getattr(c, "head", "")) for c in (getattr(src, "components", None) or [])]
        key = title[:30]
        in_source = bool(key) and any(h.startswith(key) or key in h for h in heads if h)

        if tc.get("titleComp"):
            problems.append(
                f"제목 컴포넌트가 본문 영역으로 복사됐습니다 "
                f"(추가 {tc.get('titleComp')}개 / 문서 전체 {tc.get('allTitles')}개 "
                f"· 판정={tc.get('how')})")
        elif tc.get("firstEqualsTitle") and not in_source:
            problems.append(f"본문 첫 문단이 제목과 통째로 같습니다: {title!r}")
        elif len(title) >= 6 and title in body and not in_source:
            problems.append(f"원본에 없던 제목 문구가 본문에 생겼습니다: {title!r}")
        elif len(title) >= 6 and title in body and in_source:
            self.log(f"[검증] 본문에 제목과 같은 문구가 있지만 **원본에도 있는 문구**라 정상 "
                     f"({title[:24]!r})")
        else:
            self.log("[검증] 제목이 본문에 섞이지 않음 OK")

        for noise in UI_NOISE:
            if noise in body:
                problems.append(f"에디터 UI 문구가 본문에 복사됨: {noise!r}")
        # ★라벨 잔여 검사는 여기서 하지 않는다.
        #   링크가 걸린 이미지에는 스마트에디터가 span.se-image-link-icon('링크') 배지를 붙이는데,
        #   innerText 로 보면 그 배지가 라벨 문단처럼 보여 오탐한다(2026-08-20 실측).
        #   실제 검사는 cleanup_promo 의 dry-run(<p> 문단 대상)이 담당한다.
        if not any(n in body for n in UI_NOISE):
            self.log("[검증] 에디터 UI 문구 없음 OK")

        # 기준글 주요 텍스트가 결과에 있는지 — 텍스트 컴포넌트 앞부분으로 확인
        #   후처리(홍보문구 삭제) 뒤에는 의도적으로 사라진 문단이 있으므로 건너뛴다.
        missing = []
        for c in (src.components if check_texts else []):
            if c.kind == "image" or c.chars < 10:
                continue
            key = _norm(c.head)[:18]
            if key and key not in body:
                missing.append(key)
        if missing:
            problems.append(f"기준글 텍스트 {len(missing)}개가 본문에 없음: {missing[:3]}")
        elif check_texts:
            self.log(f"[검증] 기준글 주요 텍스트 전부 존재 OK "
                     f"({st['chars']}자 / 기준글 {src.total_chars}자)")
        else:
            self.log(f"[검증] 본문 {st['chars']}자 / 이미지 {st['imgs']}개 (후처리 반영)")

        if problems:
            raise RuntimeError("[검증] 실패 — " + " / ".join(problems))

    # ── 5. 후처리 ───────────────────────────────────────────────────
    async def cleanup_promo(self) -> None:
        """홍보 문구 제거 — 0개가 될 때까지 반복하고, dry-run 으로 다시 검증한다.

        ★제품 링크(링크카드·사람이 읽는 링크문구)와 제품 이미지는 지우지 않는다.
        """
        before = await self.stats()
        opt = {"promo": list(PROMO_PREFIXES), "exactRe": PROMO_EXACT_RE,
               "urlRe": URL_TEXT_RE, "labels": list(LINK_LABELS), "dryRun": False}
        removed_all, skipped_all = [], []
        for _ in range(5):
            fr = await self._fr()
            res = await fr.evaluate(CLEANUP_JS, opt)
            if not res.get("ok"):
                raise RuntimeError(f"[후처리] 실패 — {res.get('why')}")
            skipped_all = res["skipped"]
            if not res["removed"]:
                break
            removed_all += res["removed"]
            await self.page.wait_for_timeout(400)

        for r in removed_all:
            self.log(f"      제거({r['hit']}) {r['text']!r}")
            if r.get("hrefs"):
                self.log(f"               ⚠ 이 문단에 링크가 있었습니다: {r['hrefs']}")
            if r.get("html"):
                self.log(f"               html={r['html'][:150]}")
        for sk in skipped_all:
            self.log(f"      보존({sk['why']}) {sk['text']!r}")

        after = await self.stats()
        if after["imgs"] != before["imgs"]:
            raise RuntimeError(f"[후처리] 이미지가 사라졌습니다 "
                               f"{before['imgs']} → {after['imgs']} — 발행하지 않습니다")
        self.log(f"[후처리] repurely 홍보 텍스트 {len(removed_all)}개 제거 "
                 f"(본문 {before['chars']}자 → {after['chars']}자 · 이미지 {after['imgs']}개 유지)")

        # 재검증 — 지워야 할 문구가 남아 있으면 중단(보호 대상은 남아 있어도 정상)
        fr = await self._fr()
        again = await fr.evaluate(CLEANUP_JS, {**opt, "dryRun": True})
        if again.get("removed"):
            for r in again["removed"]:
                self.log(f"      ❌ 남음({r['hit']}) {r['text']!r}")
            raise RuntimeError(f"[후처리] 지우지 못한 홍보 문구 {len(again['removed'])}개")
        self.log("[후처리] 잔여 홍보 문구 없음 OK "
                 f"(보호된 제품 링크 {len(again.get('skipped') or [])}개)")

        # ★'링크' 같은 라벨이 <p> 가 아닌 곳에 박혀 있으면 위 규칙으로 안 지워진다.
        #   남아 있으면 꼬리 컴포넌트 구조를 그대로 찍어 다음 판단 근거로 남긴다.
        st = await self.stats()
        stray = [ln for ln in st["text"].splitlines()
                 if _norm(ln).lower() in [x.lower() for x in LINK_LABELS]]
        if stray:
            self.log(f"[후처리] ⚠ 라벨 문단이 남았습니다 {stray[:3]} — 꼬리 컴포넌트 구조:")
            for row in await fr.evaluate(TAIL_DUMP_JS, 3):
                self.log(f"         .{row['cls']} text={row['text']!r}")
                self.log(f"           {row['html']}")

    # ── 6. 중앙 정렬 ────────────────────────────────────────────────
    ALIGN_DROPDOWN_BTN = (
        'button.se-property-toolbar-drop-down-button[class*="se-align"]',
        'button[class*="se-property-toolbar-drop-down-button"][class*="align"]',
        'button.se-property-toolbar-drop-down-button',
    )
    ALIGN_CENTER_BTN = (
        "button.se-toolbar-option-align-center-button",
        '[data-name="align"][data-type="group-toggle"][class*="se-align-center"]',
        "button.se-align-group-toggle-toolbar-button.se-align-center",
        '.se-property-toolbar-image [class*="se-align-center"]',
    )

    async def _click_first(self, fr, selectors, label: str) -> bool:
        for sel in selectors:
            try:
                loc = fr.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2000)
                    await self.page.wait_for_timeout(350)
                    return True
            except Exception:                                  # noqa: BLE001
                continue
        self.log(f"      [{label}] 선택자 {len(selectors)}개 모두 실패")
        return False

    async def center_all(self) -> None:
        fr = await self._fr()
        st = await fr.evaluate(ALIGN_STATS_JS)
        self.log(f"[정렬] 시작 — 이미지 {st['imgCentered']}/{st['imgTotal']} 중앙 · "
                 f"텍스트 문단 {st['paraCentered']}/{st['paraTotal']} 중앙")

        for row in st["imgs"]:
            if row["centered"]:
                continue
            i = row["i"]
            # ★링크카드(se-section-oglink)는 안쪽 <img> 를 클릭하면 잡히지 않는다
            #   (2026-08-21 실측: TimeoutError). 컴포넌트 자체를 클릭해야 선택된다.
            is_card = "oglink" in (row["cls"] or "")
            sel = f'[data-v2-img="{i}"]' if is_card else f'[data-v2-img="{i}"] img'
            try:
                await fr.locator(sel).first.click(timeout=4000)
                await self.page.wait_for_timeout(500)
            except Exception as exc:                           # noqa: BLE001
                self.log(f"      {'카드' if is_card else '이미지'}#{i} 클릭 실패"
                         f"({type(exc).__name__}) — 강제 클릭으로 재시도")
                try:
                    await fr.locator(sel).first.click(timeout=4000, force=True)
                    await self.page.wait_for_timeout(500)
                except Exception as exc2:                      # noqa: BLE001
                    self.log(f"      #{i} 강제 클릭도 실패({type(exc2).__name__}) — "
                             f"class={row['cls']}")
                    continue
            fr = await self._fr()
            if not await self._click_first(fr, self.ALIGN_CENTER_BTN, f"이미지#{i} 정렬"):
                await self._click_first(fr, self.ALIGN_DROPDOWN_BTN, f"이미지#{i} 드롭다운")
                fr = await self._fr()
                await self._click_first(fr, self.ALIGN_CENTER_BTN, f"이미지#{i} 정렬(2단계)")
            await self.page.wait_for_timeout(350)
            fr = await self._fr()
            now = await fr.evaluate(ALIGN_STATS_JS)
            done = next((x for x in now["imgs"] if x["i"] == i), None)
            self.log(f"      {'카드' if is_card else '이미지'}#{i} 중앙정렬 "
                     f"{'✅' if done and done['centered'] else '❌'}")

        fr = await self._fr()
        st = await fr.evaluate(ALIGN_STATS_JS)
        self.log(f"[정렬] 결과 — 이미지 {st['imgCentered']}/{st['imgTotal']} 중앙 · "
                 f"텍스트 문단 {st['paraCentered']}/{st['paraTotal']} 중앙")
        if st["imgTotal"] and st["imgCentered"] < st["imgTotal"]:
            self.log("[정렬] ⚠ 중앙정렬되지 않은 이미지가 있습니다")
        else:
            self.log("[정렬] 중앙 정렬 완료")

    # ── 6-b. 제품 링크 카드 만들기 ──────────────────────────────────
    #   ★붙여넣기로는 이미지에 걸린 링크가 넘어오지 않는다(에디터 내부 모델에만 주소가 있다).
    #     대신 본문 맨 아래에 제품 URL 을 그대로 입력하면 네이버가 몇 초 뒤
    #     **제품 이미지가 붙은 링크 카드**로 자동 변환해 준다(2026-08-20 사용자 확인).
    async def append_product_link(self, url: str, wait_sec: float = 3.0) -> None:
        """본문 맨 아래에 제품 링크 카드를 만든다.

        ★타이핑(keyboard.type)으로는 카드가 만들어지지 않는다(2026-08-21 실측).
          사용자 방식대로 **URL 을 클립보드에 넣고 Ctrl+V** 해야 3초쯤 뒤에 제품 이미지가
          뜨면서 하이퍼링크가 자동으로 붙는다.
        ★카드를 클래스 이름(oglink 등)으로 찾으면 놓친다 — 카드가 화면에 멀쩡히 있는데
          0개로 셌다(2026-08-21 실측). **제품 도메인 링크 + 이미지를 함께 가진 컴포넌트**로 본다.
        ★붙여넣은 URL 텍스트가 카드 바로 위에 문단으로 남는다 → 그 <p> 만 지운다.
        """
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        before = await self.stats()
        self.log(f"[제품링크] 붙여넣기로 카드 생성 — {url}")
        await self.page.bring_to_front()
        await self.ensure_caret()
        await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(300)

        await self._to_clipboard(url)
        await self.page.keyboard.press("Control+V")

        waited, after = 0.0, before
        card = {"cards": 0, "hrefs": [], "imgs": 0, "tail": []}
        while waited < max(wait_sec, 3.0) + 17:
            await self.page.wait_for_timeout(700)
            waited += 0.7
            after = await self.stats()
            card = await self._product_card_state(host)
            if card["cards"]:
                break

        self.log(f"[제품링크] {waited:.1f}초 대기 — 이미지 {before['imgs']}개 → {after['imgs']}개 · "
                 f"제품카드 {card['cards']}개 · 본문 컴포넌트 {before['comps']}→{after['comps']}")
        for c in card.get("cls") or []:
            self.log(f"           카드 컴포넌트 — .{c}")
        for h in card["hrefs"]:
            self.log(f"           카드 링크 — {h[:110]}")
        if not card["cards"]:
            self.log("[제품링크] ⚠ 카드를 찾지 못했습니다 — 꼬리 컴포넌트 구조:")
            for t in card.get("tail") or []:
                self.log(f"           .{t['cls']} {t['chars']}자 이미지{t['imgs']}개 "
                         f"links={t['hrefs']}")
            raise RuntimeError("[제품링크] URL 을 붙여넣었지만 제품 링크 카드가 "
                               "만들어지지 않았습니다")
        self.log("[제품링크] 제품 이미지 + 하이퍼링크 카드 확인 ✅")

        # ★카드 위에 남은 URL 텍스트 문단 제거 — <p> 자체만 지운다.
        removed = await self._remove_url_paragraph(url, host)
        if not removed:
            self.log("[제품링크] 지울 URL 텍스트 문단 없음")
        for t in removed:
            self.log(f"[제품링크] URL 텍스트 문단 삭제 — {t[:60]!r}")
        # ★URL 을 지우고 남은 **빈 문단 한 줄**까지 Backspace 로 마저 지운다
        #   (2026-08-25 사용자 지시. 검수용/실전용 모두 이 경로를 탄다).
        await self._remove_blank_paragraph(host)
        done = await self.stats()
        still = await self._product_card_state(host)
        if not still["cards"]:
            raise RuntimeError("[제품링크] URL 문단을 지우다가 카드까지 사라졌습니다")
        if done["imgs"] != after["imgs"]:
            raise RuntimeError(f"[제품링크] URL 문단 삭제 후 이미지가 바뀌었습니다 "
                               f"{after['imgs']} → {done['imgs']}")
        self.log(f"[제품링크] 정리 완료 — 본문 {done['chars']}자 · 이미지 {done['imgs']}개 · "
                 f"제품카드 {still['cards']}개")

    async def _remove_url_paragraph(self, url: str, host: str) -> list:
        """붙여넣은 URL 문단을 **키보드로** 지운다.

        ★DOM 에서 `p.remove()` 하면 화면에서는 사라지지만 **발행하면 되살아난다**
          (2026-08-21 실측: 발행글 #9 마지막 문단에 URL 그대로 남음).
          스마트에디터는 자체 문서 모델을 저장하므로 DOM 조작은 모델에 반영되지 않는다.
          → 문단을 세 번 클릭해 선택하고 Delete 로 지운다(에디터 입력 경로를 탄다).
        """
        removed = []
        for _ in range(3):                                     # URL 문단이 여러 개일 수도 있다
            fr = await self._fr()
            spot = await fr.evaluate(FIND_URL_P_JS, {})
            if not spot.get("found"):
                break
            await self.page.bring_to_front()
            await self.page.mouse.click(spot["x"], spot["y"], click_count=3)
            await self.page.wait_for_timeout(250)
            await self.page.keyboard.press("Delete")
            await self.page.wait_for_timeout(250)
            await self.page.keyboard.press("Backspace")        # 빈 줄까지 정리
            await self.page.wait_for_timeout(350)

            fr = await self._fr()
            again = await fr.evaluate(FIND_URL_P_JS, {})
            if again.get("found") and again.get("text") == spot.get("text"):
                self.log(f"      URL 문단 삭제 실패(그대로 남음) — {spot['text'][:50]!r}")
                break
            removed.append(spot["text"])
        return removed

    async def _remove_blank_paragraph(self, host: str) -> bool:
        """제품 카드 바로 위에 남은 **빈 문단 한 줄**을 Backspace 로 마저 지운다.

        URL 을 붙여넣기 전에 Enter 를 한 번 눌러 문단을 만들기 때문에, URL 텍스트만
        지우면 빈 줄이 하나 남는다(2026-08-25 사용자 지시로 이 줄까지 제거).
        ★빈 문단일 때만 손댄다. 글자수/이미지수가 줄면 앞 문단이나 이미지를 먹은 것이므로
          **Ctrl+Z 로 되돌린다**(DOM 조작은 발행에 반영되지 않으니 키보드로만 처리).
        """
        fr = await self._fr()
        spot = await fr.evaluate(FIND_BLANK_P_JS, host)
        if not spot.get("found"):
            self.log("[제품링크] 카드 위 빈 문단 없음 — 추가 삭제 안 함")
            return False
        before = await self.stats()
        await self.page.bring_to_front()
        await self.page.mouse.click(spot["x"], spot["y"])
        await self.page.wait_for_timeout(200)
        await self.page.keyboard.press("Backspace")
        await self.page.wait_for_timeout(350)
        after = await self.stats()
        if after["chars"] < before["chars"] or after["imgs"] != before["imgs"]:
            self.log(f"[제품링크] ⚠ 빈 문단 삭제가 본문을 건드렸습니다 "
                     f"({before['chars']}자/{before['imgs']}장 → "
                     f"{after['chars']}자/{after['imgs']}장) — Ctrl+Z 로 되돌립니다")
            await self.page.keyboard.press("Control+Z")
            await self.page.wait_for_timeout(400)
            return False
        self.log(f"[제품링크] 카드 위 빈 문단 1줄 삭제 ✅ "
                 f"(컴포넌트 {before['comps']}→{after['comps']})")
        return True

    async def _to_clipboard(self, text: str) -> None:
        """URL 을 클립보드에 넣는다(붙여넣기용).

        navigator.clipboard 는 문서 포커스가 필요해 실패할 수 있어 execCommand 로 대비한다.
        """
        try:
            await self.page.evaluate("u => navigator.clipboard.writeText(u)", text)
            return
        except Exception as exc:                               # noqa: BLE001
            self.log(f"      clipboard.writeText 실패({type(exc).__name__}) — "
                     f"execCommand 로 재시도")
        if not await self.page.evaluate(TO_CLIP_JS, text):
            raise RuntimeError("[제품링크] URL 을 클립보드에 넣지 못했습니다")

    async def _product_card_state(self, host: str = "") -> dict:
        """제품 카드 = **제품 도메인 링크 + 이미지를 함께 가진 컴포넌트**. 꼬리 구조도 같이 준다."""
        fr = await self._fr()
        return await fr.evaluate(CARD_STATE_JS, host)

    # ── 7. 발행 ─────────────────────────────────────────────────────
    async def _publish_candidates(self) -> list:
        out = []
        for scope in [self.page] + list(self.page.frames):
            try:
                els = await scope.query_selector_all(
                    "button, a[role='button'], a, [role='button'], "
                    "[class*='publish'], [class*='confirm']")
            except Exception:                                  # noqa: BLE001
                continue
            for el in els:
                try:
                    if not await el.is_visible():
                        continue
                    info = await el.evaluate(
                        "e => ({t:(e.innerText||e.textContent||'').trim().slice(0,20),"
                        " c:(e.className||'').toString().slice(0,60), id:e.id||'',"
                        " tag:e.tagName.toLowerCase()})")
                except Exception:                              # noqa: BLE001
                    continue
                txt, cls = info.get("t", ""), info.get("c", "")
                if ("발행" not in txt and "publish" not in cls.lower()
                        and "confirm" not in cls.lower()):
                    continue
                rank = 0 if txt == "발행" else (1 if "confirm" in cls.lower() else 2)
                out.append({"handle": el, "rank": rank,
                            "desc": f"{info['tag']}.{cls[:26]}#{info['id'][:12]} {txt!r}"})
        out.sort(key=lambda x: x["rank"])
        return out

    async def disable_comments(self) -> bool:
        """발행 레이어에서 '댓글 허용' 을 끈다. 이미 꺼져 있으면 건드리지 않는다(토글)."""
        for scope in [self.page] + list(self.page.frames):
            try:
                found = await scope.evaluate(r"""() => {
                     const txt = el => (el.innerText || el.textContent || '')
                           .replace(/\s+/g, ' ').trim();
                     const boxes = Array.from(document.querySelectorAll("input[type='checkbox']"))
                           .filter(b => b.offsetParent !== null || b.closest('label'));
                     for (const b of boxes) {
                       let label = '';
                       if (b.id) {
                         const l = document.querySelector('label[for="' + b.id + '"]');
                         if (l) label = txt(l);
                       }
                       if (!label && b.closest('label')) label = txt(b.closest('label'));
                       if (!label && b.parentElement) label = txt(b.parentElement);
                       if (label.indexOf('댓글') >= 0) {
                         b.setAttribute('data-v2-comment', '1');
                         return {checked: b.checked, label: label.slice(0, 30)};
                       }
                     }
                     return null;
                   }""")
            except Exception:                                  # noqa: BLE001
                continue
            if not found:
                continue
            if not found["checked"]:
                self.log(f"[발행설정] 댓글 허용 이미 OFF — 그대로 둠 ({found['label']!r})")
                return True
            try:
                await scope.locator("[data-v2-comment='1']").first.click(timeout=3000, force=True)
                await self.page.wait_for_timeout(500)
                still = await scope.evaluate(
                    "() => { const b=document.querySelector(\"[data-v2-comment='1']\");"
                    " return b ? b.checked : null; }")
                if still is False:
                    self.log(f"[발행설정] 댓글 허용 OFF 확인 ({found['label']!r})")
                    return True
                self.log(f"[발행설정] ❌ 댓글 해제 실패(checked={still})")
            except Exception as exc:                           # noqa: BLE001
                self.log(f"[발행설정] ❌ 댓글 해제 클릭 실패: {type(exc).__name__}")
        self.log("[발행설정] ❌ '댓글' 체크박스를 찾지 못했습니다")
        return False

    async def publish(self, require_comments_off: bool = True) -> str:
        before_url = self.page.url
        await self.page.bring_to_front()
        first = None
        for step in (1, 2):
            cands = await self._publish_candidates()
            clicked = False
            for c in cands:
                if step == 2 and first is not None:
                    try:
                        if await c["handle"].evaluate("(e, o) => e === o", first):
                            continue
                    except Exception:                          # noqa: BLE001
                        pass
                try:
                    await c["handle"].click(timeout=3000)
                    clicked = True
                    if step == 1:
                        first = c["handle"]
                    self.log(f"[발행] {step}단계 버튼 클릭 — {c['desc']}")
                    break
                except Exception:                              # noqa: BLE001
                    continue
            if not clicked:
                raise RuntimeError(f"[발행] {step}단계 버튼을 찾지 못했습니다(후보 {len(cands)}개)")
            await self.page.wait_for_timeout(1500)
            if step == 1:
                ok = await self.disable_comments()
                if require_comments_off and not ok:
                    raise RuntimeError("[발행] 댓글 허용 OFF 를 확인하지 못해 발행을 중단합니다")

        for _ in range(40):
            u = self.page.url or ""
            if u != before_url and ("logno" in u.lower() or "postview" in u.lower()
                                    or "postwrite" not in u.lower()):
                return clean_post_url(u)
            await self.page.wait_for_timeout(500)
        raise RuntimeError(f"[발행] 게시글 페이지로 이동하지 않았습니다(url={self.page.url[:80]})")


def clean_post_url(url: str) -> str:
    u = url or ""
    if "postview" not in u.lower():
        return u
    bid = re.search(r"blogId=([^&#]+)", u, re.I)
    no = re.search(r"logNo=(\d+)", u, re.I)
    return f"https://blog.naver.com/{bid.group(1)}/{no.group(1)}" if bid and no else u


# ── 새 글 탭 열기 ──────────────────────────────────────────────────────
async def _popup_visible(page) -> bool:
    for scope in [page] + list(page.frames):
        for mark in POPUP_MARKS:
            try:
                if await scope.locator(f"text={mark}").first.count() > 0:
                    return True
            except Exception:                                  # noqa: BLE001
                continue
    return False


async def _click_popup_cancel(page) -> bool:
    """'취소' 만 누른다. '확인'(이어쓰기)은 어떤 경우에도 누르지 않는다."""
    for scope in [page] + list(page.frames):
        for sel in ("button:has-text('취소')", "a:has-text('취소')"):
            try:
                loc = scope.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    return True
            except Exception:                                  # noqa: BLE001
                pass
        try:
            if await scope.evaluate(r"""(marks) => {
                 const all = Array.from(document.querySelectorAll('*'));
                 const host = all.find(el =>
                   marks.some(m => (el.innerText || '').indexOf(m) >= 0)
                   && el.querySelectorAll('button,a,[role=button]').length
                   && (el.innerText || '').length < 400);
                 if (!host) return false;
                 const btn = Array.from(host.querySelectorAll('button,a,[role=button]'))
                   .find(b => ((b.innerText || '').trim()) === '취소');
                 if (!btn) return false;
                 btn.click();
                 return true;
               }""", list(POPUP_MARKS)):
                return True
        except Exception:                                      # noqa: BLE001
            pass
    return False


async def dismiss_draft_popup(page, log, timeout_ms: int = 10_000) -> None:
    """'작성 중인 글이 있습니다' 팝업을 최대 10초 폴링해 '취소'(새 글)로 닫는다.
    ★0초 판정 금지 — 팝업이 화면을 덮은 채 진행하면 이후 단계가 전부 실패한다."""
    waited = 0
    while waited < timeout_ms:
        if await _popup_visible(page):
            log("[새글] '작성 중인 글' 팝업 감지 → 취소(새 글)")
            for _ in range(3):
                if await _click_popup_cancel(page):
                    await page.wait_for_timeout(800)
                    if not await _popup_visible(page):
                        log("[새글] 팝업 닫힘 확인")
                        return
                await page.wait_for_timeout(500)
            raise RuntimeError("[새글] '작성 중인 글' 팝업을 닫지 못했습니다")
        await page.wait_for_timeout(400)
        waited += 400
    log("[새글] 작성 중인 글 팝업 없음")


async def open_write(ctx, blog_id: str, log) -> NewPost:
    """새 글쓰기 화면을 **항상 새 탭**으로 연다.
    ★같은 탭에서 이동시키면 앞서 완성해 둔 글이 통째로 날아간다(2026-08-20 사고)."""
    page = await ctx.new_page()
    await page.goto(f"https://blog.naver.com/{blog_id}/postwrite", wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    if "nidlogin" in (page.url or ""):
        raise RuntimeError("새 글쓰기가 로그인 페이지로 튕겼습니다. 로그인 상태를 확인하세요.")
    await dismiss_draft_popup(page, log)
    frame = await browser.find_editor_frame(page, log, "새글", timeout_sec=40, min_score=5)
    log(f"[새글] 글쓰기 탭 열기 완료 — {page.url[:70]}")
    return NewPost(page, frame, log)


async def sleep_between(seconds: float) -> None:
    await asyncio.sleep(seconds)
