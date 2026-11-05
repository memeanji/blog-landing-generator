r"""Stage 11 검증 — 발행 작업 범위를 account.id 로 고정 (설계 ④)
    (2026-11-05 · 브랜치 feature/agent-1.3.0-stabilization)

    .\.venv\Scripts\python.exe scripts\test_stage11_account_scope.py

★운영 구조 — 같은 네이버 로그인 ID 를 브랜드별로 나눠 **여러 account.id** 로
  등록해 쓴다(`…_re` / `…_dn`). 오류가 아니라 의도된 구조다.
      account.id = meanisji_re / login_id = meanisji
      account.id = meanisji_dn / login_id = meanisji

★그래서 발행 작업(조회·보류·재개)의 범위는 **account.id 하나**다.
  `login_id` 로 범위를 정하면
    · 다른 등록의 작업까지 끌어오고(과도)
    · login_id 등록 **전**에 만든 작업은 payload.login_id 가 비어 놓친다(누락)
  `payload.account` 는 발행·세션 작업 모두에 **항상** 채워지므로 둘 다 막힌다.

★브라우저·네이버·시트·운영 큐·운영 DB·운영 계정/세션을 건드리지 않는다.
  임시 폴더의 LocalStore 와 가짜 레코드만 쓴다. 실제 발행·보류·재개·로그아웃 없음.

검증 항목(사용자 요청 4단계 ①~⑦)
  ① 같은 login_id 를 쓰는 `_dn`/`_re` 등록의 작업이 **섞이지 않는다**
  ② login_id 등록 전후로 과거 작업 조회가 유지된다(누락 0)
  ③ 보류·재개가 다른 account.id 의 작업 상태를 바꾸지 않는다
  ④ 발행 직전 확인(logout_block)이 기존 보류 작업을 빠짐없이 본다
  ⑤ account.id 가 없으면 fail-safe 차단
  ⑥ brand·기준시트·발행 이력에 영향이 없다(접미사를 해석하지 않는다)
  ⑦ 공유 세션 보호(A안)는 login_id·blog_id 로 계속 동작한다(층 분리)
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="blog_stage11_"))
os.environ["BLOG_DATA_DIR"] = str(SANDBOX)
os.environ["BLOG_ACCOUNTS_REMOTE"] = "0"
os.environ["BLOG_QUEUE_BACKEND"] = "local"
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from v2 import login_state as LS                              # noqa: E402
from v2 import queue_store                                    # noqa: E402
from v2.accounts import Account                               # noqa: E402
from v2.job import Job                                        # noqa: E402
from v2.queue_store import HELD, LocalStore, PENDING, RUNNING  # noqa: E402

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


APP = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)
NS: dict = {"queue_store": queue_store, "time": __import__("time"), "chr": chr,
            "RUNNING_STATES": (PENDING, RUNNING)}
for _n in TREE.body:
    if isinstance(_n, ast.Assign) and len(_n.targets) == 1:
        _tg = _n.targets[0]
        _names = ([getattr(e, "id", "") for e in _tg.elts]
                  if isinstance(_tg, ast.Tuple) else [getattr(_tg, "id", "")])
        if _names and all(x.isupper() for x in _names if x):
            try:
                _vals = ast.literal_eval(_n.value)
            except Exception:                                  # noqa: BLE001
                continue
            if isinstance(_tg, ast.Tuple):
                _vals = _vals if isinstance(_vals, tuple) else (_vals,)
                for _k, _v in zip(_names, _vals):
                    NS[_k] = _v
            else:
                NS[_names[0]] = _vals
NS["QUEUE_READ_WAIT"] = 0.01


class FakeSt:
    def __init__(self):
        self.session_state = {}


NS["st"] = FakeSt()
for _n in TREE.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
            "is_logout_job", "begin_render", "render_scope", "render_seq",
            "query_deadline", "store_takes_deadline", "logout_state",
            "_logout_state_read", "logout_block"):
        exec(compile(ast.Module([_n], []), "streamlit_app.py", "exec"), NS)
begin_render = NS["begin_render"]
logout_state, logout_block = NS["logout_state"], NS["logout_block"]

#  ★운영 구조 — 같은 네이버 ID 를 두 등록으로
LOGIN = "meanisji"
RE_ID, DN_ID = "meanisji_re", "meanisji_dn"
OTHER_ID = "someoneelse_re"
PC = "PC1"


def new_store(name: str) -> LocalStore:
    return LocalStore(SANDBOX / name)


def put(store: LocalStore, acct: str, login: str, n: int = 1) -> list[str]:
    """그 등록 앞으로 대기 작업을 n건 넣는다."""
    out = []
    for i in range(n):
        out.append(store.submit(
            Job(brand="repurely", account=acct, login_id=login,
                flow="production", count=1), title=f"{acct}#{i}",
            target_agent=PC))
    return out


# ══════════════════════════════════════════════════════════════════
head("① 같은 login_id 를 쓰는 _dn / _re 등록의 작업이 섞이지 않는다")
s = new_store("q1")
re_jobs = put(s, RE_ID, LOGIN, 2)        # _re 의 작업 2건 (login_id 등록됨)
dn_jobs = put(s, DN_ID, LOGIN, 3)        # _dn 의 작업 3건 (같은 login_id)
oth_jobs = put(s, OTHER_ID, "zzz", 1)    # 남의 등록

by_re = s.list_by_status(PENDING, "", RE_ID)
by_dn = s.list_by_status(PENDING, "", DN_ID)
check("_re 조회는 _re 작업만", sorted(r["id"] for r in by_re) == sorted(re_jobs),
      f"{len(by_re)}건")
check("_dn 조회는 _dn 작업만", sorted(r["id"] for r in by_dn) == sorted(dn_jobs),
      f"{len(by_dn)}건")
check("★두 등록의 작업이 겹치지 않는다",
      not (set(r["id"] for r in by_re) & set(r["id"] for r in by_dn)))
check("남의 등록 작업은 어느 쪽에도 없다",
      not (set(oth_jobs) & set(r["id"] for r in by_re + by_dn)))
#  참고 — login_id 로 조회하면 둘이 섞인다(그래서 쓰지 않는다)
by_login = s.list_by_status(PENDING, LOGIN, "")
check("★(참고) login_id 로 조회하면 두 등록이 섞인다",
      len(by_login) == 5, f"{len(by_login)}건 = _re 2 + _dn 3")
check("  그래서 운영 코드는 account.id 로만 조회한다",
      'stt = logout_state(store_obj, "", aid)' in APP)

head("② login_id 등록 전후 — 과거 작업 조회가 유지된다(누락 0)")
s2 = new_store("q2")
old_jobs = put(s2, DN_ID, "", 2)         # 등록 **전**: payload.login_id 가 비었다
new_jobs = put(s2, DN_ID, LOGIN, 2)      # 등록 **후**
got = s2.list_by_status(PENDING, "", DN_ID)
check("★등록 전·후 작업을 **모두** 본다(account.id 로 조회)",
      sorted(r["id"] for r in got) == sorted(old_jobs + new_jobs),
      f"{len(got)}건 (옛 {len(old_jobs)} + 새 {len(new_jobs)})")
lost = s2.list_by_status(PENDING, LOGIN, "")
check("★(참고) login_id 로 조회하면 옛 작업을 놓친다",
      sorted(r["id"] for r in lost) == sorted(new_jobs),
      f"{len(lost)}건 — 옛 {len(old_jobs)}건 누락")
check("  payload.account 는 옛·새 작업 모두에 채워져 있다",
      all((r.get("job") or {}).get("account") == DN_ID for r in got))
check("  제출 코드가 account 를 항상 싣는다",
      APP.count("account=account.id,") >= 2,
      f"제출 지점 {APP.count('account=account.id,')}곳")

head("③ 보류·재개가 다른 account.id 를 건드리지 않는다")
s3 = new_store("q3")
re3 = put(s3, RE_ID, LOGIN, 2)
dn3 = put(s3, DN_ID, LOGIN, 3)
oth3 = put(s3, OTHER_ID, "zzz", 1)

held = s3.hold_account("", device=PC, account_id=DN_ID)
check("★_dn 만 보류된다", sorted(held) == sorted(dn3), f"{len(held)}건")
check("★_re 작업은 그대로 pending 이다",
      sorted(r["id"] for r in s3.list_by_status(PENDING, "", RE_ID))
      == sorted(re3))
check("  남의 등록도 그대로 pending 이다",
      [r["id"] for r in s3.list_by_status(PENDING, "", OTHER_ID)] == oth3)
check("  _dn 의 보류 건수가 맞다",
      len(s3.held_jobs("", DN_ID)) == 3, f"{len(s3.held_jobs('', DN_ID))}건")
check("  _re 는 보류 0건", s3.held_jobs("", RE_ID) == [])

back = s3.resume_account("", device=PC, account_id=DN_ID)
check("★재개도 _dn 만", sorted(back) == sorted(dn3), f"{len(back)}건")
check("  재개 후 _dn 이 pending 으로 돌아온다",
      sorted(r["id"] for r in s3.list_by_status(PENDING, "", DN_ID))
      == sorted(dn3))
check("  _re 는 처음부터 끝까지 변화가 없다",
      sorted(r["id"] for r in s3.list_by_status(PENDING, "", RE_ID))
      == sorted(re3))
#  참고 — login_id 로 보류하면 둘 다 잡힌다
s3b = new_store("q3b")
re3b, dn3b = put(s3b, RE_ID, LOGIN, 2), put(s3b, DN_ID, LOGIN, 3)
held_b = s3b.hold_account(LOGIN, device=PC)
check("★(참고) login_id 로 보류하면 두 등록이 **함께** 보류된다",
      len(held_b) == 5, f"{len(held_b)}건")
check("  그래서 운영 코드는 보류도 account.id 로만 한다",
      'do_logout(store_obj, brand, account, "", device_id,' in APP
      and 'do_resume(store_obj, "", device_id, account_id=aid)' in APP)

head("④ 발행 직전 확인 — 기존 보류 작업을 빠짐없이 본다")
s4 = new_store("q4")
old4 = put(s4, DN_ID, "", 1)             # 등록 전 작업
new4 = put(s4, DN_ID, LOGIN, 1)
s4.hold_account("", device=PC, account_id=DN_ID)
begin_render()
why = logout_block(s4, "", DN_ID, who="민지(프리즈젯)")
check("★보류가 있으면 발행을 막는다", "로그아웃 대기 상태입니다" in why,
      why[:52])
check("  보류 건수에 **옛 작업까지** 들어간다", "2건" in why, why[:70])
check("  표시 이름은 who 로 받는다(세션 폴더 이름을 노출하지 않는다)",
      "민지(프리즈젯)" in why and DN_ID not in why)
begin_render()
check("★다른 등록(_re)의 발행은 막히지 않는다",
      logout_block(s4, "", RE_ID, who="민지(리퓨어리)") == "")
begin_render()
check("  남의 등록도 막히지 않는다",
      logout_block(s4, "", OTHER_ID, who="남") == "")
check("제출 가드가 job.account 를 쓴다",
      'getattr(job, "account", "") or ""' in APP
      and 'who=getattr(job, "account_name", "") or ""' in APP)

head("⑤ account.id 가 없으면 fail-safe 차단")
begin_render()
no_id = logout_block(new_store("q5"), "", "", who="")
check("★식별자가 없으면 **막는다**", "새 작업을 만들지 않았습니다" in no_id,
      no_id[:52])
check("  이유를 적는다(account.id 가 비었다)",
      "계정 식별자(account.id)가 비어 있어" in no_id)
check("  다른 계정은 영향 없다고 적는다",
      "다른 계정의 발행은 영향받지 않습니다" in no_id)
begin_render()
check("  login_id 만 있어도(account.id 없음) 막는다",
      "새 작업을 만들지 않았습니다"
      in logout_block(new_store("q5b"), LOGIN, "", who=""))
check("조회 실패도 여전히 차단한다(기존 fail-safe 유지)",
      'if stt.get("read_error"):' in APP)

head("⑥ brand·기준시트·발행 이력 — 접미사를 해석하지 않는다")
check("접미사(_re·_dn)를 떼거나 해석하는 코드가 없다",
      '_re"' not in APP.replace('"_render', '"X')
      and ".rstrip(\"_dn\")" not in APP and 'replace("_dn"' not in APP)
check("brand 는 선택된 브랜드를 그대로 쓴다(Job.brand=brand.id)",
      "Job(flow=flow, brand=brand.id," in APP)
check("  기준시트 탭도 그대로 실어 보낸다(ref_tab)",
      "ref_tab=ref_tab," in APP)
check("  brand_config 를 함께 보낸다(화면과 같은 시트)",
      "brand_config=brands.config_json()," in APP)
s6 = new_store("q6")
j_re = put(s6, RE_ID, LOGIN, 1)[0]
rec = s6.get(j_re)
check("작업에 brand·account·login_id 가 모두 남는다(이력 유지)",
      (rec.get("brand") or (rec.get("job") or {}).get("brand")) == "repurely"
      and (rec.get("job") or {}).get("account") == RE_ID
      and (rec.get("job") or {}).get("login_id") == LOGIN,
      str({k: (rec.get("job") or {}).get(k)
           for k in ("account", "login_id", "brand")}))

head("⑦ 공유 세션 보호(A안)는 login_id·blog_id 로 계속 동작한다")


def acc(ident, blog="", login=""):
    return Account(id=ident, label=ident, blog_id=blog, login_id=login)


tmp = Path(tempfile.mkdtemp(prefix="acc11_")) / "accounts.json"
rows = [acc(RE_ID, blog="meanblog", login=LOGIN),
        acc(DN_ID, blog="meanblog", login=LOGIN),
        acc(OTHER_ID, blog="otherblog", login="zzz")]
tmp.write_text(json.dumps({"accounts": [r.to_dict() for r in rows]},
                          ensure_ascii=False), encoding="utf-8")
peers = LS.session_peers(rows[0], tmp)
check("★같은 네이버 ID 의 다른 등록을 공유 후보로 **본다**",
      [p["id"] for p in peers] == [DN_ID], str([p["id"] for p in peers]))
ok_s, _p, why_s = LS.logout_allowed(rows[0], tmp)
check("  login_id 가 같으므로 로그아웃은 **허용**된다", ok_s is True, str(ok_s))
check("  그래도 영향 범위를 알려 준다",
      "함께 재로그인이 필요해집니다" in why_s, why_s[:60])
check("  다른 네이버 ID 의 등록은 후보가 아니다",
      LS.session_peers(rows[2], tmp) == [])
print("""
   → 발행 작업은 account.id 로 **분리**되고, 공유 세션·서버 로그아웃 영향은
     login_id 로 **함께** 본다. 두 층이 서로 간섭하지 않는다.""")

print("\n" + "=" * 76)
if fails:
    print(f" 실패 {len(fails)}건: {fails}")
else:
    print(" 전부 통과")
print(f" 임시 폴더: {SANDBOX}")
print("=" * 76)
sys.exit(1 if fails else 0)
