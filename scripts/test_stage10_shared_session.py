r"""Stage 10 검증 — 공유 세션 보호 A안(같은 blog_id 를 위험 후보로)
    (2026-11-05 · 브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage10_shared_session.py

★브라우저·네이버·시트·운영 큐·운영 DB·**운영 계정/세션**을 건드리지 않는다.
  임시 폴더에 **합성 계정**과 **합성 세션 파일**만 만든다. 실제 로그아웃·서버
  로그아웃·쿠키 삭제를 하지 않는다(`cmd_logout` 은 소스만 읽어 검증한다).

검증 항목(사용자 요청 4-3 ①~⑦)
  ① `session_peers` ④번 경로 — 같은 blog_id 를 후보로 올린다
  ② 단정하지 않는다 — 같은 login_id 로 합치지 않는다(후보일 뿐)
  ③ login_id 가 다르거나 미확인이면 **차단**
  ④ `logout_allowed`·`cmd_logout`·`_server_logout` 판정 규칙 유지
  ⑤ `--confirm-shared` 우회 시 영향받을 계정을 명확히 안내하는가
  ⑥ login_id 미등록 계정의 오탐·과도한 차단 — 실제 등록 모양으로 수치화
  ⑦ 기존 ①②③ 경로(profile_dir·별칭)가 그대로 동작하는가
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage10_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ["BLOG_QUEUE_BACKEND"] = "local"
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from v2 import login_state as LS                              # noqa: E402
from v2 import session_store as SS                            # noqa: E402
from v2.accounts import Account                               # noqa: E402

OK, NG = "  [o]", "  [X]"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def head(t: str) -> None:
    print("\n" + "=" * 76)
    print(f" {t}")
    print("=" * 76)


# ── 합성 계정표 ───────────────────────────────────────────────────
def acc(ident, blog="", login="", brand="", tab="", profile="",
        enabled=True) -> Account:
    return Account(id=ident, label=ident, blog_id=blog, login_id=login,
                   ref_tab=tab, brand=brand, enabled=enabled,
                   profile_dir=profile)


def write_accounts(rows: list[Account]) -> Path:
    path = Path(tempfile.mkdtemp(prefix="acc_")) / "accounts.json"
    path.write_text(json.dumps({"accounts": [r.to_dict() for r in rows]},
                               ensure_ascii=False), encoding="utf-8")
    return path


def give_session(ident: str, cookies: int, blog: str = "") -> None:
    """합성 세션 파일을 만든다(쿠키 값은 가짜, 임시폴더 안)."""
    d = SS.SESSIONS_ROOT / ident if hasattr(SS, "SESSIONS_ROOT") else None
    if d is None:
        d = Path(os.environ["BLOG_DATA_DIR"]) / "sessions" / ident
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "cookies": [{"name": f"c{i}", "value": "x"} for i in range(cookies)],
        "saved_at": "2026-11-05T10:00:00", "blog_id": blog,
    }, ensure_ascii=False), encoding="utf-8")


APP_SESSION = (ROOT / "v2" / "session.py").read_text(encoding="utf-8")
APP_LS = (ROOT / "v2" / "login_state.py").read_text(encoding="utf-8")

# ══════════════════════════════════════════════════════════════════
head("① 같은 blog_id 를 위험 후보로 올린다 (④번 경로)")
A = acc("a_main", blog="myblog", login="loginA")
B = acc("b_other", blog="myblog")                   # login_id 미등록
C = acc("c_far", blog="otherblog", login="loginC")
P = write_accounts([A, B, C])

peers = LS.session_peers(A, P)
ids = sorted(p["id"] for p in peers)
check("같은 blog_id 등록이 후보로 올라온다", ids == ["b_other"], str(ids))
check("  다른 blog_id 는 올라오지 않는다", "c_far" not in ids)
check("  사유에 blog_id 가 같다고 적힌다",
      peers and "blog_id=myblog 가 같습니다" in peers[0]["why"],
      peers[0]["why"][:52] if peers else "")
check("★단정하지 않는다 — '수 있습니다(확인되지 않음)' 로 적는다",
      peers and "확인되지 않음" in peers[0]["why"])
check("  자기 자신은 후보가 아니다", all(p["id"] != "a_main" for p in peers))

#  blog_id 가 비면 ④를 쓰지 않는다(빈 값끼리 묶이면 전부 후보가 된다)
E1, E2 = acc("e1"), acc("e2")
PE = write_accounts([E1, E2])
check("★blog_id 가 둘 다 비면 후보로 묶지 않는다",
      LS.session_peers(E1, PE) == [], str(LS.session_peers(E1, PE)))
check("  대소문자는 같은 것으로 본다(blog_id 규칙 그대로)",
      len(LS.session_peers(acc("x1", blog="MyBlog"),
                           write_accounts([acc("x1", blog="MyBlog"),
                                           acc("x2", blog="myblog")]))) == 1)
check("  blog_id 에서 도메인을 떼지 않는다(login_id 규칙과 다르다)",
      LS.session_peers(acc("y1", blog="abc"),
                       write_accounts([acc("y1", blog="abc"),
                                       acc("y2", blog="abc@naver.com")])) == [])

head("② 같은 login_id 로 합치지 않는다 — 후보일 뿐")
check("후보에 상대의 login_id 가 그대로 담긴다(없으면 빈 문자열)",
      peers and peers[0]["login_id"] == "", str(peers[0]))
check("  내 login_id 를 상대에게 복사하지 않는다",
      peers and peers[0]["login_id"] != "loginA")
check("  판정은 logout_allowed 가 한다(session_peers 는 bool 을 내지 않는다)",
      isinstance(peers, list))

head("③ login_id 가 다르거나 미확인이면 차단")
allowed, got_peers, why = LS.logout_allowed(A, P)
check("★login_id 미등록 후보가 있으면 **차단**", allowed is False, str(allowed))
check("  사유에 '확인되지 않았습니다' 가 들어간다", "확인되지 않았습니다" in why,
      why[:60])
check("  영향 건수를 적는다", "1건" in why, why[:60])

D1 = acc("d1", blog="shared", login="same@naver.com")
D2 = acc("d2", blog="shared", login="same")        # 표기만 다른 같은 계정
PD = write_accounts([D1, D2])
ok2, pr2, why2 = LS.logout_allowed(D1, PD)
check("★같은 로그인 계정(표기만 다름)이면 허용", ok2 is True, str(ok2))
check("  그때도 영향 범위를 알려 준다(함께 재로그인 필요)",
      "함께 재로그인이 필요해집니다" in why2, why2[:60])

F1 = acc("f1", blog="shared2", login="loginF")
F2 = acc("f2", blog="shared2", login="someoneelse")   # 다른 로그인
PF = write_accounts([F1, F2])
ok3, _, why3 = LS.logout_allowed(F1, PF)
check("★로그인 계정이 **다르면** 차단", ok3 is False, str(ok3))

G1 = acc("g1", blog="shared3")                     # 내 login_id 도 미등록
G2 = acc("g2", blog="shared3", login="loginG")
PG = write_accounts([G1, G2])
ok4, _, _ = LS.logout_allowed(G1, PG)
check("  내 login_id 가 미등록이면(상대는 등록) 차단", ok4 is False)

head("④ 판정 규칙·기존 동작 유지")
check("logout_allowed 의 세 분기가 그대로다(없음/같음/그 밖)",
      "if not peers:" in APP_LS and "if not odd:" in APP_LS
      and "same_login(p[\"login_id\"], mine)" in APP_LS)
check("  blog_id 비교는 same_id(도메인 안 뗀다), login_id 는 same_login",
      "same_id(other.blog_id, mine_blog)" in APP_LS
      and "same_login(p[\"login_id\"], mine)" in APP_LS)
check("cmd_logout 의 차단 조건이 그대로다",
      "if not allowed and not confirm_shared:" in APP_SESSION)
check("  확인 후 진행 조건도 그대로다",
      "if peers and confirm_shared and not allowed:" in APP_SESSION)
check("  차단 시 큐·세션을 건드리지 않고 돌아간다(return 1)",
      "reason=\"shared_session_unverified\"" in APP_SESSION
      and "return 1" in APP_SESSION.split("if not allowed and not confirm_shared:")[1][:400])
check("_server_logout 은 손대지 않았다(쿠키 먼저 복원 후 로그아웃)",
      "저장 쿠키를 **지우기 전에** 해야 의미가 있다" in APP_SESSION
      and "NAVER_LOGOUT_URL" in APP_SESSION)
check("session_store.logout 은 이 계정만 지운다(기존 주석 유지)",
      "다른 계정은 건드리지 않는다" in
      (ROOT / "v2" / "session_store.py").read_text(encoding="utf-8"))
check("hard logout 은 여전히 별도 승인",
      "BLOG_ALLOW_HARD_LOGOUT" in APP_SESSION)

head("⑤ --confirm-shared 우회 — 영향받을 계정을 명확히 안내하는가")
blk = APP_SESSION.split("allowed, peers, why = login_state.logout_allowed(acc)")[1][:1800]
check("후보 **전체**를 한 줄씩 적는다(일부만 아님)",
      "for pr in peers:" in blk and "[:4]" not in blk)
check("  각 줄에 id·login_id·브랜드·사유를 적는다",
      all(k in blk for k in ("id={pr['id']}", "login_id=", "브랜드=",
                             "{pr['why']}")))
check("★각 줄에 **세션(쿠키) 보유 여부**를 적는다 — 실제로 끊기는 건 그쪽이다",
      "쿠키 {pr.get('cookies', 0)}개 저장됨" in blk
      and "이 PC 에 세션 없음" in blk)
check("  세션 있는 건수를 머리에 요약한다",
      "그중 {len(_live)}건은 이 PC 에 세션이 저장돼 있습니다" in blk)
check("★서버 로그아웃이 같은 네이버 계정 전체에 걸린다고 경고한다",
      "같은 네이버 계정** 전체의 세션을" in blk)
check("  쿠키 폴더 분리로는 막지 못한다고 적는다",
      "분리돼 있어도 이 영향은 막지 못합니다" in blk)
check("  차단 시 체크박스 안내를 그대로 한다",
      "공유 세션 확인" in APP_SESSION)
check("  진행하면 사용자가 확인했다고 기록한다",
      "사용자가 영향 범위를 확인했습니다" in APP_SESSION)

#  logout_allowed 의 사유 문구에도 세션 정보가 들어간다
give_session("b_other", 18, blog="myblog")
_, _, why_live = LS.logout_allowed(A, P)
check("★사유 문구에 '저장된 세션 쿠키 N개' 가 들어간다",
      "저장된 세션 쿠키 18개" in why_live, why_live[-90:])
check("  서버 로그아웃으로 함께 끊긴다고 적는다",
      "함께 끊깁니다" in why_live)
no_sess = LS.session_peers(A, P)
check("  후보에 has_session·cookies 가 담긴다",
      no_sess and no_sess[0]["has_session"] is True
      and no_sess[0]["cookies"] == 18, str(no_sess[0]))

head("⑥ 오탐·과도한 차단 — 실제 등록 모양으로 수치화")
#   ★운영 실측(2026-11-05) 모양을 **합성 이름**으로 재현한다. 실제 계정 이름·
#     blog_id 는 쓰지 않는다. 쿠키 수만 실측과 같게 둔다.
SHAPE = [
    #  (id, blog, login_id, 쿠키)
    ("s1", "blogA", "loginS1", 18),   # 등록 있음 · 세션 있음
    ("s2", "blogA", "", 18),          # 미등록 · 세션 있음   ← 실제 위험
    ("s3", "blogA", "", 0),
    ("s4", "blogA", "", 0),
    ("t1", "blogB", "", 19),          # 미등록 · 세션 있음   ← 실제 위험
    ("t2", "blogB", "", 17),          # 미등록 · 세션 있음
    ("t3", "blogB", "", 0),
    ("u1", "blogC", "", 0),
    ("u2", "blogC", "", 0),
    ("v1", "blogD", "loginV1", 13),
    ("v2", "blogD", "", 0),
    ("v3", "blogD", "", 0),
    ("w1", "blogE", "", 17),
    ("w2", "blogE", "", 0),
    ("w3", "blogE", "", 0),
    ("z1", "blogF", "loginZ1", 0),
    ("z2", "blogF", "", 0),
    ("solo", "blogG", "loginSolo", 5),  # 혼자 쓰는 블로그
]
rows = [acc(i, blog=b, login=l) for i, b, l, _ in SHAPE]
PS = write_accounts(rows)
for i, b, _l, ck in SHAPE:
    if ck:
        give_session(i, ck, blog=b)

print("%-8s%-10s%-12s%8s%8s%10s  %s"
      % ("id", "blog", "login_id", "쿠키", "후보수", "판정", "세션있는 후보"))
print("-" * 76)
blocked, allowed_n, live_block = 0, 0, 0
for a_row, (i, b, l, ck) in zip(rows, SHAPE):
    prs = LS.session_peers(a_row, PS)
    ok, _, _ = LS.logout_allowed(a_row, PS)
    live = [p for p in prs if p.get("has_session")]
    blocked += (not ok)
    allowed_n += bool(ok)
    live_block += (not ok and bool(live))
    print("%-8s%-10s%-12s%8d%8d%10s  %d건"
          % (i, b, l or "(없음)", ck, len(prs),
             "허용" if ok else "★차단", len(live)))
print("-" * 76)
print(f"  전체 {len(SHAPE)}건 — 허용 {allowed_n} · 차단 {blocked}"
      f" (그중 세션 있는 후보가 실제로 있는 경우 {live_block}건)")

check("혼자 쓰는 블로그는 후보가 없고 허용된다",
      LS.session_peers(rows[-1], PS) == []
      and LS.logout_allowed(rows[-1], PS)[0] is True)
check("★같은 blog_id 인데 모두 login_id 미등록이면 차단된다(의도된 보수적 동작)",
      LS.logout_allowed(rows[4], PS)[0] is False)
check("★표기가 같은 login_id 끼리는 허용된다(합치지 않고 비교만)",
      LS.logout_allowed(D1, PD)[0] is True)
check("차단이 전체의 과반이다(= login_id 등록 정리가 필요하다는 신호)",
      blocked > allowed_n, f"차단 {blocked} / 허용 {allowed_n}")
over = [i for a_row, (i, _b, _l, _c) in zip(rows, SHAPE)
        if not LS.logout_allowed(a_row, PS)[0]
        and not [p for p in LS.session_peers(a_row, PS) if p.get("has_session")]]
print(f"  ※ 세션 있는 후보가 **하나도 없는데** 차단된 경우: {len(over)}건 {over}")
check("★그 경우를 '과도한 차단 후보' 로 식별할 수 있다(has_session 으로 구분)",
      isinstance(over, list))
print("     → 이 PC 기준으로는 끊길 세션이 없다. 다만 **다른 PC** 에 세션이")
print("       있을 수 있어 자동으로 풀어 주지는 않았다(안전 우선 · 미적용).")

head("⑦ 기존 경로(profile_dir · login_id 별칭)가 그대로 동작하는가")
shared_dir = str(SANDBOX / "shared_profile")
H1 = acc("h1", blog="hb1", login="loginH1", profile=shared_dir)
H2 = acc("h2", blog="hb2", login="loginH2", profile=shared_dir)
PH = write_accounts([H1, H2])
ph = LS.session_peers(H1, PH)
check("① profile_dir 가 같으면 후보다(blog_id 가 달라도)",
      [p["id"] for p in ph] == ["h2"], str([p["id"] for p in ph]))
check("  사유가 profile_dir 로 적힌다",
      ph and "profile_dir" in ph[0]["why"], ph[0]["why"][:40] if ph else "")
check("  그때도 login_id 가 다르면 차단",
      LS.logout_allowed(H1, PH)[0] is False)
check("④가 ①②를 **덮어쓰지 않는다**(먼저 걸린 사유를 유지)",
      "if not why and mine_blog" in APP_LS)
check("별칭 경로 코드가 그대로 있다",
      "session_for_login(other.login_id) == me" in APP_LS)
check("목록을 못 읽으면 빈 목록(모른다 → 호출한 쪽이 판단)",
      LS.session_peers(A, Path(SANDBOX / "no_such.json")) == [])

print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
