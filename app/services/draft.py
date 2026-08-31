"""LandingBrief → 네이버 블로그용 신규 원고(초안) 작성.

설계 원칙
  · 원문 문장을 **가져오지 않는다**. 브리프에서 넘어오는 건 짧은 키워드/구뿐이고,
    문장은 전부 이 모듈의 템플릿이 직접 만든다 → 구조적으로 복제가 불가능하다.
  · 랜딩에 없는 수치·후기·효능은 만들지 않는다. 효과 관련 서술은 전부 단정하지 않는 표현으로 쓴다.
  · 완성 후 `check_overlap()` 으로 원문과 12자 이상 겹치는 구간이 있는지 기계적으로 재검증한다.
  · 문단은 짧게(네이버 블로그 가독성).

흐름: 도입부 → 고민/문제 → 계기 → 사용/경험 → 변화 및 느낀 점 → 제품 소개 → 마무리
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.services.reference import BANNED_EXPRESSIONS, LandingBrief

_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class BlogDraft:
    title: str
    body: str
    source_url: str

    @property
    def paragraphs(self) -> list[str]:
        return [p for p in self.body.split("\n\n") if p.strip()]


def _pick(items: list[str], seed: int, fallback: str = "") -> str:
    items = [i for i in items if i]
    if not items:
        return fallback
    return items[seed % len(items)]


def _has_batchim(word: str) -> bool:
    """마지막 글자에 받침이 있는지(한글이 아니면 있는 것으로 취급)."""
    for ch in reversed(word.strip()):
        if ch.isspace():
            continue
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28 != 0
        return not ch.isdigit()          # 영문/기호는 받침 있는 쪽으로
    return False


def _p(word: str, with_batchim: str, without: str) -> str:
    """조사 자동 선택. 예: _p('앰플', '을', '를') → '앰플를'이 아니라 '앰플을'."""
    return word + (with_batchim if _has_batchim(word) else without)


_TAIL_TRIM = re.compile(
    r"(?:이|가|은|는|을|를)?\s*"
    r"(?:때문에|때문|으로|로|이라|라서|해서|하고|고민|문제|신경|걱정|스트레스|쓰여요|쓰이는)\s*$"
)


def _stem(phrase: str) -> str:
    """추출 구의 꼬리 연결어를 잘라 템플릿 문장에 자연스럽게 얹히게 한다."""
    s = _SPACES.sub(" ", phrase or "").strip()
    for _ in range(3):                    # '…때문에 고민' 처럼 두 번 붙은 경우까지
        new = _TAIL_TRIM.sub("", s).strip()
        if new == s:
            break
        s = new
    return s or phrase.strip()


def _topic(brief: LandingBrief) -> str:
    """글 전체를 관통할 주제어. 제품명이 길면 앞부분만."""
    t = brief.product or brief.page_title or "이 제품"
    t = _SPACES.sub(" ", t).strip()
    return t[:24]


def _concern(brief: LandingBrief) -> str:
    return _pick(brief.problems, 3, fallback="요즘 부쩍 신경 쓰이던 부분")


def copy_source(brief: LandingBrief) -> BlogDraft:
    """기본 모드 — 참고 랜딩 **원문을 그대로** 옮긴다(재작성 없음).

    랜딩에서 순서대로 뽑아둔 본문 블록을 그대로 이어붙이고, 제목은 랜딩 제목을 쓴다.
    문장을 바꾸지 않으므로 여기서는 원문 중복 검사(check_overlap)를 하지 않는다.
    """
    title = _SPACES.sub(" ", brief.page_title or brief.product or "").strip()
    # 네이버 블로그 글을 참고할 때 붙는 ' : 네이버 블로그' 꼬리는 제목에 넣지 않는다.
    title = re.sub(r"\s*[:|-]\s*네이버\s*블로그\s*$", "", title).strip()[:100]
    body = brief.content_text()
    if not body:
        # 블록 추출이 비면 페이지 전체 텍스트로 폴백
        body = _SPACES.sub(" ", brief.raw_text).strip()
    return BlogDraft(title=title or "(제목 없음)", body=body, source_url=brief.url)


def compose(brief: LandingBrief) -> BlogDraft:
    """브리프로 초안 1편을 만든다. 같은 URL이면 같은 결과(재현 가능)."""
    seed = int(hashlib.sha256(brief.url.encode("utf-8")).hexdigest()[:8], 16)
    topic = _topic(brief)
    concern = _stem(_concern(brief))
    audience = brief.audience
    appeal = _pick(brief.appeals, seed + 1)
    routine = _pick(brief.experiences, seed + 2)
    spec = _pick(brief.product_points, seed + 3)

    # ── 제목 ──
    title_forms = [
        f"{concern} 때문에 {topic} 써본 솔직한 기록",
        f"{topic}, 한동안 써보고 남기는 사용기",
        f"{concern}으로 고민하다 {topic} 만나본 이야기",
        f"{topic} 며칠 써보고 느낀 점 정리",
    ]
    title = _SPACES.sub(" ", title_forms[seed % len(title_forms)]).strip()[:60]

    p: list[str] = []

    # ① 도입부 — 인사와 오늘 글의 주제만. 사실 주장 없음.
    p.append(
        "안녕하세요.\n"
        "오늘은 요즘 제가 관심 두고 있던 것에 대해 적어보려고 해요."
    )

    # ② 고민 / 문제 제기 — 랜딩이 짚은 지점을 '내 고민'의 형태로 다시 씀
    q = concern or "거울을 볼 때마다 마음에 걸리던 부분"
    p.append(
        f"사실 저는 {_p(q, '이', '가')} 계속 마음에 걸렸어요.\n"
        "크게 티가 나는 건 아닌데, 신경 쓰이기 시작하니까 계속 눈이 가더라고요."
    )
    if audience:
        p.append(f"저처럼 {_p(_stem(audience), '이라면', '라면')} 비슷한 고민 한 번쯤 해보셨을 것 같아요.")

    # ③ 계기 — 어떻게 알게 됐는지(정보 탐색 과정). 수치·후기 인용 없음.
    p.append(
        "그러다 이것저것 찾아보게 됐어요.\n"
        f"여러 개를 비교하다가 {_p(topic, '을', '를')} 알게 됐고, 설명을 좀 더 읽어봤습니다."
    )
    if appeal and appeal != topic:
        p.append(f"소개에서 강조하는 부분은 '{appeal}' 쪽이었어요.")

    # ④ 사용 / 경험 — 루틴 위주. 기간·횟수 같은 수치는 쓰지 않는다.
    use = f"{routine} 위주로" if routine else "평소 하던 순서 그대로"
    p.append(
        f"받아보고 나서는 {use} 써봤어요.\n"
        "특별히 번거로운 건 없었고, 하던 루틴에 얹는 정도였습니다."
    )

    # ⑤ 변화 및 느낀 점 — 전부 비확정 표현. 효과 단정 금지.
    p.append(
        "짧은 기간에 뭐가 달라졌다고 말하긴 어렵고요.\n"
        "다만 매일 챙기다 보니 신경 쓰던 부분을 덜 들여다보게 되긴 했어요.\n"
        "이건 어디까지나 제 느낌이라 사람마다 다를 수 있어요."
    )

    # ⑥ 제품 소개 — 랜딩에 적힌 항목만, 짧게.
    if spec:
        p.append(f"제품 쪽은 '{spec}' 부분이 눈에 들어왔어요.\n자세한 건 상세 페이지에 정리돼 있습니다.")
    else:
        p.append("제품에 대한 설명은 상세 페이지에 정리돼 있어요.")

    # ⑦ 마무리 — 과장 없이. CTA는 강권하지 않는 톤.
    p.append(
        "정리하면, 저는 비슷한 고민이 있어서 한번 써보게 된 케이스예요.\n"
        "궁금하신 분은 직접 확인해보시고 판단하시는 게 제일 좋을 것 같아요."
    )
    p.append("읽어주셔서 감사합니다.")

    body = "\n\n".join(seg.strip() for seg in p if seg.strip())
    return BlogDraft(title=title, body=body, source_url=brief.url)


# ── 검증 ────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "")


def check_overlap(draft: BlogDraft, raw_text: str, n: int = 12) -> list[str]:
    """초안과 원문이 n자 이상 연속으로 겹치는 구간 목록(공백·문장부호 무시)."""
    a, b = _norm(draft.title + draft.body), _norm(raw_text)
    if not a or not b:
        return []
    found: list[str] = []
    for i in range(len(a) - n + 1):
        chunk = a[i:i + n]
        if chunk in b and chunk not in found:
            found.append(chunk)
    return found


def check_banned(draft: BlogDraft) -> list[str]:
    """의학적 확정·과장 표현이 섞였는지."""
    text = draft.title + draft.body
    return [w for w in BANNED_EXPRESSIONS if w in text]


def check_digits(draft: BlogDraft) -> list[str]:
    """임의 수치(기간·퍼센트·개수)가 들어갔는지 — 랜딩에 없는 수치 창작 방지."""
    return re.findall(r"\d+\s*(?:%|퍼센트|일|주|개월|년|배|만원|원)", draft.title + draft.body)


def verify(draft: BlogDraft, brief: LandingBrief) -> tuple[bool, list[str]]:
    """초안이 규칙을 지켰는지 일괄 점검. (통과여부, 문제목록)"""
    problems: list[str] = []
    overlaps = check_overlap(draft, brief.raw_text)
    if overlaps:
        problems.append(f"원문과 12자 이상 겹치는 구간 {len(overlaps)}건: {overlaps[:3]}")
    banned = check_banned(draft)
    if banned:
        problems.append(f"금지 표현 포함: {banned}")
    digits = check_digits(draft)
    if digits:
        problems.append(f"임의 수치 의심: {digits}")
    return (not problems), problems
