r"""블로그 랜딩 생성기 — Streamlit UI.

    실행_Streamlit.bat   (또는  python -m streamlit run streamlit_app.py)

★이 화면은 **Playwright 를 직접 실행하지 않는다.** 고른 값으로 `Job` 을 만들어 큐에 넣기만
  하고(`v2.queue_store`), 실제 실행은 그 PC 의 로컬 에이전트(`v2.agent`)가 한다.

    Streamlit(UI) ──submit──▶ 큐 ──claim──▶ Local Agent ──▶ v2.run / v2.run_production
                     ▲                          │
                     └────── 로그·@@EVENT ───────┘

  · 화면이 하는 일은 **Job 조립 + 큐 저장 + 상태 보기** 뿐이다.
  · 브랜드를 고르면 기준시트와 UTM 빌더가 **한 세트로** 따라온다(`brands.json`).
  · `ready:false` 인 브랜드(준비 중)는 실행 버튼이 잠긴다. CLI 쪽에서도 한 번 더 막는다.
  · 네이버 로그인 세션은 항상 실행하는 PC 의 `sessions/<account>/` 에만 남는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import (accounts, batch_plan, brands, catalog, landing_sheet,  # noqa: E402
                queue_store, session_store, sheets, timeutil)
from v2 import pairing                                                   # noqa: E402
from v2.login_state import LABEL as GRADE_LABEL                          # noqa: E402
from v2.job import FLOWS, KINDS, PROD_MODES, Job, python_exe              # noqa: E402

st.set_page_config(page_title="시리즈빌더", page_icon="📝", layout="wide")

# ── 표시용 CSS (기능과 무관) ──────────────────────────────────────
#   · 제목은 기본보다 살짝 작게
#   · 브랜드/기준시트/UTM 빌더 안내는 **본문 글씨 크기·색**으로(st.caption 은 작고 연하다)
st.markdown("""
<style>
  /* 상단 여백을 '조금만' 줄인다.
     ★Streamlit 고정 헤더가 약 3.75rem 이라 그보다 작게 주면 제목이 헤더 뒤로 잘린다.
       기본값(약 6rem)에서 4.75rem 으로만 줄인다 — 음수 margin·translate 는 쓰지 않는다. */
  .block-container, [data-testid="stMainBlockContainer"] {
      padding-top: 4.75rem !important;
  }
  h1 { font-size: 1.9rem !important;
       padding-top: .25rem !important; padding-bottom: .1rem !important;
       margin-bottom: .35rem !important; }
  /* 라벨 → 줄바꿈 → 값. 항목마다 한 칸 띄운다(한 줄에 이어붙이지 않는다) */
  .info-block { margin: 0 0 .5rem 0; }
  .info-item  { margin: 0 0 .6rem 0; }
  .info-label { font-size: 1rem; font-weight: 700; line-height: 1.35;
                color: inherit; }
  .info-value { font-size: 1rem; line-height: 1.35; color: inherit; }
  .pair-code  { font-size: 2rem; font-weight: 700; letter-spacing: .35em;
                text-align: center; padding: .5rem 0; margin: .3rem 0;
                border: 1px dashed currentColor; border-radius: .5rem; }
</style>
""", unsafe_allow_html=True)


def info_block(*pairs: tuple[str, str]) -> str:
    """`라벨 / 값` 을 항목마다 줄을 바꿔 쌓는다(본문 크기 · 라벨만 굵게)."""
    items = "".join(
        f"<div class='info-item'><div class='info-label'>{label}</div>"
        f"<div class='info-value'>{value}</div></div>"
        for label, value in pairs)
    return f"<div class='info-block'>{items}</div>"

RUNNING_STATES = (queue_store.PENDING, queue_store.RUNNING)
NEWLINE = chr(10)
BADGE = {queue_store.PENDING: "⏳ 대기", queue_store.RUNNING: "▶ 실행 중",
         queue_store.DONE: "✅ 완료", queue_store.FAILED: "❌ 실패",
         queue_store.CANCELED: "⏹ 중단됨"}

# @@EVENT 의 stage 이름 → 사람이 읽는 단계 이름
STAGE_LABELS = {
    "brand_config": "브랜드 설정",
    "reference_sheet_selected": "기준시트 선택",
    "reference_sheet_access": "기준시트 접근",
    "utm_sheet_selected": "UTM 빌더 선택",
    "utm_sheet_access": "UTM 빌더 접근",
    "reference_lookup": "기준글 조회",
    "result_columns": "결과 기록 열 확인",
    "row_match": "대상 행 매칭",
    "product_url_lookup": "최종 제품 URL 조회",
    "product_link_find": "기존 제품링크 탐색",
    "product_link_remove": "기존 제품링크 제거",
    "product_link_insert": "신규 제품링크 입력",
    "product_link_verify": "신규 링크 검증",
    "post_build": "글 작성",
    "sheet_mark_done": "시트 완료 표시",
}


# ══════════════════════════════════════════════════════════════════
# 캐시 (읽기 전용 — 브라우저를 켜지 않는다)
# ══════════════════════════════════════════════════════════════════
@st.cache_resource
def get_store():
    """큐 연결 객체(프로세스에 한 번 만들어 재사용한다).

    ★2026-11-03 — 원격 큐를 쓰는 설정이면 **strict**: 붙지 못하면 로컬 큐로
      조용히 내려가지 않고 예외를 던진다. 예전에는 조용히 내려간 객체가 이
      캐시에 박혀서, 작업을 넣어도 PC 가 못 보는 상태를 **관리자 재부팅** 말고는
      풀 수 없었다. 예외는 캐시되지 않으므로 [연결 다시 맺기]·새로고침으로
      곧바로 다시 시도된다.
    """
    return queue_store.get_store(strict=queue_store.wants_remote())


@st.cache_data(ttl=300, show_spinner=False)
def load_catalog(brand_id: str, account_id: str, ref_tab: str,
                 refresh: bool = False) -> dict:
    """★브랜드가 캐시 키의 맨 앞이다 — 브랜드끼리 목록이 섞이면 안 된다."""
    # ★시트를 읽는 데 필요한 것은 **브랜드와 탭**이다. 계정 목록 파일이 있느냐에
    #   매이면 안 된다(클라우드에는 그 파일이 없어 시트조차 못 읽었다).
    try:
        acc = accounts.resolve(account_id) if account_id else None
    except Exception:                                          # noqa: BLE001
        acc = None
    return catalog.load(acc, ref_tab=ref_tab, refresh=refresh, brand=brand_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_tabs(brand_id: str, refresh: bool = False) -> dict:
    """`기준 계정` 선택지 = **그 브랜드 기준시트의 `… 기준랜딩` 탭들**.

    ★코드에 계정을 박지 않는다. 시트에 탭을 추가하면 화면에 그대로 늘어난다.
    """
    return catalog.load_tabs(brand_id, refresh=refresh)


@st.cache_data(ttl=120, show_spinner=False)
def preview_rows(brand_id: str, flow: str, media: str, deficiency: str, kind: str,
                 date: str, campaign: str, mode: str) -> dict:
    """실행 전에 시트만 읽어 '무엇이 매칭됐고 어떤 제품 URL 이 들어가는지' 보여준다.

    ★읽기 전용이다. Playwright 도, 시트 쓰기도 하지 않는다(쓰기 자리 확인은 Dry-run 에서).
    """
    from v2.config import load_settings
    brand = brands.resolve(brand_id)
    brand.require_ready()
    sheets.set_brand(brand)
    landing_sheet.set_brand(brand)
    settings = load_settings()
    settings.check()
    lines: list[str] = []
    out: dict = {"brand": brand.summary(), "rows": [], "reference": None,
                 "log": lines}

    def log(msg: str = "") -> None:
        lines.append(str(msg))

    ref = sheets.find_reference(settings.service_account_json,
                                settings.spreadsheet_id, media, deficiency, kind)
    out["reference"] = {"url": ref.url, "row": ref.row,
                        "product_url": ref.product_url}
    if flow == "production":
        finder = (landing_sheet.find_pending_rows if mode == "create"
                  else landing_sheet.find_published_rows)
        rows = finder(settings.service_account_json, settings.spreadsheet_id,
                      media, date, log, campaign=campaign or "",
                      note=deficiency or "", on_missing="drop")
        out["rows"] = [{"행": r["row"], "순번": r.get("seq", ""),
                        "utm_campaign": r.get("campaign", ""),
                        "최종 제품 URL": r.get("product_url", ""),
                        "블로그 링크": r.get("blog_url", "")} for r in rows]
    return out


# ══════════════════════════════════════════════════════════════════
# 접근 · 내 PC(Agent) — 실행은 언제나 **사용자 PC 의 Agent** 가 한다.
#   화면은 큐에 넣기만 하고, Playwright 는 클라우드에서 절대 돌지 않는다.
# ══════════════════════════════════════════════════════════════════
# 화면 버전 — 배포할 때마다 바뀐다. "지금 보는 게 최신인가" 를 눈으로 확인하려고 둔다.
#   (클라우드는 새로고침해도 옛 화면이 잠깐 남을 수 있어, 이 번호로 가른다)
APP_VERSION = "11-03 10:45"


def _job_has(field: str) -> bool:
    """지금 import 된 `Job` 이 이 필드를 아는가.

    ★클라우드가 화면(streamlit_app.py)만 새로 읽고 `v2.job` 은 옛 모듈을 그대로 쓸 때가 있다.
      새 옵션을 생성자에 그냥 넘기면 그 순간 화면 전체가 TypeError 로 죽으므로,
      **넘기기 전에 물어본다.** 옛 모듈이면 그 옵션만 빠지고 화면은 산다.
    """
    try:
        return field in Job.__dataclass_fields__                # noqa: SLF001
    except Exception:                                           # noqa: BLE001
        return False

AGENT_DOWNLOAD_URL = ("https://github.com/memeanji/blog-landing-generator/"
                      "releases/latest/download/BlogLandingAgentSetup.exe")
AGENT_RELEASES_URL = "https://github.com/memeanji/blog-landing-generator/releases/latest"


# 비밀번호를 담는 키 이름(먼저 찾은 것을 쓴다). ★값은 코드에 두지 않는다.
PASSWORD_KEYS = ("TEAM_PASSWORD", "APP_PASSWORD")


def app_password() -> str:
    """팀 공용 비밀번호 — `.streamlit/secrets.toml` 또는 Streamlit Secrets 에서 읽는다.

    로컬:      .streamlit/secrets.toml   TEAM_PASSWORD = "…"
    클라우드:  앱 Settings → Secrets      TEAM_PASSWORD = "…"
    ★코드·로그·화면 어디에도 값이 남지 않는다(비교만 한다).
    """
    for key in PASSWORD_KEYS:
        try:
            v = st.secrets.get(key, "")
        except Exception:                                      # noqa: BLE001
            v = ""
        v = str(v or os.getenv(key) or "").strip()
        if v:
            return v
    return ""


def require_password() -> bool:
    """비밀번호가 설정돼 있으면 통과할 때까지 화면을 막는다."""
    want = app_password()
    if not want:                       # 설정 전(로컬 개발 등) — 막지 않는다
        return True
    if st.session_state.get("authed"):
        return True
    st.title("네이버 블로그 랜딩 생성기")
    st.markdown(info_block(("접근", "팀 공용 비밀번호를 입력해 주세요")),
                unsafe_allow_html=True)
    with st.form("gate"):
        pw = st.text_input("비밀번호", type="password", key="gate_pw")
        if st.form_submit_button("들어가기", type="primary"):
            if pw == want:
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("비밀번호가 다릅니다.")
    st.caption("비밀번호는 앱 Secrets 에만 저장됩니다(저장소·코드에는 없습니다).")
    return False


def my_device(store) -> dict | None:
    """이 브라우저가 연결한 PC. 링크(?device=…) → 살아 있는 Agent 순으로 찾는다."""
    want = st.query_params.get("device") or st.session_state.get("device_id") or ""
    if isinstance(want, (list, tuple)):   # 주소에 같은 이름이 여러 번 올 수 있다
        want = want[0] if want else ""
    want = str(want).strip()
    if want:
        dev = store.device(want) if hasattr(store, "device") else None
        if dev:
            st.session_state["device_id"] = dev["device_id"]
            return dev
    # 자동으로 잡아 주는 건 **내 PC 에서 띄운 화면**일 때만이다.
    #   ★서버 화면에서도 이렇게 했더니, 팀 비밀번호만 아는 사람이 들어와
    #     [실행] 을 누르면 **남의 PC 에서 그 사람 네이버 계정으로** 글이 올라갔다.
    #     서버 화면에서는 아래 '어느 PC 에서 돌릴지' 를 직접 고르게 한다.
    if not sys.platform.startswith("win"):
        return None
    alive = [a for a in store.agents(max_age_sec=30)
             if a.get("alive") and a.get("state") != "stopped"]
    if len(alive) == 1:
        a = alive[0]
        return {"device_id": a["agent"],
                "label": a.get("label") or a.get("host") or a["agent"],
                "state": a.get("state", "idle"), "alive": True,
                "version": a.get("version", ""), "last_seen": a.get("at", "")}
    return None


def link_device(device_id: str) -> None:
    """이 브라우저가 쓸 PC 를 정한다.

    ★주소(query params)는 여기서 바꾸지 않는다. 화면을 그리는 도중에 바꾸면
      그것만으로도 다시 그리기가 걸려, 방금 그린 요소와 부딪혀 빨간 오류
      (removeChild)가 났다. 바꾸는 일은 맨 위 `sync_address()` 에서 한 번만 한다.
    """
    st.session_state["device_id"] = device_id
    st.session_state["_address"] = device_id       # 다음 그리기 때 주소에 반영
    st.session_state["_just_linked"] = device_id   # 알림도 다음 그리기 때


def unlink_device() -> None:
    """연결을 끊는다(주소 정리는 마찬가지로 맨 위에서 한 번만)."""
    st.session_state.pop("device_id", None)
    st.session_state["_address"] = ""
    st.session_state.pop("_just_linked", None)


def sync_address() -> None:
    """주소를 세션 상태에 맞춘다 — **화면을 그리기 전에 한 번만** 부른다."""
    # ★확인 대기 중인 멱등성 키는 `_address` 와 **무관하게** 항상 맞춘다
    #   (아래 early return 뒤에 두면 새로고침 복구가 안 된다).
    sync_pending_key()
    want = st.session_state.pop("_address", None)
    if want is None:
        return
    try:
        if want:
            if st.query_params.get("device") != want:
                st.query_params["device"] = want
        else:
            st.query_params.pop("device", None)
    except Exception:                              # noqa: BLE001
        pass


def sync_pending_key() -> None:
    """'등록 여부 확인 불가' 멱등성 키를 **주소에도** 남긴다.

    ★`st.session_state` 는 브라우저를 새로 고치면 사라진다. 그러면 확인해야 할
      작업의 키를 잃어버리고, 사용자는 같은 작업을 다시 넣게 된다(중복 발행).
      주소(`?pending=…`)에 있으면 새로고침·재접속 후에도 그 작업을 조회할 수 있다.
      ※키는 무작위 식별자일 뿐이라 주소에 남아도 비밀이 새지 않는다.
    """
    try:
        pend = st.session_state.get("_pending_submit") or {}
        key = pend.get("key") or ""
        if not key:                                # 세션에 없으면 주소에서 되살린다
            from_url = st.query_params.get("pending") or ""
            if isinstance(from_url, (list, tuple)):
                from_url = from_url[0] if from_url else ""
            if from_url:
                st.session_state["_pending_submit"] = {
                    "key": str(from_url), "label": "작업", "at": "",
                    "from_url": True}
            return
        if st.query_params.get("pending") != key:
            st.query_params["pending"] = key
    except Exception:                              # noqa: BLE001
        pass


def clear_pending_key() -> None:
    """확인이 끝났으면 세션과 주소에서 모두 지운다."""
    st.session_state.pop("_pending_submit", None)
    try:
        st.query_params.pop("pending", None)
    except Exception:                              # noqa: BLE001
        pass


def running_job(store, device) -> str:
    """이 PC 에서 지금 돌고 있는 작업 제목(없으면 빈 문자열) — 중복 실행 방지용."""
    if not device:
        return ""
    want = device.get("device_id")
    for rec in store.list_jobs(limit=12):
        if rec.get("status") not in (queue_store.PENDING, queue_store.RUNNING):
            continue
        if rec.get("target_agent") in ("", want) or rec.get("agent") == want:
            return str(rec.get("title") or rec.get("id"))
    return ""


def render_agent_panel(store) -> dict | None:
    """사이드바 — 🟢/🔴 상태 · 설치 버튼 · 6자리 페어링."""
    st.header("내 PC Agent")
    dev = my_device(store)
    just = st.session_state.pop("_linked_name", None)
    if just:
        st.toast(f"연결됐습니다 — {just}")
    if dev and dev.get("alive"):
        st.success("🟢 연결됨")
        st.markdown(info_block(("PC 이름", dev.get("label") or dev["device_id"]),
                               ("마지막 연결", (dev.get("last_seen") or "")[11:16] or "-")),
                    unsafe_allow_html=True)
        if dev.get("version"):
            st.caption(f"Agent 버전 {dev['version']}")
        if st.button("연결 해제", use_container_width=True):
            unlink_device()
            st.rerun()
        return dev

    if dev and not dev.get("alive"):
        st.warning("🟡 연결은 돼 있지만 Agent 가 꺼져 있습니다")
        st.markdown(info_block(("PC 이름", dev.get("label") or dev["device_id"])),
                    unsafe_allow_html=True)
        st.caption("그 PC 를 켜고 작업표시줄에 **블로그 랜딩 Agent** 가 있는지 "
                   "확인해 주세요.")
        return dev

    # 이미 등록된 PC 가 있으면, 그중 내 PC 를 고르게 한다(한 번 고르면 기억한다)
    known = [a for a in store.agents(max_age_sec=30)
             if a.get("state") != "stopped"]
    if known:
        st.info("어느 PC 에서 돌릴지 골라 주세요.")
        def _name(a):
            mark = "🟢" if a.get("alive") else "🔴"
            return f"{mark} {a.get('label') or a.get('host') or a.get('agent')}"
        pick = st.selectbox("내 PC", known, format_func=_name,
                            index=None, placeholder="PC 선택",
                            key="device_pick")
        if pick is not None and st.button("이 PC 로 연결", use_container_width=True,
                                          type="primary"):
            link_device(pick["agent"])
            st.rerun()
        st.caption("한 번 고르면 이 브라우저는 계속 그 PC 를 씁니다. "
                   "**내 PC 가 목록에 없으면** 아래에서 설치하고 연결해 주세요.")
        st.divider()

    st.error("🔴 Agent 연결 안 됨")
    st.link_button("⬇ Windows Agent 설치", AGENT_DOWNLOAD_URL,
                   use_container_width=True, type="primary")
    st.caption(f"[릴리스 페이지 열기]({AGENT_RELEASES_URL})")

    code = st.session_state.get("pair_code")
    if st.button("연결 코드 받기", use_container_width=True):
        try:
            got = store.create_pairing(minutes=10)
            st.session_state["pair_code"] = got["code"]
            code = got["code"]
        except Exception as exc:                   # noqa: BLE001
            st.error(f"코드를 만들지 못했습니다: {exc}")
    if code:
        st.markdown(f"<div class='pair-code'>{code}</div>", unsafe_allow_html=True)
        st.caption("설치 후 트레이 아이콘 → **연결** 에 이 번호를 입력하세요 (10분간 유효)")
        got = None
        try:
            got = store.pairing_result(code)
        except Exception:                          # noqa: BLE001
            got = None
        if got:
            # 알림은 다음 그리기에서 보여 준다(여기서 그리면 곧 지워질 요소가
            # 다시 그리기와 부딪혀 빨간 오류가 났다)
            st.session_state["_linked_name"] = (got.get("label")
                                                or got["device_id"])
            link_device(got["device_id"])
            st.session_state.pop("pair_code", None)
            st.rerun()
        else:
            st.caption("연결을 기다리는 중…")
            if st.button("연결 확인", use_container_width=True):
                st.rerun()
    return None


@st.cache_data(ttl=60, show_spinner=False)
def brand_names() -> dict:
    """내부 브랜드 키 → 화면 표시명 (repurely → 리퓨어리 …)."""
    try:
        return {b.id: b.title for b in brands.load_brands(include_disabled=True)}
    except Exception:                                          # noqa: BLE001
        return {}


def brand_name(key: str) -> str:
    """★화면에는 항상 한글 브랜드명으로 보여 준다.
    내부 Job/queue/CLI 는 키(`repurely` · `doctor_nuscent`)를 그대로 쓴다.
    설정에 없는 키가 들어오면(옛 기록 등) 원문을 그대로 둔다."""
    k = (key or "").strip()
    return brand_names().get(k, k)


def today_tag() -> str:
    now = datetime.now()
    return f"{now.month}{now.day:02d}"


def start_local_agent() -> str:
    """이 PC 에 에이전트를 하나 띄운다(창 없이)."""
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen([python_exe(), "-m", "v2.agent"], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=flags)
    return f"{python_exe()} -m v2.agent"


# ══════════════════════════════════════════════════════════════════
# 실행 준비 — 로그인(수동) + 에이전트 확보를 버튼 하나로 묶는다.
#   ★기존 기능을 지우지 않고 **내부 함수로 재사용**한다.
#     · 로그인 창      = `v2.session --login <계정>`  (큐 kind="session")
#     · 세션 확인      = `v2.session --check <계정>`  (headless, 화면 버튼은 없앴지만 유지)
#     · 에이전트 시작  = `python -m v2.agent`         (이미 살아 있으면 띄우지 않는다)
#   ★로그인 창은 **에이전트가 도는 PC** 에서 열린다. 그래서 에이전트를 먼저 확보한 뒤
#     로그인 작업을 큐에 넣는다(순서를 바꾸면 로그인 창이 뜨지 않는다).
# ══════════════════════════════════════════════════════════════════
def live_agents(store) -> list[dict]:
    return [a for a in store.agents(max_age_sec=30)
            if a.get("alive") and a.get("state") != "stopped"]


def ensure_agent(store) -> bool:
    """에이전트가 없을 때만 이 PC 에 하나 띄운다. 띄웠으면 True."""
    if live_agents(store):
        return False                      # ★이미 돌고 있으면 중복 실행하지 않는다
    start_local_agent()
    for _ in range(10):                   # 하트비트가 찍힐 때까지 잠깐 기다린다
        time.sleep(0.5)
        if live_agents(store):
            break
    return True


def submit_session_job(store, brand, account, action: str,
                       device_id: str = "", ref_tab: str = "",
                       extra_args: list | None = None):
    """세션 작업(`--login` / `--check`)을 **내 PC 의 Agent** 앞으로 넣는다.

    ★어느 기준랜딩 탭인지도 함께 보낸다. 계정(세션 폴더)이 그 PC 에 아직
      없으면 **그 PC 에서** 만들어 쓰기 위해서다(여기서 만들면 서버 안에만 생긴다).
    ★2026-11-03 — 제출도 3상태로 확인한다(`SubmitResult`). 로그인 작업은
      중복으로 들어가도 창만 두 번 뜨지만, '확인 불가' 를 실패로 보면 사용자가
      계속 다시 누르게 되므로 같은 규칙을 쓴다.
    """
    titles = {"--login": "로그인", "--check": "세션 확인",
              "--logout": "로그아웃", "--confirm": "계정 확인 기록"}
    tab = ref_tab or getattr(account, "ref_tab", "") or ""
    extra = [action, account.id, "--events"]
    if tab:
        extra += ["--ref-tab", tab, "--brand", brand.id]
    extra += [a for a in (extra_args or []) if a]
    who = account.label or account.id
    got_id = getattr(account, "login_id", "") or ""
    return submit_guarded(
        store,
        Job(brand=brand.id, brand_config=brands.config_json(),
            account=account.id, ref_tab=tab,
            account_name=who, login_id=got_id, extra=extra),
        kind="session",
        title=f"{titles.get(action, action)} — {who}"
              + (f" ({got_id})" if got_id else ""),
        target_agent=device_id,
        label=f"{titles.get(action, action)} 작업")


def resolve_account(brand, ref_tab: str):
    """기준랜딩 탭을 고르면 **그 자리에서** 계정이 정해진다.

    ★예전에는 로컬 accounts.json 을 읽어야만 계정을 알 수 있었다. 클라우드
      화면에는 그 파일이 없으니 계정이 빈칸이 되고, "로그인했는가" 판단이
      아예 안 돼서 [실전 실행] 이 영영 잠겼다. 이제 파일에 기대지 않는다.

    찾는 순서
      1) 로컬 계정 목록 (각자 PC 에서 화면을 띄웠을 때 — 예전 그대로)
      2) 브랜드 설정의 `accounts` (화면·PC 양쪽에 이미 전달되는 설정)
      3) 탭 이름에서 만든 값 (설정에 없어도 빈칸이 되지 않게)
    세션 폴더 id 는 탭 이름에서 만들므로 어디서 계산하든 같다.
    """
    tab = (ref_tab or "").strip()
    found = accounts.ensure_for_tab(tab, brand, create=False)
    if found is not None:
        return found
    info = brand.account_of(tab) if hasattr(brand, "account_of") else {}
    # ★계정 키(세션 폴더 이름)는 설정에 적힌 값을 먼저 쓴다. 없을 때만 탭에서
    #   만든 값을 쓰는데, 그 값은 **PC 가 기준랜딩 탭으로 다시 풀어** 실제 계정을
    #   찾으므로 어긋나지 않는다(옛 설정이 깔린 화면에서도 안전하다).
    return accounts.Account(
        id=info.get("session_id") or accounts.tab_slug(tab, brand),
        label=info.get("name") or brand.account_name_of(tab) or tab,
        login_id=info.get("login_id", ""), blog_id=info.get("blog_id", ""),
        ref_tab=tab, brand=brand.id)


def session_ready(store, rec: dict) -> dict:
    """PC 가 돌려준 '준비 완료' 응답. 없으면 빈 dict.

    ★화면은 로컬 파일이 아니라 **이 응답**으로 로그인 여부를 판단한다.
    """
    if not rec or rec.get("status") != "done":
        return {}
    try:
        events, _ = store.read_events(rec["id"])
    except Exception:                                          # noqa: BLE001
        return {}
    for ev in reversed(events or []):
        if ev.get("stage") == "session_ready" or ev.get("name") == "session_ready":
            return ev
    # 옛 Agent 는 이 응답을 보내지 않는다 — 작업이 성공했으면 준비된 것으로 본다
    return {"session_ready": True, "legacy": True}


def session_job(store, account, brand=None) -> dict | None:
    """이 계정의 가장 최근 세션(로그인) 작업 기록.

    ★화면과 PC 가 같은 계정을 다른 이름으로 부를 수 있다. 화면(클라우드)에는
      계정 목록이 없어 탭에서 만든 이름을 쓰고, PC 는 원래 쓰던 이름을 쓴다.
      그래서 **둘 다** 같은 것으로 본다. 아니면 진행 상황을 못 찾는다.
    """
    names = {account.id}
    tab = getattr(account, "ref_tab", "") or ""
    if tab:
        try:
            names.add(accounts.tab_slug(tab, brand))
        except Exception:                                      # noqa: BLE001
            pass
    for rec in store.list_jobs(limit=20):
        if rec.get("kind") != "session":
            continue
        if (rec.get("job") or {}).get("account") not in names:
            continue
        return rec
    return None


def session_job_state(store, account) -> str:
    """이 계정의 최근 세션 작업 상태 — running / pending / done / failed / ''."""
    rec = session_job(store, account, getattr(account, "brand", None))
    return (rec or {}).get("status") or ""


def render_progress(store, rec: dict, title: str = "진행 상황") -> None:
    """지금 무엇을 하고 있는지 짧게 보여 준다.

    ★사람이 PC 앞에서 기다리는 동안 "돌고는 있나?" 를 알 수 있어야 한다.
      자세한 내용은 아래 상세 화면에 그대로 있고, 여기서는 마지막 몇 줄만 본다.
    """
    if not rec:
        return
    status = rec.get("status") or ""
    started = (rec.get("created_at") or "")[11:19]
    passed = ""
    try:
        t0 = datetime.fromisoformat((rec.get("created_at") or "").replace("Z", ""))
        sec = int((datetime.now() - t0).total_seconds())
        passed = f"{sec // 60}분 {sec % 60}초" if sec >= 60 else f"{sec}초"
    except Exception:                                          # noqa: BLE001
        pass

    with st.container(border=True):
        head = st.columns([3, 1])
        head[0].markdown(f"**{title}** · {BADGE.get(status, status)}")
        head[1].caption(f"{started} 시작" + (f" · {passed} 지남" if passed else ""))

        total = int(rec.get("total") or 0)
        made = int(rec.get("made") or 0)
        published = list(rec.get("published") or [])
        if total or made or published:
            m = st.columns(3)
            m[0].metric("전체", total or "—")
            m[1].metric("작성", made)
            m[2].metric("발행", len(published))
            if total:
                st.progress(min(1.0, made / total), text=f"{made}/{total}건")

        try:
            text, _ = store.read_log(rec["id"])
        except Exception:                                      # noqa: BLE001
            text = ""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # 사람이 읽을 줄만 남긴다(내부 명령줄·빈 줄은 뺀다)
        lines = [ln for ln in lines if not ln.startswith("[agent] C:")]
        st.code(NEWLINE.join(lines[-6:]) or "시작하는 중입니다…", language="log")

        if status in RUNNING_STATES:
            st.caption("몇 초마다 저절로 갱신됩니다. 이 화면을 그대로 두셔도 됩니다.")
        elif status == "failed":
            why = (rec.get("error") or "").strip().splitlines()
            st.error("실패했습니다" + (f" — {why[0][:150]}" if why else ""))


# ══════════════════════════════════════════════════════════════════
# 배치 진행 — 10개씩 **순차**로. 배치가 바뀔 때마다 사람이 다시 로그인한다.
#   ★큐·에이전트·CLI 는 그대로다. 여기서는 "다음에 무엇을 큐에 넣을지"만 판단한다.
#     한 번에 한 배치만 큐에 올리므로 동시에 도는 일이 없다.
# ══════════════════════════════════════════════════════════════════
def _job_status(store, job_id: str) -> tuple[str, dict]:
    rec = store.get(job_id) if job_id else None
    return ((rec or {}).get("status") or ""), (rec or {})


def plan_results(store, plan: dict) -> dict:
    """계획의 모든 배치 로그(@@EVENT)를 모아 **건별 성공/실패**를 정리한다.

    같은 행이 여러 번 나오면(재실행) **마지막 결과**를 쓰고, 한 번이라도 성공했으면
    성공으로 본다 — 이미 성공한 건이 실패 목록에 남지 않게 하기 위해서다.
    """
    by_row: dict = {}
    order: list = []
    for b in plan.get("batches") or []:
        jid = b.get("run_job")
        if not jid:
            continue
        events, _ = store.read_events(jid)
        for e in events:
            name = e.get("event")
            if name not in ("published", "post_failed"):
                continue
            row = str(e.get("row") or e.get("no") or "")
            item = {
                "batch": b.get("no"),
                "행": e.get("row") or "",
                "순번": e.get("seq") or e.get("no") or "",
                "utm_campaign": e.get("campaign") or "",
                "블로그 URL": e.get("url") or e.get("blog_url") or "",
            }
            if name == "published":
                item.update({"ok": True, "단계": "발행", "사유": ""})
            else:
                item.update({"ok": False,
                             "단계": e.get("stage") or "post_build",
                             "사유": e.get("reason") or "",
                             "오류": (e.get("error") or "")[:200],
                             "구분": "공통" if e.get("scope") == "common" else "행"})
            if row not in by_row:
                order.append(row)
            prev = by_row.get(row)
            # 한 번이라도 성공했으면 성공을 남긴다
            by_row[row] = item if (prev is None or item["ok"] or not prev["ok"]) \
                else prev
    items = [by_row[r] for r in order]
    fails = [x for x in items if not x["ok"]]
    return {"items": items, "ok": len([x for x in items if x["ok"]]),
            "fail": len(fails), "fails": fails,
            "failed_rows": [str(x["행"]) for x in fails if x["행"]]}


def advance_plan(store, plan: dict) -> dict:
    """큐 상태를 보고 계획을 한 칸 진행시킨다(화면을 그릴 때마다 호출).

    판단은 `v2.batch_plan.advance()` 가 하고, 여기서는 큐를 읽고/넣는 일만 한다.
    """
    def submit_run(batch: dict) -> str:
        total = len(plan.get("batches") or [])
        res = submit_guarded(
            store, Job.from_dict(batch_plan.job_for(plan, batch)),
            title=f"{plan.get('title')} — 배치 {batch['no']}/{total}",
            target_agent=plan.get("device") or "",
            label=f"배치 {batch['no']}/{total}")
        if not res.ok:
            # ★확인 불가도 여기서 멈춘다 — 계획을 진행시키면 다음 그리기에서
            #   같은 배치를 또 넣게 된다(중복 발행). 사람이 확인한 뒤 잇는다.
            raise RuntimeError(res.message or "등록 결과를 알 수 없습니다")
        return res.job_id

    try:
        plan, changed = batch_plan.advance(
            plan, job_status=lambda jid: _job_status(store, jid),
            submit_run=submit_run)
    except Exception as exc:                                   # noqa: BLE001
        # ★등록에 실패했거나 확인되지 않았으면 계획 상태를 **바꾸지 않는다**
        #   (save 하지 않는다) — "넣은 줄 알았는데 안 들어간" 상태와, 반대로
        #   "안 넣은 줄 알고 또 넣는" 상태를 모두 막기 위해서다.
        st.error(f"다음 배치를 큐에 넣지 못했습니다.{chr(10)}{chr(10)}{exc}")
        return plan
    return batch_plan.save(plan) if changed else plan


def start_batch_login(store, plan: dict, brand, account,
                      device_id: str = "") -> None:
    """다음 배치를 위한 **수동 로그인**을 띄운다(사용자가 버튼을 눌렀을 때만)."""
    if batch_plan.current(plan) is None:
        return
    res = submit_session_job(store, brand, account, "--login", device_id)
    if not res.ok:
        # 안내는 submit_guarded 가 이미 화면에 그렸다. 계획 상태는 바꾸지 않는다.
        return
    batch_plan.mark_logging_in(plan, res.job_id)


def job_summary(rec: dict) -> dict:
    """최근 실행 기록 한 줄 — 시간·브랜드·계정·작업 종류·매체·결핍·건수·결과."""
    job = rec.get("job") or {}
    flow = job.get("flow") or ""
    kind_label = FLOWS.get(flow, {}).get("label", flow)
    if rec.get("kind") == "session":
        kind_label = "세션/로그인"
    elif flow == "production":
        kind_label = f"실전용 · {job.get('mode', '')}"
    elif job.get("dry_run"):
        kind_label = f"{kind_label} (dry-run)"
    err = (rec.get("error") or "").strip().splitlines()
    return {
        "시간": (rec.get("created_at") or "")[5:16].replace("T", " "),
        "브랜드": brand_name(rec.get("brand") or job.get("brand") or ""),
        "계정": ((job.get("account_name") or job.get("account") or "")
                + (f" ({job['login_id']})" if job.get("login_id") else "")),
        "작업": kind_label,
        "매체": job.get("media") or "",
        "결핍": job.get("deficiency") or "",
        # 표는 열마다 형이 같아야 한다(숫자·빈칸이 섞이면 경고가 뜬다)
        "건수": str(job.get("count")) if job.get("count") is not None else "",
        "결과": BADGE.get(rec.get("status"), rec.get("status") or ""),
        "발행": len(rec.get("published") or []),
        "실패 사유": (err[0][:80] if err else ""),
    }


def render_steps(events: list[dict]) -> None:
    """@@EVENT 를 사람이 읽는 진행 단계로."""
    if not events:
        st.caption("아직 단계 정보가 없습니다.")
        return
    lines: list[str] = []
    for e in events:
        name = e.get("event")
        if name == "run_started":
            lines.append(f"▶ 시작 — 브랜드 "
                         f"{e.get('brand_label') or brand_name(e.get('brand', ''))}"
                         f" · {e.get('media', '')} / {e.get('deficiency', '')}")
        elif name == "stage":
            label = STAGE_LABELS.get(e.get("stage"), e.get("stage"))
            ok = e.get("status") == "ok"
            tail = ""
            if e.get("row"):
                tail += f" (행 {e['row']})"
            if e.get("matched"):
                tail += f" — {e['matched']}건"
            if not ok:
                tail += f" — {e.get('reason', '')}"
            lines.append(f"{'✅' if ok else '❌'} {label}{tail}")
        elif name == "plan":
            lines.append(f"📋 계획 — {e.get('total', 0)}건")
        elif name == "post_ready":
            lines.append(f"📝 작성 완료 [{e.get('no')}/{e.get('total')}]")
        elif name == "published":
            lines.append(f"🚀 발행 [{e.get('no')}/{e.get('total')}] {e.get('url', '')}")
        elif name == "post_failed":
            lines.append(f"❌ 실패 [{e.get('no')}] {e.get('reason') or e.get('error', '')}")
        elif name == "run_finished":
            if e.get("ok"):
                lines.append(f"🏁 종료 — 발행 {len(e.get('published') or [])}건"
                             + (f" · 건너뛴 행 {e.get('failed_rows')}"
                                if e.get("failed_rows") else ""))
            else:
                lines.append(f"🛑 종료(실패) — {str(e.get('error', ''))[:120]}")
    st.markdown("  \n".join(lines[-60:]))


if not require_password():          # ★팀 공용 비밀번호(Secrets)
    st.stop()

sync_address()                      # ★화면을 그리기 전에 주소를 한 번만 맞춘다

def cloud_not_wired(store_obj=None) -> list[str]:
    """서버에서 도는 화면인데 원격 큐에 안 붙어 있으면, 빠진 설정 이름을 돌려준다.

    ★이 상태를 알아채기가 아주 어려웠다 — [연결 코드 받기] 는 멀쩡히 6자리를
      만들어 주지만, 그 코드는 **서버가 아니라 이 화면 안에만** 있어서 PC 에서
      아무리 입력해도 붙지 않는다. 그래서 화면에 대놓고 적어 둔다.
    """
    if sys.platform.startswith("win"):
        return []                          # 각자 PC 에서 띄운 화면이면 정상이다
    if type(store_obj if store_obj is not None else store).__name__ != "LocalStore":
        return []                          # 이미 원격 큐에 붙어 있다
    need = ("BLOG_QUEUE_BACKEND", "SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    missing = []
    for key in need:
        got = os.getenv(key)
        if not got:
            try:
                got = st.secrets.get(key)
            except Exception:                                  # noqa: BLE001
                got = None
        if not got:
            missing.append(key)
    return missing or list(need)


# ══════════════════════════════════════════════════════════════════
# 연결(큐) — 관리자 재부팅 없이 사용자가 다시 맺을 수 있게 (2026-11-03, 1단계)
# ══════════════════════════════════════════════════════════════════
#   ★[연결 다시 맺기] 가 하는 일 / 하지 않는 일을 여기서 못박는다.
#       한다    : 큐 연결 객체 · 기준랜딩 탭 목록 · 결핍/제품 목록 · 시트 미리보기
#                 · 브랜드 이름 · 계정 목록 메모리 캐시 — **읽기 캐시만** 버린다.
#       안 한다 : 작업 제출 · 작업 취소 · 큐 파일/행 삭제 · 세션(쿠키) 변경.
#                 추적 중인 작업 번호(job_id · plan_id)도 **그대로 둔다** —
#                 지우면 같은 작업을 또 넣을 위험이 생긴다.
RECONNECT_SCOPE = ("큐 연결", "기준랜딩 탭 목록", "결핍·제품 목록",
                   "시트 미리보기", "브랜드 이름", "계정 목록 캐시")


def reconnect() -> list[str]:
    """화면이 들고 있던 읽기 캐시를 버린다(큐 데이터는 건드리지 않는다)."""
    done: list[str] = []
    for label, fn in (("큐 연결", get_store.clear),
                      ("기준랜딩 탭 목록", load_tabs.clear),
                      ("결핍·제품 목록", load_catalog.clear),
                      ("시트 미리보기", preview_rows.clear),
                      ("브랜드 이름", brand_names.clear)):
        try:
            fn()
            done.append(label)
        except Exception:                                      # noqa: BLE001
            pass
    try:
        accounts.refresh()                 # 계정 목록 메모리 캐시만 버린다
        done.append("계정 목록 캐시")
    except Exception:                                          # noqa: BLE001
        pass
    return done


def backend_label(store_obj=None) -> tuple[str, str]:
    """(표시 문구, 심각도) — 심각도는 'ok' | 'warn' | 'error'."""
    info = queue_store.backend_info()
    kind = type(store_obj).__name__ if store_obj is not None else (info.get("kind") or "")
    if kind == "SupabaseStore":
        mode = getattr(store_obj, "mode", "") or info.get("mode") or ""
        return f"Supabase 원격 큐{f' ({mode})' if mode else ''}", "ok"
    if info.get("fallback"):
        return "로컬 큐 — ⚠ 원격 큐 연결 실패로 내려온 상태", "error"
    if queue_store.wants_remote():
        return "로컬 큐 — ⚠ 설정은 원격인데 로컬로 돌고 있습니다", "error"
    return "로컬 큐 (이 컴퓨터 안)", "ok" if sys.platform.startswith("win") else "warn"


def render_connection_panel(store_obj, device: dict | None) -> None:
    """지금 무엇에 붙어 있는지 · 마지막 통신 시각 · [연결 다시 맺기]."""
    text, level = backend_label(store_obj)
    st.header("연결 상태")
    (st.success if level == "ok" else st.warning if level == "warn"
     else st.error)(f"큐: {text}")
    last = ""
    try:
        last = store_obj.last_contact()
    except Exception:                                          # noqa: BLE001
        last = ""
    st.markdown(info_block(
        ("큐 마지막 통신", (f"{timeutil.fmt(last)} ({timeutil.ago(last)})"
                         if last else "아직 없음")),
        ("내 PC Agent", (f"🟢 {device.get('label') or device.get('device_id')}"
                        f"{chr(32)}· 신호 {timeutil.ago(device.get('last_seen'))}"
                        if device and device.get("alive") else "🔴 연결 안 됨")),
    ), unsafe_allow_html=True)
    if st.button("연결 다시 맺기", use_container_width=True,
                 help="관리자 재부팅 없이 이 화면이 들고 있던 읽기 캐시를 버리고 "
                      "큐에 다시 붙습니다. 작업을 새로 넣거나 취소하지 않습니다."):
        got = reconnect()
        st.toast("연결을 다시 맺었습니다 — " + ", ".join(got))
        st.rerun()
    st.caption("다시 맺는 범위: " + " · ".join(RECONNECT_SCOPE)
               + f"{chr(10)}작업 제출·취소·삭제는 하지 않으며, 진행 중인 작업 번호도 "
                 "그대로 둡니다.")


def connect_store():
    """큐에 붙는다. 못 붙으면 **조용히 로컬로 가지 않고** 화면을 멈춘다."""
    try:
        got = get_store()
    except Exception as exc:                                   # noqa: BLE001
        st.error("🔌 **작업 큐(Supabase)에 연결하지 못했습니다.**"
                 + chr(10) + chr(10) +
                 "이 상태에서는 작업을 넣어도 **각자 PC 의 Agent 가 보지 못합니다.** "
                 "그래서 실행 버튼을 열지 않고 여기서 멈춥니다."
                 + chr(10) + chr(10) + f"원인: `{type(exc).__name__}: {exc}`")
        st.info("아래 **[연결 다시 맺기]** 를 먼저 눌러 보세요. 그래도 같으면 "
                "**Settings → Secrets** 의 `SUPABASE_URL` · `SUPABASE_SERVICE_KEY` · "
                "`BLOG_QUEUE_BACKEND` 를 확인해 주세요. "
                "(관리자 재부팅은 필요하지 않습니다)")
        if st.button("연결 다시 맺기", type="primary"):
            reconnect()
            st.rerun()
        st.caption("※ 이 화면은 작업을 넣기만 합니다. 연결이 끊긴 동안 **이미 넣은 "
                   "작업은 그대로** 있고, 취소되지 않습니다.")
        st.stop()
    # ★서버(클라우드)에서 로컬 큐는 **쓸 수 없다.** 설정이 빠져 백엔드가 local 로
    #   잡힌 경우가 여기 걸린다(이때는 예외가 아니라 조용히 LocalStore 가 된다).
    #   그 상태로 두면 작업이 서버 컨테이너 안으로 들어가 PC 가 못 본다.
    missing = cloud_not_wired(got)
    if missing:
        st.error("🔌 **이 화면이 각자 PC 와 이어져 있지 않습니다 — 작업 큐 설정이 "
                 "빠졌습니다.**"
                 + chr(10) + chr(10) +
                 "이 상태에서는 작업이 **이 서버 안에만** 쌓여 PC 의 Agent 가 보지 "
                 "못합니다. 그래서 실행 버튼을 열지 않고 멈춥니다."
                 + chr(10) + chr(10) +
                 "**Settings → Secrets** 에 다음을 넣고 저장해 주세요 → "
                 + ", ".join(f"`{k}`" for k in missing))
        if st.button("연결 다시 맺기", type="primary"):
            reconnect()
            st.rerun()
        st.caption("※ 이미 넣은 작업은 그대로 있고, 취소되지 않습니다.")
        st.stop()
    return got


# ══════════════════════════════════════════════════════════════════
# 선택 계정 로그아웃 (Stage 2 · 2026-11-03)
#   ★기존 발행 흐름은 건드리지 않는다. 전부 **보조 기능**이다.
#   ★상태는 PC 의 파일이 아니라 **큐**에서 읽는다 — 클라우드 화면에는 sessions/ 가 없고,
#     큐는 화면·PC 가 함께 보는 유일한 공용 저장소다.
#   ★판단 기준은 **login_id** 뿐이다. blog_id 로 묶지 않는다.
# ══════════════════════════════════════════════════════════════════
def is_logout_job(rec: dict) -> bool:
    job = (rec or {}).get("job") or {}
    return "--logout" in list(job.get("extra") or [])


def logout_state(store_obj, login_id: str, account_id: str = "") -> dict:
    """그 계정의 로그아웃 진행 상태. 큐를 한 번만 읽는다.

        {"login_id", "account_id", "held"(보류 작업),
         "running"(끝나길 기다리는 배치),
         "logout_job"(대기/실행 중인 로그아웃 작업), "pending"(대기 중인가)}

    ★식별자는 **login_id 1순위, 없으면 account.id(세션 폴더) 정확일치**다.
      `blog_id` 로는 고르지 않는다 — 같은 blog_id 에 로그인 계정이 둘 이상일 수 있다.
    """
    lid, aid = (login_id or "").strip(), (account_id or "").strip()
    out = {"login_id": lid, "account_id": aid, "held": [], "running": [],
           "logout_job": None, "pending": False}
    if not lid and not aid:
        return out
    try:
        rows = store_obj.list_jobs(limit=120)
    except Exception:                                          # noqa: BLE001
        return out                          # 큐를 못 읽으면 아무것도 단정하지 않는다
    for rec in rows:
        if not queue_store.matches(rec, lid, aid):
            continue
        status = rec.get("status") or ""
        if status == queue_store.HELD:
            out["held"].append(rec)
        elif status == queue_store.RUNNING and (rec.get("kind") or "run") == "run":
            out["running"].append(rec)
        elif is_logout_job(rec) and status in RUNNING_STATES:
            out["logout_job"] = out["logout_job"] or rec
    out["pending"] = bool(out["held"] or out["logout_job"])
    return out


def logout_block(store_obj, login_id: str, account_id: str = "") -> str:
    """로그아웃 대기 중이면 **새 작업을 만들지 못하게** 할 이유를 돌려준다."""
    stt = logout_state(store_obj, login_id, account_id)
    if not stt["pending"]:
        return ""
    bits = []
    if stt["held"]:
        bits.append(f"보류된 작업 {len(stt['held'])}건")
    if stt["running"]:
        bits.append(f"끝나기를 기다리는 배치 {len(stt['running'])}건")
    if stt["logout_job"]:
        bits.append("로그아웃 작업 대기")
    who = login_id or f"계정 {account_id}"
    return (f"**{who} 는 로그아웃 대기 상태입니다** — 새 작업을 만들지 "
            f"않습니다({' · '.join(bits)})."
            f"{chr(10)}{chr(10)}재로그인(**[실행 준비]**) 뒤 **[보류 작업 재개]** 를 "
            f"누르면 이어서 진행됩니다. 다른 계정의 작업은 영향받지 않습니다.")


def agent_can_logout(device: dict | None) -> bool:
    """이 PC 의 Agent 가 선택 계정 로그아웃을 할 수 있는 버전인가.

    ★`v2.session --logout` 은 1.3.0 에서 생긴 명령이다. 옛 설치본에 보내면
      argparse 가 거부해 **작업이 실패**한다 → 그 PC 에서는 버튼을 잠근다.
    """
    return pairing.supports((device or {}).get("version") or "", "logout")


def grade_line(ready_info: dict) -> str:
    """PC 가 돌려준 검증 등급 한 줄. 등급이 없으면(옛 Agent) 빈 문자열."""
    g = (ready_info or {}).get("grade") or ""
    if not g:
        return ""
    label = (ready_info or {}).get("grade_label") or GRADE_LABEL.get(g, g)
    tail = (" · 발행 대상 블로그 일치 확인됨"
            if (ready_info or {}).get("blog_verified") else "")
    extra = "" if g == "authenticated" else "  ※ 네이버 인증 ID 확인은 아닙니다"
    return f"· 로그인 계정 확인: **{label}**{tail}{extra}"


def render_account_tools(store_obj, brand, account, ready_info: dict,
                         device: dict | None = None) -> None:
    """계정 세션 관리(보조) — 상태확인 / 로그아웃 / 보류 재개.

    ★기존 발행 흐름(기준랜딩 탭 선택 → 실행 준비 → Dry-run → 실전 실행)은
      그대로다. 이 영역은 **필요할 때만** 펼쳐 쓰는 보조 기능이다.
    """
    device_id = (device or {}).get("device_id") or ""
    can_logout = agent_can_logout(device)
    lid = (getattr(account, "login_id", "") or "").strip()
    #   ★login_id 가 없는 기존 계정(tab_… 등)도 로그아웃할 수 있어야 한다.
    #     그 계정의 유일한 정확한 식별자는 **세션 폴더 이름(account.id)** 이다.
    aid = "" if lid else (getattr(account, "id", "") or "").strip()
    stt = logout_state(store_obj, lid, aid)
    title = "계정 세션 관리 (보조)"
    if stt["pending"]:
        title += " — 로그아웃 대기 중"
    with st.expander(title, expanded=bool(stt["pending"])):
        st.caption(f"· 대상 계정: **{account.label or account.id}**"
                   + (f" (login_id: {lid})" if lid
                      else " (login_id 미등록 — 이 계정 전용 세션 기준)")
                   + f"{chr(10)}· 여기서 하는 일은 **이 계정에만** 적용됩니다. "
                     "다른 로그인 계정의 세션·쿠키는 건드리지 않습니다.")
        line = grade_line(ready_info)
        if line:
            st.markdown(line)

        if stt["pending"]:
            bits = []
            if stt["running"]:
                bits.append(f"**실행 중인 배치 {len(stt['running'])}건은 끝까지 "
                            f"발행합니다**(중간에 끊지 않습니다)")
            if stt["held"]:
                bits.append(f"대기 작업 {len(stt['held'])}건을 **보류**했습니다")
            if stt["logout_job"]:
                bits.append("배치가 끝나면 로그아웃이 실행됩니다")
            st.warning("로그아웃 대기 — " + " · ".join(bits)
                       + f"{chr(10)}{chr(10)}재로그인은 위 **[실행 준비]** 로 하고, 그 뒤 아래 "
                         "**[보류 작업 재개]** 를 눌러 주세요.")

        c1, c2 = st.columns(2)
        if c1.button("로그인 상태 다시 확인", use_container_width=True,
                     disabled=not device_id,
                     help="창을 띄우지 않고 저장된 세션이 살아 있는지, 발행 대상 "
                          "블로그가 맞는지 확인합니다"):
            res = submit_session_job(store_obj, brand, account, "--check",
                                     device_id,
                                     ref_tab=getattr(account, "ref_tab", ""))
            if res.ok:
                st.session_state["job_id"] = res.job_id
                st.toast("로그인 상태를 확인하고 있습니다.")
                st.rerun()

        if not stt["pending"]:
            #   세션을 공유하는 계정이 있어 **불분명한 경우**에만 체크가 필요하다.
            #   판정은 계정 목록이 있는 PC(Agent)가 하고, 여기서는 그 결과를 보여 준다.
            shared_ok = st.checkbox(
                "공유 세션 확인 — 같은 세션을 쓰는 다른 계정도 재로그인이 필요함을 "
                "확인했습니다", key=f"_shared_{aid or lid}",
                help="PC 에서 '이 세션을 함께 쓰는 계정' 이 발견되면 로그아웃이 "
                     "중단됩니다. 영향 범위를 확인하셨다면 이걸 체크하고 다시 "
                     "눌러 주세요")
            if c2.button("선택 계정 로그아웃", use_container_width=True,
                         disabled=not (device_id and (lid or aid) and can_logout),
                         help="이 계정만 로그아웃합니다. 실행 중인 10건 배치는 "
                              "끝까지 발행한 뒤 로그아웃합니다"):
                do_logout(store_obj, brand, account, lid, device_id,
                          account_id=aid, confirm_shared=bool(shared_ok))
            if not (lid or aid):
                st.caption("※ 계정을 특정할 수 없습니다(세션 폴더 이름도 비어 있음).")
            elif not lid:
                st.caption("※ login_id 가 등록되지 않은 계정이라 **이 계정 전용 "
                           "세션**만 초기화합니다. blog_id 는 기준으로 쓰지 "
                           "않습니다 — 같은 blog_id 에 다른 로그인 계정이 있을 수 "
                           "있기 때문입니다.")
            if device_id and not can_logout:
                st.caption("※ 이 PC 의 Agent 가 "
                           f"{(device or {}).get('version') or '옛 버전'} 입니다 — "
                           "로그아웃 기능은 **"
                           + ".".join(str(n) for n in pairing.NEEDS["logout"])
                           + " 이상**에서 동작합니다. "
                           "왼쪽에서 설치본을 다시 받아 설치하면 열립니다"
                           "(설치해도 저장된 로그인 세션은 그대로입니다).")
        elif c2.button("보류 작업 재개", type="primary", use_container_width=True,
                       disabled=not (lid or aid),
                       help="보류해 둔 작업을 다시 대기 상태로 돌립니다"):
            do_resume(store_obj, lid, device_id, account_id=aid)


def do_logout(store_obj, brand, account, login_id: str, device_id: str,
              account_id: str = "", confirm_shared: bool = False) -> None:
    """① 대기 작업 보류 → ② 로그아웃 작업 등록. 순서를 바꾸면 안 된다.

    ★②를 먼저 하면 그 사이에 Agent 가 다음 배치를 집어 가 로그아웃 뒤에 발행이
      이어질 수 있다. ①이 먼저여야 "보류된 것은 절대 집히지 않는다" 가 성립한다.
    ★①은 **status 가 아직 pending 인 것만** 바꾸는 조건부 변경이다. 그 순간
      Agent 가 집어간 작업(running)은 조건에 걸리지 않고 끝까지 간다.
    """
    try:
        held = store_obj.hold_account(login_id, device=device_id or "",
                                      account_id=account_id)
    except NotImplementedError:
        st.error("이 큐에서는 보류를 지원하지 않습니다 — 로그아웃을 하지 않았습니다.")
        return
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"대기 작업을 보류하지 못해 **로그아웃을 하지 않았습니다**."
                 f"{chr(10)}{chr(10)}원인: `{type(exc).__name__}: {exc}`")
        return

    running = []
    try:
        running = store_obj.running_for_login(login_id, account_id)
    except Exception:                                          # noqa: BLE001
        pass

    res = submit_session_job(store_obj, brand, account, "--logout", device_id,
                             ref_tab=getattr(account, "ref_tab", ""),
                             extra_args=["--confirm-shared"] if confirm_shared
                             else None)
    if not res.ok:
        # ★로그아웃 작업이 안 들어갔다 → 보류를 **되돌린다**(작업이 멈춘 채
        #   방치되지 않게). 안내는 submit_guarded 가 이미 그렸다.
        try:
            store_obj.resume_account(login_id, device=device_id or "",
                                     account_id=account_id)
            st.info("로그아웃 작업을 등록하지 못해 보류를 되돌렸습니다 "
                    "(작업은 그대로 대기 상태입니다).")
        except Exception:                                      # noqa: BLE001
            st.warning("로그아웃 작업 등록에 실패했고 보류도 되돌리지 못했습니다 — "
                       "[보류 작업 재개] 를 눌러 주세요.")
        return
    st.session_state["job_id"] = res.job_id
    msg = [f"보류 {len(held)}건"]
    if running:
        msg.append(f"실행 중인 배치 {len(running)}건은 **끝까지 발행**합니다")
    msg.append("그 뒤 로그아웃이 실행됩니다")
    st.success("로그아웃을 예약했습니다 — " + " · ".join(msg))
    st.rerun()


def do_resume(store_obj, login_id: str, device_id: str,
              account_id: str = "") -> None:
    try:
        back = store_obj.resume_account(login_id, device=device_id or "",
                                        account_id=account_id)
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"보류 작업을 재개하지 못했습니다 — `{type(exc).__name__}: {exc}`")
        return
    st.success(f"보류 작업 {len(back)}건을 다시 대기 상태로 돌렸습니다.")
    st.rerun()


def cancel_guarded(store_obj, job_id: str, label: str = "작업") -> bool:
    """중단 요청. 실패를 성공처럼 보이지 않게 **결과를 화면에 알린다**."""
    try:
        store_obj.request_cancel(job_id)
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"{label} 중단 요청을 보내지 못했습니다 — **작업은 계속 돌고 "
                 f"있을 수 있습니다.**{chr(10)}{chr(10)}"
                 f"원인: `{type(exc).__name__}: {exc}`")
        return False
    return True


def submit_guarded(store_obj, job, *, kind: str = "run", title: str = "",
                   target_agent: str = "", label: str = "작업"):
    """`queue_store.submit_checked` + 결과를 **화면에 그려 준다** → `SubmitResult`.

    판단은 큐 계층이 하고(테스트가 브라우저 없이 그걸 검증한다), 여기서는
    상태별로 무엇을 누르면 되는지만 덧붙인다.
      · 성공 확인   → 조용히 통과(호출한 쪽이 작업 번호를 추적한다)
      · 실패 확정   → 빨간 안내. 다시 눌러도 **중복 발행 위험 없음**
      · 확인 불가   → 노란 안내 + **자동 재제출 금지**. 멱등성 키를 남겨
                      [등록 여부 다시 확인] 으로 되찾게 한다.
    """
    # ★로그아웃 대기 중인 계정의 **랜딩 작업(run)** 은 새로 만들지 않는다.
    #   로그인·세션확인·로그아웃(session)은 복구 수단이므로 막지 않는다.
    #   아직 아무것도 보내지 않았으므로 '실패 확정' 이 맞다(중복 위험 없음).
    if kind == "run":
        why = logout_block(store_obj, getattr(job, "login_id", "") or "",
                           getattr(job, "account", "") or "")
        if why:
            st.error(why)
            return queue_store.SubmitResult(queue_store.SUBMIT_FAILED, message=why)

    res = queue_store.submit_checked(
        store_obj, job, kind=kind, title=title, target_agent=target_agent,
        label=label)
    body = (res.message or "").replace(chr(10), chr(10) + chr(10))
    if res.status == queue_store.SUBMIT_FAILED:
        st.error(body + f"{chr(10)}{chr(10)}왼쪽 **연결 상태** 에서 "
                 "[연결 다시 맺기] 를 누른 뒤 다시 시도해 주세요.")
    elif res.unknown:
        st.session_state["_pending_submit"] = {
            "key": res.client_key, "label": label,
            "at": timeutil.now_kst().isoformat(timespec="seconds")}
        sync_pending_key()                 # 새로고침해도 키를 잃지 않게 주소에도
        st.warning("⚠ " + body + f"{chr(10)}{chr(10)}**같은 작업을 다시 넣지 "
                   "마세요.** 아래 **[등록 여부 다시 확인]** 으로 먼저 확인해 "
                   "주세요(중복 발행을 막기 위해서입니다).")
    elif res.message:                      # 성공이지만 알려 줄 것이 있는 경우
        st.info(body)
    return res


def render_pending_submit(store_obj) -> None:
    """'등록 여부 확인 불가' 가 남아 있으면 **조회로만** 되찾게 한다(재제출 안 함)."""
    pend = st.session_state.get("_pending_submit") or {}
    key = pend.get("key") or ""
    if not key:
        return
    when = timeutil.fmt(pend.get("at"), "%H:%M:%S")
    st.warning(f"⚠ **{pend.get('label') or '작업'}의 등록 여부가 확인되지 "
               f"않았습니다**" + (f" ({when} 제출)" if when else "")
               + ". 아래 버튼으로 확인하기 전에는 다시 넣지 마세요."
               + (f"{chr(10)}{chr(10)}※ 새로고침 전에 제출한 작업입니다"
                  "(주소에 남겨 둔 확인용 번호로 찾습니다)."
                  if pend.get("from_url") else ""))
    if st.button("등록 여부 다시 확인", use_container_width=True,
                 help="작업을 새로 넣지 않고, 멱등성 키로 이미 등록된 작업만 찾습니다"):
        rec, why = queue_store.find_job_by_client_key(store_obj, key, tries=2)
        if rec:
            st.session_state["job_id"] = rec.get("id") or ""
            clear_pending_key()
            st.toast("등록돼 있었습니다 — 그 작업을 이어서 보여 드립니다.")
            st.rerun()
        elif why:
            st.error("아직 확인하지 못했습니다 — 다시 넣지 마세요."
                     f"{chr(10)}{chr(10)}원인: `{why}`")
        else:
            clear_pending_key()
            st.info("등록되지 않은 것으로 확인했습니다 — 이제 다시 실행하셔도 "
                    "**중복 발행 위험이 없습니다.**"
                    f"{chr(10)}{chr(10)}※ 그래도 아래 **최근 실행 기록**에 같은 "
                    "작업이 보이면 그것을 이어서 쓰세요(늦게 처리된 경우).")


store = connect_store()

# ══════════════════════════════════════════════════════════════════
# 사이드바 — 브랜드만 (계정/실행 준비는 본문에서 고른다)
# ══════════════════════════════════════════════════════════════════
with st.sidebar:
    # ══ 최상단: 브랜드 ══ 고르면 기준시트 + UTM 빌더가 **한 세트로** 따라온다.
    st.header("브랜드")
    try:
        # ★strict — 설정을 못 읽으면 **다른 브랜드로 대체하지 않고** 여기서 멈춘다.
        brand_rows = brands.load_brands(strict=True)
    except Exception as exc:                                   # noqa: BLE001
        st.error(f"브랜드 설정을 읽지 못했습니다.{chr(10)}{chr(10)}{exc}")
        st.stop()
    if not brand_rows:
        st.error("`brands.json` 에 브랜드가 없습니다.")
        st.stop()

    brand_labels = {b.id: (b.title if b.ready else f"{b.title} (준비 중)")
                    for b in brand_rows}
    brand_id = st.selectbox("브랜드", list(brand_labels), key="brand_id",
                            format_func=lambda i: brand_labels[i])
    brand = next(b for b in brand_rows if b.id == brand_id)
    if brand.ready:
        st.success(f"● 실행 가능 — {brand.title}")
    else:
        st.warning(f"○ 준비 중 — {brand.title}")
    st.markdown(info_block(("기준시트", brand.reference_title),
                           ("UTM 빌더", brand.utm_title)),
                unsafe_allow_html=True)

    st.divider()
    device = render_agent_panel(store)      # 🟢/🔴 · 설치 · 6자리 페어링
    agent_ready = bool(device and device.get("alive"))

    st.divider()
    render_connection_panel(store, device)  # 큐 백엔드 · 마지막 통신 · 다시 맺기

    st.caption("네이버 로그인 세션은 실행하는 PC 안에만 저장됩니다"
               "(큐로 나가지 않습니다).")
    st.caption(f"화면 버전 {APP_VERSION}")

# ══════════════════════════════════════════════════════════════════
# 계정(기준 계정) — 화면에서는 본문의 `기준 계정` 으로 고른다.
#   ★내부 구조는 그대로다: accounts.json · sessions/<id>/ · --account.
#     계정마다 기준랜딩 탭(ref_tab)이 다르므로, 그 탭 이름을 그대로 선택지로 쓴다.
# ══════════════════════════════════════════════════════════════════
alive = live_agents(store)          # 실행 에이전트 현황(아래 여러 곳에서 쓴다)

# ══════════════════════════════════════════════════════════════════
# 본문
# ══════════════════════════════════════════════════════════════════
st.title("네이버 블로그 랜딩 생성기")
st.markdown(info_block(("브랜드", brand.title),
                       ("기준시트", brand.reference_title),
                       ("UTM 빌더", brand.utm_title)),
            unsafe_allow_html=True)

_gap = cloud_not_wired()
if _gap:
    st.error("🔌 **이 화면이 아직 각자 PC 와 이어져 있지 않습니다.**"
             + chr(10) + chr(10) +
             "지금은 [연결 코드 받기] 를 눌러도 그 코드가 이 화면 안에만 "
             "만들어져서, PC 에서 입력해도 연결되지 않습니다."
             + chr(10) + chr(10) +
             "**Settings → Secrets** 에 다음 항목을 넣고 저장해 주세요 → "
             + ", ".join(f"`{k}`" for k in _gap))
elif not agent_ready:
    st.warning("⚠ **아직 어느 PC 에서 돌릴지 정해지지 않았습니다.**"
               f"{chr(10)}{chr(10)}왼쪽 **내 PC** 에서 본인 컴퓨터를 고르세요. "
               "목록에 없으면 그 컴퓨터에 Agent 를 **한 번만** 설치하면 됩니다 — "
               "**[⬇ Windows Agent 설치]** → 설치 → **[연결 코드 받기]** 의 6자리 입력."
               f"{chr(10)}{chr(10)}※ 다른 사람 PC 를 고르면 **그 사람 컴퓨터에서 "
               "그 사람 네이버 계정으로** 글이 올라갑니다. 꼭 본인 PC 를 고르세요.")

left, right = st.columns([3, 2], gap="large")

# ── 왼쪽: 작업 설정 ────────────────────────────────────────────────
with left:
    if not brand.ready:
        # ★준비 중인 브랜드 — 화면에는 보이되 **실행은 잠근다**.
        st.subheader(f"{brand.title} — 준비 중")
        st.warning(f"**{brand.title}** 은(는) 아직 실행할 수 없습니다."
                   + (f"{chr(10)}{chr(10)}사유: {brand.status_note}"
                      if brand.status_note else ""))
        st.markdown(
            "- 기준시트(매체 · 결핍 · 검수용/실전용 블로그랜딩 · 제품 링크) 작성\n"
            "- UTM 빌더에 서비스 계정 편집 권한 부여\n"
            f"- 끝나면 `brands.json` 의 `{brand.id}` 항목에서 "
            "`\"ready\": true` 로만 바꾸면 됩니다(코드 수정 없음)")
        st.caption(f"기준시트 ID: `{brand.reference_sheet_id}`")
        st.caption(f"UTM 빌더 ID: `{brand.utm_sheet_id}`")
        b1, b2 = st.columns(2)
        b1.button("Dry-run (브라우저 안 켬)", use_container_width=True, disabled=True,
                  key="dry_locked")
        b2.button("실전 실행", type="primary", use_container_width=True, disabled=True,
                  key="run_locked")
        st.caption("⛔ 준비 중인 브랜드라 실행 버튼이 잠겨 있습니다. "
                   "왼쪽 사이드바에서 리퓨어리를 선택하세요.")
    else:
        st.subheader("무엇을 만들까요")
        # ── 작업 종류 × 참고 랜딩 종류 → 내부 flow/mode 매핑 (2026-09-17) ──
        #   ★화면은 두 축만 묻는다: '무엇을 할지(새 글/기존 글)' 와
        #     '어느 참고글을 쓸지(검수용/실전용)'. 내부 flow(review/production)와
        #     mode(create/convert)는 아래 표로 **자동 결정**한다.
        #     예전에는 `실전용 방식` 을 따로 골라야 했는데, '기존 글 수정' 안에
        #     '새 글로 만들기' 가 숨어 있어 찾을 수 없었다(사용자 지적).
        #   ⚠️`v2.run`/`v2.run_production` 내부와 review/production 키는 건드리지 않는다 —
        #     바뀐 것은 화면 선택값을 그 키로 옮기는 **매핑 규칙**뿐이다.
        TASKS = {"new": "새 글 생성", "edit": "기존 글 수정"}
        ROUTES = {
            #  (작업 종류, 참고 랜딩) → (내부 flow, production mode)
            ("new", "검수용"): ("review", "convert"),       # v2.run
            ("new", "실전용"): ("production", "create"),    # v2.run_production
            ("edit", "실전용"): ("production", "convert"),  # v2.run_production
            #  ("edit", "검수용") 는 지원하지 않는다 — 아래에서 막는다.
        }
        task = st.radio("작업 종류", list(TASKS), format_func=lambda k: TASKS[k],
                        horizontal=True, key="task_kind")
        kind = st.radio("참고 랜딩 종류 (기준시트 컬럼)", KINDS, horizontal=True,
                        index=1 if task == "edit" else 0, key=f"kind_{task}",
                        help="검수용 블로그랜딩 / 실전용 블로그랜딩 중 어느 참고글을 "
                             "읽을지 고릅니다.")

        route = ROUTES.get((task, kind))
        if route is None:
            # ★임의로 다른 경로로 보내지 않는다. 여기서 멈추고 무엇을 고르면 되는지 알려준다.
            st.error(
                f"**{TASKS[task]} + {kind}** 은 지원하지 않는 조합입니다."
                f"{chr(10)}{chr(10)}"
                f"· 검수용 참고글로 **새로** 쓰려면 → `새 글 생성 + 검수용`{chr(10)}"
                f"· 이미 올린 글을 실전용으로 고치려면 → `기존 글 수정 + 실전용`")
            st.stop()
        flow, prod_mode = route
        is_prod = flow == "production"
        st.caption(f"→ 실행 경로: `{FLOWS[flow]['module']}`"
                   + (f" · mode=`{prod_mode}`" if is_prod else ""))

        # ★`기준 계정` 선택지는 **브랜드 기준시트에서 자동으로** 읽는다.
        #   시트에 `<이름> 기준랜딩` 탭을 추가하면 코드 수정 없이 그대로 늘어난다.
        #   위젯은 왼쪽부터 매체 · 결핍 · 기준 계정 순서로 보이지만, 탭이 정해져야
        #   결핍 목록을 읽을 수 있으므로 **탭을 먼저 만든다**(자리 배치는 그대로).
        mcol, dcol, acol = st.columns([1, 2, 2])
        try:
            tabinfo = load_tabs(brand.id)
        except Exception as exc:                               # noqa: BLE001
            st.error(f"[{brand.title}] 기준시트의 탭 목록을 읽지 못했습니다."
                     f"{chr(10)}{chr(10)}{exc}")
            st.stop()
        tab_list = tabinfo.get("tabs") or []
        if not tab_list:
            st.warning(f"[{brand.title}] 기준시트에 `기준랜딩` 이 이름에 들어간 탭이 "
                       f"없습니다. `<계정이름> 기준랜딩` 탭을 만들면 여기에 자동으로 "
                       f"나옵니다.")
            st.stop()

        ref_tab = acol.selectbox("기준 계정", tab_list, key=f"ref_tab_{brand.id}",
                                 help="기준시트의 어느 기준랜딩 탭을 쓸지 고릅니다. "
                                      "목록은 시트에서 자동으로 읽어옵니다")
        account = resolve_account(brand, ref_tab)

        # ── 참고용 랜딩 기준 시트 바로가기 · 선택 계정의 로그인 ID ──
        #   ★URL 은 하드코딩하지 않고 **선택한 브랜드 설정**에서 만든다.
        #   ★로그인 ID 는 accounts.json(git 제외)에서 읽어 **표시만** 한다.
        #     비밀번호는 저장하지도 보여주지도 않는다.
        lcol, icol = st.columns([1, 2])
        ref_url = brand.sheet_url(brand.reference_sheet_id)
        if ref_url:
            lcol.link_button(f"📄 {brand.title} 참고용 랜딩 기준 시트 열기", ref_url,
                             use_container_width=True,
                             help="새 탭에서 기준시트가 열립니다")
        if account and account.login_id:
            icol.markdown(info_block((account.title, f"ID: {account.login_id}")),
                          unsafe_allow_html=True)
        elif account:
            icol.caption(f"{account.title} · 로그인 ID 미등록"
                         f"(accounts.json 의 login_id 에 적으면 여기에 표시됩니다)")
        else:
            icol.caption("이 기준랜딩 탭은 아직 계정이 없습니다 — "
                         "[실행 준비] 를 누르면 만들어집니다")

        try:
            cat = load_catalog(brand.id, account.id if account else "", ref_tab)
        except Exception as exc:                               # noqa: BLE001
            st.error(f"[{brand.title}] 기준시트를 읽지 못했습니다.{chr(10)}{chr(10)}{exc}")
            st.stop()
        if not (cat.get("media") or []):
            st.warning(f"[{brand.title}] `{cat.get('tab')}` 탭에 매체/결핍 행이 없습니다. "
                       f"기준시트를 먼저 채워 주세요.")
            st.stop()

        media_list = cat.get("media") or []
        media = mcol.selectbox("매체", media_list, key="media")
        defs = catalog.deficiencies(cat, media, kind) or catalog.deficiencies(cat, media)
        deficiency = dcol.selectbox("결핍 / 제품", defs, key=f"def_{media}_{kind}")
        cap = st.columns([3, 1])
        cap[0].caption(f"목록 출처: `{brand.reference_title}` / `{cat.get('tab')}` · "
                       f"{str(cat.get('cached_at'))[:16]}"
                       f"{' (캐시)' if cat.get('from_cache') else ''} — "
                       f"기준랜딩 탭·결핍이 늘면 기준시트만 채우면 됩니다")
        if cap[1].button("목록 새로고침", use_container_width=True,
                         help="이 브랜드 기준시트에서 기준랜딩 탭 목록과 "
                              "결핍/제품 목록을 다시 읽어옵니다"):
            load_tabs.clear()
            load_catalog.clear()
            load_tabs(brand.id, refresh=True)                  # 기준 계정(탭) 목록
            load_catalog(brand.id, account.id if account else "", ref_tab,
                         refresh=True)                          # 결핍/제품 목록
            st.rerun()

        # ★캠페인 접두사는 'UTM 빌더 시트의 행을 좁힐 때' 만 쓴다.
        #   · 실전용  — 항상 쓴다(대상 행을 고르는 기준)
        #   · 검수용  — 날짜를 넣어 시트에 기록할 때만 쓴다. 날짜가 없으면 기록 자체를
        #               하지 않으므로 입력칸을 숨긴다(쓰이지 않는 값이라 혼란만 준다).
        needs_campaign = is_prod or bool(st.session_state.get("date", today_tag()).strip())

        cols = st.columns(3 if needs_campaign else 2)
        count = cols[0].number_input(
            "처리 건수 (0=전부)" if is_prod else "생성 개수",
            min_value=0, max_value=200,
            value=0 if is_prod else 1, step=1, key=f"count_{flow}")
        date = cols[1].text_input("시트 날짜", value=today_tag(), key="date",
                                  help="UTM 빌더 시트에서 이 날짜 행을 대상으로 합니다")
        if needs_campaign:
            campaign = cols[2].text_input(
                "utm_campaign 접두사", value="", key=f"camp_{flow}",
                placeholder="예: g_i_b_o_l_0831",
                help="같은 날짜에 그룹이 여럿일 때 그 그룹만 고릅니다. "
                     "`g_i_b_o_l_0831` 을 넣으면 `_1` 부터 `_20`, `_100` 까지 "
                     "순번 자릿수와 관계없이 모두 매칭됩니다. 비우면 그 날짜 전체입니다.")
        else:
            campaign = ""
            st.caption("· 시트 날짜가 비어 있어 `utm_campaign 접두사` 는 쓰이지 않습니다"
                       "(입력칸 숨김).")

        # ★`실전용 방식`(prod_mode) 선택 UI 는 없앴다 — 위 '작업 종류 + 참고 랜딩'
        #   조합이 이미 정하므로 중복이었다(같은 걸 두 번 묻고 서로 어긋날 수 있었다).
        #   `제목/본문 출처`(content_from) 는 다른 축이라 그대로 둔다.
        if is_prod:
            content_from = st.selectbox(
                "제목/본문 출처", ("ref", "review"),
                format_func=lambda k: ("ref — 실전용 참고글" if k == "ref"
                                       else "review — 기존 검수용 글"),
                key="content_from")
        else:
            content_from = "ref"

        show_window = st.checkbox("브라우저 창 보기", value=False,
                                  help="끄면 창 없이(headless) 돕니다. "
                                       "로그인할 때만 창이 뜹니다")
        keep_going = st.checkbox("한 건 실패해도 나머지 계속 (실전용)", value=True,
                                 disabled=not is_prod,
                                 help="실패한 건은 발행하지 않고 시트에도 남기지 않습니다. "
                                      "끄면 예전처럼 그 배치를 통째로 멈춥니다")
        # ★테스트 계정처럼 `랜딩` 탭에 기록할 자리가 없을 때 쓰는 스위치(2026-09-04 사용자 요청).
        #   글이 제대로 써지고 발행되는지만 보고 싶은데 '기록할 자리가 0개' 로 막히면 확인 자체를 못 한다.
        no_sheet = st.checkbox("시트에 기록하지 않기 (발행만 테스트)", value=False,
                               key=f"no_sheet_{flow}",
                               help="`랜딩` 탭의 빈 행 확인과 발행 URL 기록을 모두 건너뜁니다. "
                                    "테스트 계정으로 작성·발행만 확인할 때 켜세요")
        if no_sheet:
            st.caption("· 시트 인자를 아예 보내지 않습니다 — **Agent 버전과 무관하게** "
                       "`랜딩` 탭을 건드리지 않습니다.")

        # ── Job 조립 ─────────────────────────────────────────────
        #   ★고급 CLI 옵션(--ref-tab · --batch · --url · --product-url ·
        #     --sheet-product · --start · --copy-mode · --ref-copy-from · --no-sheet)은
        #     **화면에 두지 않는다.** 값을 넣지 않으면 각 CLI 의 기존 기본값이 그대로
        #     쓰인다(옵션 자체는 CLI 에 그대로 살아 있다 — 필요하면 터미널에서 준다).
        #       · --ref-tab       비움 → 계정(같은 브랜드) → 브랜드 기본 탭
        #       · --sheet-product 안 줌 → 결핍 앞 단어로 자동 대조 (run.py `_sheet_product`)
        #       · --batch/--start/--copy-mode/--ref-copy-from → CLI 기본값
        def build_job(dry: bool) -> Job:
            job = Job(flow=flow, brand=brand.id,
                      # ★화면이 보고 있는 브랜드 설정을 그대로 실어 보낸다.
                      #   PC 에 그 브랜드가 없어도 화면과 같은 시트를 쓴다.
                      brand_config=brands.config_json(),
                      account=account.id,
                      # ★계정 이름·로그인 ID 도 함께 싣는다. 실행 기록이
                      #   "계정=행복하서연 (rhksrhf6996)" 로 남아야 나중에 볼 수 있다.
                      account_name=account.label or account.id,
                      login_id=getattr(account, "login_id", "") or "",
                      ref_tab=ref_tab,          # ★고른 기준랜딩 탭을 그대로 넘긴다
                      media=media, deficiency=deficiency, kind=kind,
                      count=int(count), publish=not dry, dry_run=dry,
                      headless=(False if show_window else None), events=True)
            # ★새로 생긴 Job 필드는 **생성자 대신 나중에** 붙인다.
            #   Streamlit Cloud 는 파일이 바뀌면 이 스크립트만 다시 돌리고, 이미 import 해 둔
            #   `v2.*` 모듈은 옛것을 그대로 쓸 때가 있다. 그때 새 인자를 생성자에 넘기면
            #   `TypeError: unexpected keyword argument` 로 **화면이 통째로 죽는다**(2026-09-04 실제 발생).
            #   여기서 걸러 두면 옛 모듈일 때는 그 옵션만 조용히 빠진다
            #   (`Job.from_dict` 가 모르는 키를 버리는 것과 같은 태도).
            if _job_has("no_sheet"):
                job.no_sheet = bool(no_sheet)
            if is_prod:
                job.date = date.strip()
                job.campaign = campaign.strip()
                job.mode = prod_mode
                job.content_from = content_from
                job.on_error = "skip" if keep_going else "abort"
            else:
                job.count = max(1, int(count))
                # ★'시트에 기록하지 않기' 면 **시트 인자를 아예 안 보낸다.**
                #   `--no-sheet` 플래그에만 기대면 그 옵션을 아는 새 Agent 가 깔려 있어야 하는데,
                #   설치가 안 된 PC 에서는 옛 Agent 가 플래그를 버리고 그대로 사전검사에 걸린다
                #   (2026-09-04 실제로 겪음). 반면 **인자를 안 주는 것**은 예전부터 있던 동작이라
                #   Agent 버전이 무엇이든 시트를 건드리지 않는다. 버전에 안 매이게 이쪽을 쓴다.
                #   (`--no-sheet` 는 그대로 같이 보낸다 — 새 Agent 에서는 의도가 로그에 남는다.)
                if date.strip() and not no_sheet:
                    job.sheet_media = catalog.sheet_media_for(media)
                    job.sheet_date = date.strip()
                    job.sheet_campaign = campaign.strip()
                # job.sheet_product 는 None 그대로 = --sheet-product 를 주지 않는다
                #   → run.py 가 결핍 앞 단어로 자동 대조한다(기존 동작).
            return job

        preview = build_job(dry=False)
        problems = preview.validate()
        if problems:
            st.warning(" · ".join(problems))

        # ── 실행 전 확인 (읽기 전용 · 브라우저 안 켬) ──────────────
        with st.expander("실행 전 확인", expanded=True):
            st.write({
                "브랜드": brand.title,
                "기준 계정": ref_tab,
                "작업 종류": f"{TASKS[task]} + {kind}"
                          + f"  →  {FLOWS[flow]['module']}"
                          + (f" · mode={prod_mode}" if is_prod else ""),
                "참고 랜딩 종류": kind,
                "매체": media,
                "결핍": deficiency,
                "날짜": date.strip() or "(없음)",
                "캠페인": campaign.strip() or "(전체)",
                "건수": int(count) if int(count) else "전부",
                "기준시트": f"{brand.reference_title} / {cat.get('tab')}",
                "네이버 계정(세션)": (account.title if account
                                 else "(아직 없음 — 실행 준비 때 만듭니다)"),
                "UTM 빌더": brand.utm_title,
            })
            if st.button("시트 조회해서 매칭 확인 (읽기 전용)",
                         use_container_width=True):
                try:
                    info = preview_rows(brand.id, flow, media, deficiency, kind,
                                        date.strip(), campaign.strip(), prod_mode)
                except Exception as exc:                       # noqa: BLE001
                    st.error(f"{exc}")
                else:
                    ref = info.get("reference") or {}
                    st.success(f"기준시트 {ref.get('row')}행 — 참고글 {ref.get('url')}")
                    if ref.get("product_url"):
                        st.caption(f"기준시트 제품 링크: {ref['product_url']}")
                    rows = info.get("rows") or []
                    if rows:
                        st.caption(f"매칭된 행 {len(rows)}개 · "
                                   f"최종 제품 URL 은 행마다 다릅니다")
                        st.dataframe(rows, use_container_width=True, hide_index=True)
                    elif flow == "production":
                        st.warning("매칭된 행이 없습니다.")
                    else:
                        st.caption("검수용은 기록할 자리 확인을 Dry-run 에서 합니다.")
                    with st.popover("시트 조회 로그"):
                        st.code(chr(10).join(info.get("log") or []) or "(없음)")

        # ── 실행 준비 (로그인 + 실행 준비를 버튼 하나로) ──────────
        #   ★기존 기능을 그대로 재사용한다: `v2.session --login` 을 큐에 넣고
        #     그 PC 의 에이전트가 로그인 창을 연다. 로그인은 사람이 직접 한다.
        info = session_store.describe(account) if account else {
            "state_exists": False, "cookies": 0, "saved_at": "", "profile": ""}
        st.session_state["_auto_refresh"] = False
        last_login = session_job(store, account, brand) if account else None
        job_state = (last_login or {}).get("status") or ""
        ready_info = session_ready(store, last_login)
        # ★로컬에 계정 파일이 있느냐로 판단하지 않는다. 그 PC 가 "준비됐다" 고
        #   돌려준 응답(session_ready)과, 화면에서 고른 계정으로 판단한다.
        logged_in = bool(info["state_exists"]) or bool(ready_info.get("session_ready"))
        ready = bool(agent_ready) and logged_in

        st.divider()
        if ready:
            who = ready_info.get("account_name") or account.label
            got_id = ready_info.get("login_id") or account.login_id
            st.success(f"● 실행 준비 완료 — {who}"
                       + (f" ({got_id})" if got_id else "")
                       + f" · {ref_tab}")
            # ★로그인 다음은 **자동으로 이어지지 않는다**(실수 발행을 막으려고).
            #   그 사실을 적어 두지 않아 "멈춘 것 같다" 는 이야기가 나왔다.
            st.info("여기서 자동으로 글이 써지지는 않습니다."
                    f"{chr(10)}{chr(10)}**바로 아래**로 내려가서 **[Dry-run (발행 없음)]** 을 먼저 눌러 "
                    "어떤 행이 잡히는지 확인하고, 괜찮으면 **[실전 실행]** 을 눌러 주세요.")
        elif job_state in RUNNING_STATES:
            # 로그인이 끝나면 저절로 바뀌도록, 이 화면을 잠시 뒤 다시 그린다
            #   (아래 맨 끝에서 한 번만 건다 — 그리는 도중에 걸면 화면이 깨진다)
            st.session_state["_auto_refresh"] = True
            st.info("▶ 로그인 창이 열렸습니다 — 네이버 로그인을 직접 마쳐 주세요."
                    f"{chr(10)}로그인이 끝나면 세션이 저장되고 자동으로 준비 완료가 됩니다.")
        else:
            st.warning("○ 준비 필요 — 아래 [실행 준비] 를 눌러 주세요")

        st.caption("※ 로그인창은 **이 화면 안에 뜨지 않습니다.** "
                   "Agent 가 설치된 PC 의 바탕화면에 Chromium 창이 열립니다.")
        if st.button("실행 준비", type="primary", use_container_width=True,
                     disabled=not agent_ready,
                     help="네이버 로그인 창을 엽니다. 직접 로그인하면 세션이 저장되고 "
                          "실행 준비가 끝납니다"):
            # ★이 기준랜딩 탭에 계정(세션 폴더)이 아직 없으면 여기서 만든다.
            #   기존 계정이 있으면 그대로 쓴다(세션 재사용).
            # 계정이 없으면 **로그인하는 PC 에서** 만든다. 여기(서버)서 만들면
            # 서버 안에만 생겨서 PC 는 "계정을 찾지 못했습니다" 가 난다.
            # 탭 이름이 같으면 id 도 같으므로 미리 계산해 보내면 어긋나지 않는다.
            # 계정은 위에서 이미 정해 뒀다(없으면 탭 이름으로 만든 것).
            #   실제 등록은 **로그인하는 PC 에서** 한다.
            res = submit_session_job(store, brand, account, "--login",
                                     device["device_id"] if device else "",
                                     ref_tab=ref_tab)
            if res.ok:
                st.session_state["job_id"] = res.job_id
                st.toast("실행 준비를 시작했습니다 — 로그인 창을 확인해 주세요.")
                st.rerun()
            # 실패·확인 불가 안내는 submit_guarded 가 이미 그렸다(여기서 다시 넣지 않는다)

        # 지금 어디까지 왔는지 — 버튼 바로 아래에서 보인다
        watching = session_job(store, account, brand) if account else None
        if watching:
            render_progress(store, watching, "실행 준비 진행 상황")

        st.caption(("· 로그인 세션: 있음 "
                    f"(쿠키 {info['cookies']}개 · {info['saved_at'][:16]})"
                    if logged_in else "· 로그인 세션: 없음")
                   + f"{chr(10)}· 네이버 로그인은 매번 직접 하셔야 합니다.")

        # ── 계정 세션 관리(보조) ─────────────────────────────────
        #   ★평소 발행에는 **아무것도 누를 필요가 없다.** 접어 둔다.
        #   ★여기서 하는 일은 모두 **고른 계정 하나**에만 적용된다.
        if account:
            render_account_tools(store, brand, account, ready_info, device)

        if not agent_ready:
            st.info("이 컴퓨터의 **Agent 가 연결되면** 실행 버튼이 열립니다 "
                    "(왼쪽에서 설치·연결).")
        elif not ready:
            st.info("**실행 준비**(네이버 로그인)를 끝내면 [실전 실행] 이 열립니다. "
                    "Dry-run 은 로그인 없이도 지금 눌러 볼 수 있습니다.")

        # ★Dry-run 은 시트만 읽고 브라우저를 켜지 않는다 → **네이버 로그인 전에도 가능**.
        #   실전 실행만 로그인 세션까지 요구한다.
        busy = running_job(store, device)

        # ★버튼이 회색이면 왜 그런지 그 자리에서 알려 준다.
        #   예전에는 아무 말 없이 잠겨 있어 "눌러도 안 된다" 로만 보였다.
        blockers = []
        if problems:
            blockers.append("시트에서 걸린 것이 있습니다 — " + " · ".join(problems))
        if not agent_ready:
            blockers.append("이 화면에 PC 가 연결돼 있지 않습니다(왼쪽 [내 PC] 확인)")
        if busy:
            blockers.append(f"이 PC 에서 다른 작업이 도는 중입니다 — {busy}")
        if blockers:
            st.error("**지금은 실행할 수 없습니다**" + chr(10) + chr(10)
                     + chr(10).join(f"- {b}" for b in blockers))
        elif not ready:
            st.info("**[실전 실행]** 은 위쪽 **[실행 준비]**(네이버 로그인)를 끝내야 열립니다."
                    f"{chr(10)}**[Dry-run]** 은 로그인 없이 지금 눌러 보실 수 있습니다.")
        else:
            st.caption("아래 두 버튼 중 하나를 누르면 글쓰기가 시작됩니다. "
                       "**[Dry-run]** 은 글을 올리지 않습니다.")

        b1, b2 = st.columns(2)
        if b1.button("Dry-run (발행 없음)", use_container_width=True,
                     disabled=bool(problems) or not agent_ready or bool(busy),
                     help="실제로 글을 올리지 않습니다. 시트에서 어떤 행이 잡히는지만 "
                          "확인합니다(네이버 로그인 없이도 됩니다)"):
            job = build_job(dry=True)
            res = submit_guarded(
                store, job,
                title=f"[dry][{brand.title}] {FLOWS[flow]['label']} · "
                      f"{media}/{deficiency}",
                target_agent=device["device_id"] if device else "",
                label="Dry-run 작업")
            if res.ok:
                st.session_state["job_id"] = res.job_id
                st.rerun()

        # ★확인 체크박스는 두지 않는다(2026-08-31 사용자 요청).
        #   오발행 방지는 **실행 준비 완료 상태에서만 버튼이 열리는 것**으로 갈음한다.
        if b2.button("실전 실행", type="primary", use_container_width=True,
                     disabled=bool(problems) or not ready or bool(busy)):
            job = build_job(dry=False)
            title = (f"[{brand.title}] {FLOWS[flow]['label']} · "
                     f"{media}/{deficiency}")
            if int(count) > 0:
                # ★10개씩 **순차** 배치로 쪼갠다. 첫 배치만 지금 큐에 올라가고,
                #   다음 배치는 사용자가 다시 로그인해야 이어진다.
                plan = batch_plan.create(
                    job.to_dict(), int(count), title=title, brand=brand.id,
                    account=account.id if account else "", flow=flow,
                    mode=prod_mode,
                    device=device["device_id"] if device else "")
                st.session_state["plan_id"] = plan["id"]
                st.session_state.pop("job_id", None)
                st.rerun()
            else:
                # 건수 0(=전부)은 총 건수를 알 수 없어 나누지 않는다(기존 동작).
                res = submit_guarded(
                    store, job, title=title,
                    target_agent=device["device_id"] if device else "",
                    label="실전 실행 작업")
                if res.ok:
                    st.session_state["job_id"] = res.job_id
                    st.rerun()
        # 등록 여부를 확인하지 못한 제출이 있으면 **조회로만** 되찾는다
        render_pending_submit(store)

        # 지금 어디까지 왔는지 — 실행 버튼 바로 아래에서 보인다
        running_id = st.session_state.get("job_id") or ""
        watch_run = store.get(running_id) if running_id else None
        if watch_run:
            render_progress(store, watch_run, "실행 진행 상황")
            if watch_run.get("status") in RUNNING_STATES:
                st.session_state["_auto_refresh"] = True

        st.caption(f"버튼을 누르면 **큐에 넣기만** 합니다. 실제 실행은 에이전트가 합니다."
                   f"{chr(10)}건수를 넣으면 **{batch_plan.BATCH_SIZE}개씩 순차로** 나눠 "
                   f"실행하고, 배치가 바뀔 때마다 다시 로그인합니다"
                   f"(0=전부 는 나누지 않습니다).")

        # ══ 배치 진행 ═══════════════════════════════════════════
        plan = batch_plan.get(st.session_state.get("plan_id") or "")
        if plan is None:            # 새로고침 등으로 잃어버렸으면 진행 중인 것을 되찾는다
            open_plans = [q for q in batch_plan.list_plans(8) if batch_plan.is_open(q)]
            plan = open_plans[0] if open_plans else None
            if plan:
                st.session_state["plan_id"] = plan["id"]
        if plan:
            plan = advance_plan(store, plan)
            cur = batch_plan.current(plan)
            sizes = plan.get("sizes") or []
            done_n = batch_plan.done_count(plan)

            st.divider()
            st.markdown(f"#### 배치 진행 — {batch_plan.summary(plan)}")
            st.progress(min(1.0, done_n / max(1, int(plan.get("total") or 1))),
                        text=f"완료 {done_n}/{plan.get('total')}건")
            mark = {batch_plan.DONE: "✅", batch_plan.RUNNING: "▶",
                    batch_plan.LOGGING_IN: "🔑", batch_plan.LOGIN_WAIT: "⏸",
                    batch_plan.PENDING: "·", batch_plan.FAILED: "❌",
                    batch_plan.CANCELED: "⏹"}
            st.markdown("  \n".join(
                f"{mark.get(b['status'], '·')} 배치 {b['no']} — {b['size']}건"
                + (f" · 발행 {b['published']}건" if b.get("published") else "")
                + (f" · {b['error'][:80]}" if b.get("error") else "")
                for b in plan.get("batches") or []))

            # 지금 도는 작업을 아래 진행 패널에 연결한다(자동 새로고침도 여기서 걸린다)
            if cur and cur.get("status") == batch_plan.RUNNING and cur.get("run_job"):
                st.session_state["job_id"] = cur["run_job"]
            elif cur and cur.get("status") == batch_plan.LOGGING_IN \
                    and cur.get("login_job"):
                st.session_state["job_id"] = cur["login_job"]

            # ── 건별 결과 · 실패 목록 ──────────────────────────
            res = plan_results(store, plan)
            if res["items"]:
                st.caption(f"총 {plan.get('total')}건 / 성공 {res['ok']} / "
                           f"실패 {res['fail']}")
                with st.expander(f"건별 결과 ({len(res['items'])}건)",
                                 expanded=bool(res["fail"])):
                    st.markdown("  \n".join(
                        (f"✅ 순번 {x['순번']} — 완료" if x["ok"] else
                         f"❌ 순번 {x['순번']} — 실패: {x['사유']}"
                         + (f" ({x.get('구분')} 오류)" if x.get("구분") else ""))
                        + (f"  · {x['행']}행" if x["행"] else "")
                        for x in res["items"][-60:]))
            if res["fails"]:
                st.error(f"실패 {res['fail']}건 — 아래 목록을 확인하세요")
                st.dataframe(
                    [{"순번": x["순번"], "utm_campaign": x["utm_campaign"],
                      "시트 행": x["행"], "실패 단계": x["단계"],
                      "실패 사유": x["사유"], "구분": x.get("구분", ""),
                      "블로그 URL": x["블로그 URL"], "오류": x.get("오류", "")}
                     for x in res["fails"]],
                    use_container_width=True, hide_index=True)

            if plan.get("status") == batch_plan.DONE:
                if res["fail"]:
                    st.warning(f"🏁 모든 배치 종료 — 총 {plan.get('total')}건 / "
                               f"성공 {res['ok']} / 실패 {res['fail']}")
                else:
                    st.success(f"🏁 모든 배치 완료 — 총 {plan.get('total')}건 "
                               f"(성공 {res['ok']})")
                cc = st.columns(2)
                if res["failed_rows"] and cc[0].button(
                        f"실패 {res['fail']}건만 재실행", type="primary",
                        use_container_width=True,
                        help="실패한 시트 행만 다시 큐에 넣습니다. "
                             "이미 성공한 건은 절대 다시 실행하지 않습니다"):
                    retry_job = dict(plan.get("job") or {})
                    if plan.get("flow") == "production":
                        # ★행 번호를 콕 집어 넘긴다(--rows) — 성공한 행은 포함되지 않는다
                        retry_job["rows"] = ",".join(res["failed_rows"])
                        retry_job["start"] = 1
                    new_plan = batch_plan.create(
                        retry_job, res["fail"],
                        title=f"{plan.get('title')} · 실패 {res['fail']}건 재실행",
                        brand=plan.get("brand", ""), account=plan.get("account", ""),
                        flow=plan.get("flow", "review"),
                        mode=plan.get("mode", "convert"),
                        device=plan.get("device", ""))
                    st.session_state["plan_id"] = new_plan["id"]
                    st.rerun()
                if cc[1].button("배치 진행 닫기", use_container_width=True):
                    st.session_state.pop("plan_id", None)
                    st.rerun()
            elif plan.get("status") == batch_plan.FAILED:
                st.error("배치가 실패해 멈췄습니다. 원인을 확인한 뒤 다시 실행해 주세요.")
                if st.button("배치 진행 닫기", use_container_width=True):
                    batch_plan.cancel(plan)
                    st.session_state.pop("plan_id", None)
                    st.rerun()
            elif cur and cur.get("status") == batch_plan.RECOVER_WAIT:
                # ★공통 오류(로그인 만료 · 브라우저 종료 · 시트 접근 불가)
                st.error(f"⛔ 재로그인/복구 필요 — 배치 {cur['no']} 가 공통 오류로 "
                         f"멈췄습니다.{chr(10)}{chr(10)}{cur.get('error', '')}")
                st.caption("이미 발행된 건은 건너뛰고, 남은 건부터 이어서 실행합니다.")
                rc = st.columns(2)
                if rc[0].button("재로그인하고 이어서 실행", type="primary",
                                use_container_width=True):
                    batch_plan.prepare_retry(plan)      # 발행된 만큼 건너뛴다
                    acc = accounts.ensure_for_tab(ref_tab, brand, create=True)
                    start_batch_login(store, plan, brand, acc,
                                      device["device_id"] if device else "")
                    st.rerun()
                if rc[1].button("여기서 중단", use_container_width=True):
                    batch_plan.cancel(plan)
                    st.session_state.pop("plan_id", None)
                    st.rerun()
            elif cur and cur.get("status") == batch_plan.LOGIN_WAIT:
                st.warning(f"⏸ 다음 배치 로그인 필요 — 배치 {cur['no']}"
                           f"({cur['size']}건)를 시작하려면 네이버에 다시 "
                           f"로그인해 주세요.")
                if st.button("로그인하고 이어서 실행", type="primary",
                             use_container_width=True):
                    acc = accounts.ensure_for_tab(ref_tab, brand, create=True)
                    start_batch_login(store, plan, brand, acc,
                                      device["device_id"] if device else "")
                    st.rerun()
            elif cur and cur.get("status") == batch_plan.LOGGING_IN:
                st.info("🔑 로그인 창에서 로그인해 주세요 — 끝나면 이 배치가 "
                        "자동으로 이어서 실행됩니다.")
            elif cur and cur.get("status") == batch_plan.RUNNING:
                st.info(f"▶ 배치 {cur['no']} 실행 중 — {cur['size']}건")

            if batch_plan.is_open(plan) and st.button("배치 전체 중단",
                                                      use_container_width=True):
                for key in ("run_job", "login_job"):
                    if cur and cur.get(key):
                        cancel_guarded(store, cur[key], "배치")
                batch_plan.cancel(plan)
                st.session_state.pop("plan_id", None)
                st.rerun()

# ── 오른쪽: 최근 실행 기록 ────────────────────────────────────────
with right:
    st.subheader("최근 실행 기록")
    jobs = store.list_jobs(limit=20)
    if not jobs:
        st.caption("아직 실행한 작업이 없습니다.")
    else:
        st.dataframe([job_summary(r) for r in jobs], use_container_width=True,
                     hide_index=True, height=340)
        pick = {f"{BADGE.get(r.get('status'), '')} {r.get('title')}  "
                f"[{r['id'][9:15]}]": r["id"] for r in jobs}
        chosen = st.selectbox("자세히 볼 작업", list(pick), key="pick_job")
        if st.button("이 작업 열기", use_container_width=True):
            st.session_state["job_id"] = pick[chosen]
            st.rerun()

# ══════════════════════════════════════════════════════════════════
# 아래 — 진행 상황 / 단계 / 로그 / 발행 URL
# ══════════════════════════════════════════════════════════════════
st.divider()
job_id = st.session_state.get("job_id")
rec = store.get(job_id) if job_id else None

if rec is None:
    st.info("작업을 실행하면 여기에 진행 상황과 발행 URL 이 나옵니다.")
else:
    status = rec.get("status")
    head = st.columns([3, 1, 1, 1, 1])
    head[0].markdown(f"### {BADGE.get(status, status)} — {rec.get('title')}")
    total = int(rec.get("total") or 0)
    made = int(rec.get("made") or 0)
    published = list(rec.get("published") or [])
    head[1].metric("전체", total or "—")
    head[2].metric("작성", made)
    head[3].metric("발행", len(published))
    if status in RUNNING_STATES:
        if head[4].button("중단", type="secondary", use_container_width=True):
            if cancel_guarded(store, rec["id"]):
                st.toast("중단을 요청했습니다.")
                time.sleep(1.0)
                st.rerun()
    else:
        head[4].caption(f"코드 {rec.get('exit_code')}")

    st.caption(f"브랜드 {brand_name(rec.get('brand')) or '(없음)'} · "
               f"계정 {(rec.get('job') or {}).get('account') or '(기본)'} · "
               f"에이전트 {rec.get('agent') or '—'} · {rec.get('created_at', '')[:19]}")

    if total:
        st.progress(min(1.0, (made + len(published)) / max(1, total * 2)),
                    text=f"작성 {made}/{total} · 발행 {len(published)}/{total}")
    elif status == queue_store.RUNNING:
        st.progress(0.0, text="시작하는 중…")

    if rec.get("error"):
        st.error(rec["error"])
    if status == queue_store.PENDING and not alive:
        st.warning("대기 중입니다 — 에이전트가 떠 있지 않으면 실행되지 않습니다"
                   "(왼쪽에서 시작하세요).")

    events, _ = store.read_events(rec["id"])
    tab_step, tab_log, tab_url, tab_ev = st.tabs(
        ["진행 단계", "진행 로그", f"발행 URL ({len(published)})", "원본 이벤트"])
    with tab_step:
        render_steps(events)
    with tab_log:
        text, _ = store.read_log(rec["id"])
        st.code(text[-12000:] or "(아직 출력이 없습니다)", language="log")
    with tab_url:
        if published:
            st.code(chr(10).join(published), language="text")
            for url in published:
                st.markdown(f"- [{url}]({url})")
        else:
            st.caption("아직 발행된 URL 이 없습니다.")
    with tab_ev:
        st.json(events[-40:] if events else [], expanded=False)

    auto = st.checkbox("자동 새로고침 (실행 중일 때)", value=True, key="auto_refresh")
    if auto and status in RUNNING_STATES:
        time.sleep(1.5)
        st.rerun()


# ══════════════════════════════════════════════════════════════════
# 작업이 도는 동안 — 스스로 새로고침
#   ★사람이 PC 에서 로그인을 마치거나 글이 올라가는 순간을 화면은 알 수 없다.
#     그래서 도는 동안만 몇 초마다 다시 그려, 끝나면 저절로 결과가 보이게 한다.
#   ★반드시 **맨 끝에서 한 번만** 건다. 화면을 그리는 도중에 걸면 방금 그린
#     것과 부딪혀 빨간 오류(removeChild)가 난다.
# ══════════════════════════════════════════════════════════════════
if st.session_state.get("_auto_refresh"):
    time.sleep(2.0)
    st.rerun()
