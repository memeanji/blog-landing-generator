r"""1단계 회귀 테스트 — 큐 연결 안정화 · 제출 실패 안내 · [연결 다시 맺기].

    .\.venv\Scripts\python.exe scripts\test_stage1_queue.py          # 전부
    .\.venv\Scripts\python.exe scripts\test_stage1_queue.py --no-app # 화면 테스트 생략

브라우저·네이버·운영 큐를 건드리지 않는다.
  · 작업 제출 테스트는 **임시 폴더 큐**(`LocalStore(root=tmp)`)에만 넣는다.
  · 원격 큐 테스트는 `127.0.0.1:9`(연결 거부)로만 — 외부로 나가지 않는다.
  · 화면 테스트(AppTest)는 **제출 버튼을 누르지 않는다.** 누르는 것은
    [연결 다시 맺기] 뿐이고, 전후로 운영 큐 pending 파일 수가 같은지 확인한다.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from v2 import queue_store, timeutil                           # noqa: E402
from v2.job import Job                                         # noqa: E402
from v2.queue_store import LocalStore, QueueBackendError        # noqa: E402

OK, NG = "  ✅", "  ❌"
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print((OK if cond else NG) + f" {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def with_env(**kw):
    """환경변수를 바꿔 끼우는 컨텍스트(테스트가 서로 안 섞이게)."""
    class _Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in kw}
            for k, v in kw.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *_):
            for k, v in self.old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _Ctx()


# ══════════════════════════════════════════════════════════════════
print("1) 백엔드 이름 해석 — 기존 기본값 보존")
with with_env(BLOG_QUEUE_BACKEND=None):
    check("설정 없음 → local", queue_store.configured_backend() == "local")
    check("설정 없음 → 원격 아님", queue_store.wants_remote() is False)
for name, want, remote in (("local", "local", False), ("file", "local", False),
                           ("LOCAL", "local", False), ("supabase", "supabase", True),
                           ("remote", "remote", True), (" Supabase ", "supabase", True)):
    check(f"{name!r} → {want} (원격={remote})",
          queue_store.configured_backend(name) == want
          and queue_store.wants_remote(name) is remote)

# ══════════════════════════════════════════════════════════════════
print()
print("2) local 백엔드 — 예전 그대로")
with with_env(BLOG_QUEUE_BACKEND="local"):
    st_local = queue_store.get_store()
    info = queue_store.backend_info()
check("local → LocalStore", type(st_local).__name__ == "LocalStore")
check("폴백 표시 없음", info.get("fallback") is False, str(info))
check("backend_info 에 요청/종류/시각", bool(info.get("requested"))
      and bool(info.get("kind")) and bool(info.get("at")), str(info))
check("마지막 통신 시각이 찍힌다", bool(st_local.last_contact()),
      st_local.last_contact())
check("describe_backend 모양", set(st_local.describe_backend())
      == {"kind", "mode", "last_contact"})
check("probe 기본값은 통과", st_local.probe() is True)

# ══════════════════════════════════════════════════════════════════
print()
print("3) 원격 설정인데 못 붙는 경우 — strict 와 기존 동작")
BAD = {"BLOG_QUEUE_BACKEND": "supabase", "SUPABASE_URL": "",
       "SUPABASE_SERVICE_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": "",
       "SUPABASE_PUBLISHABLE_KEY": "", "SUPABASE_ANON_KEY": "",
       "BLOG_DEVICE_TOKEN": ""}
with with_env(**BAD):
    # (1) 기본(strict=False) = Agent·CLI 경로 → 예전처럼 로컬 큐로 내려간다
    st_fb = queue_store.get_store()
    info_fb = queue_store.backend_info()
    check("설정 없음 + strict=False → LocalStore(기존 동작 보존)",
          type(st_fb).__name__ == "LocalStore")
    check("폴백 사실이 기록된다", info_fb.get("fallback") is True, str(info_fb))
    check("폴백 원인이 남는다", "Supabase" in (info_fb.get("reason") or "")
          or bool(info_fb.get("reason")), str(info_fb.get("reason"))[:80])

    # (2) strict=True = 화면 경로 → 조용히 내려가지 않는다
    try:
        queue_store.get_store(strict=True)
        check("설정 없음 + strict=True → 예외", False, "예외가 안 났다")
    except QueueBackendError as exc:
        check("설정 없음 + strict=True → QueueBackendError", True)
        check("오류 문구가 '보지 못합니다' 를 알려 준다",
              "보지 못" in str(exc), str(exc).splitlines()[0][:60])

# (3) 설정은 맞지만 **키가 틀린** 경우 — 객체 생성은 되고 probe 에서 걸린다
with with_env(BLOG_QUEUE_BACKEND="supabase",
              SUPABASE_URL="http://127.0.0.1:9",
              SUPABASE_SERVICE_KEY="bogus-for-test",
              BLOG_DEVICE_TOKEN=""):
    try:
        queue_store.get_store(strict=True)
        check("붙을 수 없는 주소 + strict → 예외", False, "예외가 안 났다")
    except QueueBackendError:
        check("붙을 수 없는 주소 + strict → QueueBackendError (probe 가 걸러낸다)", True)
    except Exception as exc:                                   # noqa: BLE001
        check("붙을 수 없는 주소 + strict → QueueBackendError",
              False, f"{type(exc).__name__}: {exc}")
    # ★strict=False(Agent·CLI)는 **probe 를 하지 않는다** = 예전과 완전히 동일하다.
    #   설정값이 있으면 SupabaseStore 를 그대로 쓰고, 키가 틀린 건 첫 호출에서 드러난다.
    #   (Agent 동작을 바꾸지 않기 위한 의도된 차이)
    st_fb2 = queue_store.get_store()                           # strict=False
    check("같은 상황에서 strict=False 는 예전처럼 SupabaseStore (동작 보존)",
          type(st_fb2).__name__ == "SupabaseStore", type(st_fb2).__name__)
    check("strict=False 는 probe 를 하지 않아 '통신한 적 없음' 으로 남는다",
          st_fb2.last_contact() == "", st_fb2.last_contact())

# ══════════════════════════════════════════════════════════════════
print()
print("4) 제출 확인 — 실패를 성공처럼 보이지 않게")
tmp = Path(tempfile.mkdtemp(prefix="blog_q_test_"))
sandbox = LocalStore(root=tmp)
job = Job(brand="repurely", media="GFA", deficiency="테스트")

r1 = queue_store.submit_checked(sandbox, job, title="테스트", label="테스트 작업")
check("정상 제출 → 등록 성공 확인", r1.ok and bool(r1.job_id), repr(r1))
check("멱등성 키가 함께 남는다", bool(r1.client_key))
check("제출된 작업을 큐에서 다시 읽을 수 있다", bool(sandbox.get(r1.job_id)))
check("멱등성 키로도 같은 작업을 찾는다",
      (sandbox.find_by_client_key(r1.client_key) or {}).get("id") == r1.job_id)
check("임시 폴더에만 들어갔다", (tmp / "pending").exists()
      and len(list((tmp / "pending").glob("*.json"))) == 1)


class RaisingStore(LocalStore):
    """제출 자체가 **확실히** 실패하는 저장소(작업이 생기지 않는다)."""

    def submit(self, *a, **kw):
        raise RuntimeError("연결 끊김(테스트)")


class LostReplyStore(LocalStore):
    """서버에는 작업이 생겼는데 **응답만 유실**된 상황."""

    def submit(self, *a, **kw):
        super().submit(*a, **kw)           # 실제로 큐에 들어간다
        raise RuntimeError("응답 유실(테스트)")


class BlindStore(LocalStore):
    """제출은 실패했고 **조회도 못 하는** 상황 = 확인 불가."""

    def submit(self, *a, **kw):
        raise RuntimeError("연결 끊김(테스트)")

    def find_by_client_key(self, client_key: str):
        raise RuntimeError("조회 불가(테스트)")


class VanishingStore(LocalStore):
    """넣었다고 하는데 `get` 으로 다시 못 찾는 상황."""

    def get(self, job_id: str):
        return None


# ① 실패 확정 — 조회로 '없음' 을 확인했다
r2 = queue_store.submit_checked(RaisingStore(root=tmp), job, label="실전 실행 작업")
check("제출 실패 + 조회로 없음 확인 → 실패 확정",
      r2.status == queue_store.SUBMIT_FAILED and r2.job_id == "", repr(r2))
check("실패 문구에 '실행되지 않았' 안내", "실행되지 않았" in r2.message)
check("실패 문구에 원인 포함", "연결 끊김(테스트)" in r2.message)

# ② 응답만 유실 — **재제출 금지**, 조회로 되찾는다
tmp_lost = Path(tempfile.mkdtemp(prefix="blog_q_lost_"))
lost = LostReplyStore(root=tmp_lost)
r3 = queue_store.submit_checked(lost, job, label="실전 실행 작업")
check("응답만 유실된 경우 → **성공으로 되찾는다**",
      r3.ok and bool(r3.job_id), repr(r3))
check("되찾았다는 사실을 알려 준다", "응답만 유실" in (r3.message or ""),
      r3.message)
check("작업이 **한 건만** 들어갔다(중복 제출 없음)",
      len(list((tmp_lost / "pending").glob("*.json"))) == 1,
      str(len(list((tmp_lost / "pending").glob("*.json")))))

# ③ 확인 불가 — 실패로 단정하지 않는다
r4 = queue_store.submit_checked(BlindStore(root=tmp), job, label="Dry-run 작업")
check("제출 실패 + 조회 실패 → **확인 불가**",
      r4.status == queue_store.SUBMIT_UNKNOWN, repr(r4))
check("확인 불가 문구는 '다시 넣지 마세요' 로 안내",
      "다시 넣지 마세요" in r4.message, r4.message.splitlines()[0])
check("확인 불가에도 멱등성 키를 돌려준다(나중에 조회용)", bool(r4.client_key))
check("세 상태의 문구가 서로 다르다",
      len({r2.message, r3.message or "", r4.message}) == 3)

# ④ get 으로 못 찾아도 키로 찾히면 성공
r5 = queue_store.submit_checked(VanishingStore(root=tmp), job, label="Dry-run 작업")
check("get 이 못 찾아도 멱등성 키로 찾히면 성공", r5.ok, repr(r5))

# ⑤ 옛 저장소(멱등성 키를 모르는 구현)와의 호환
class OldStore(LocalStore):
    def submit(self, job, *, kind="run", title="", created_by="",
               target_agent=""):          # client_key 를 모른다
        return super().submit(job, kind=kind, title=title,
                              created_by=created_by, target_agent=target_agent)


r6 = queue_store.submit_checked(OldStore(root=tmp), job, label="옛 저장소 작업")
check("client_key 를 모르는 저장소에서도 제출된다(호환)", r6.ok, repr(r6))

# ⑥ 조회 자체의 두 결과를 구분한다
rec_none, why_none = queue_store.find_job_by_client_key(sandbox, "없는키")
check("'확실히 없음' 은 (None, 빈 사유)", rec_none is None and why_none == "")
rec_x, why_x = queue_store.find_job_by_client_key(BlindStore(root=tmp), "아무키")
check("'확인 못 함' 은 (None, 사유 있음)", rec_x is None and bool(why_x), why_x)

# ── ⑦ 같은 멱등성 키로 다시 부르면 **또 넣지 않는다** ───────────────
tmp_same = Path(tempfile.mkdtemp(prefix="blog_q_same_"))
same = LocalStore(root=tmp_same)
KEY = "fixed-key-0001"
a1 = queue_store.submit_checked(same, job, label="작업", client_key=KEY)
a2 = queue_store.submit_checked(same, job, label="작업", client_key=KEY)
a3 = queue_store.submit_checked(same, job, label="작업", client_key=KEY)
check("같은 키로 3번 불러도 모두 성공으로 답한다",
      a1.ok and a2.ok and a3.ok, f"{a1.status}/{a2.status}/{a3.status}")
check("같은 키로 3번 불러도 **작업은 한 건**",
      len(list((tmp_same / "pending").glob("*.json"))) == 1,
      str(len(list((tmp_same / "pending").glob("*.json")))))
check("두 번째부터는 같은 작업 번호를 돌려준다",
      a1.job_id == a2.job_id == a3.job_id, f"{a1.job_id} {a2.job_id}")
check("'다시 넣지 않았습니다' 를 알려 준다", "다시 넣지 않았" in (a2.message or ""),
      a2.message)

# ── ⑧ 늦게 처리될 수 있는 실패는 **실패로 단정하지 않는다** ──────────
from v2.supabase_store import SupabaseError                    # noqa: E402


class FlakyStore(LocalStore):
    """원격처럼 행동하는 저장소 — 예외 종류로 '확실한 실패' 를 구분한다."""

    is_local = False                       # ★원격처럼 — 떠 있는 요청이 있을 수 있다

    def __init__(self, root, exc) -> None:
        super().__init__(root=root)
        self._exc = exc

    def submit(self, *a, **kw):
        raise self._exc

    # 조회는 되지만 아직 아무것도 안 보이는 상태
    def find_by_client_key(self, client_key: str):
        return None


tmp_flaky = Path(tempfile.mkdtemp(prefix="blog_q_flaky_"))
r_timeout = queue_store.submit_checked(
    FlakyStore(tmp_flaky, TimeoutError("응답 시간 초과(테스트)")), job,
    label="실전 실행 작업")
check("타임아웃 + 조회에 안 보임 → **확인 불가**(실패 단정 금지)",
      r_timeout.status == queue_store.SUBMIT_UNKNOWN, repr(r_timeout))
check("문구가 '등록 실패로 단정하지 않습니다' 라고 알려 준다",
      "단정하지 않습니다" in r_timeout.message,
      r_timeout.message.splitlines()[0])

r_5xx = queue_store.submit_checked(
    FlakyStore(tmp_flaky, SupabaseError("POST jobs 실패(503): busy", status=503)),
    job, label="실전 실행 작업")
check("5xx + 조회에 안 보임 → 확인 불가",
      r_5xx.status == queue_store.SUBMIT_UNKNOWN, repr(r_5xx))

r_4xx = queue_store.submit_checked(
    FlakyStore(tmp_flaky, SupabaseError("POST jobs 실패(400): bad", status=400,
                                        definite_failure=True)),
    job, label="실전 실행 작업")
check("4xx(서버가 거부) + 조회에 안 보임 → **실패 확정**",
      r_4xx.status == queue_store.SUBMIT_FAILED, repr(r_4xx))

r_pre = queue_store.submit_checked(
    FlakyStore(tmp_flaky, SupabaseError("실행할 PC(device_id)가 지정되지 않았습니다.",
                                        definite_failure=True)),
    job, label="실전 실행 작업")
check("보내기 전에 막힌 경우 → 실패 확정",
      r_pre.status == queue_store.SUBMIT_FAILED, repr(r_pre))
check("확인 불가에는 멱등성 키가 남아 조회할 수 있다",
      bool(r_timeout.client_key) and bool(r_5xx.client_key))

# ── ⑨ UNIQUE 인덱스가 걸린 뒤의 동작(409 = 이미 있음) ─────────────
#   ★`supabase/schema_jobs_idempotency.sql` 을 적용하면 같은 키의 두 번째 INSERT 가
#     409 로 거부된다. 그때 우리 코드가 **기존 작업을 되찾아 성공으로 답하는지** 본다.
tmp_409 = Path(tempfile.mkdtemp(prefix="blog_q_409_"))
existing = LocalStore(root=tmp_409)
DUP_KEY = "dup-key-409"
first = queue_store.submit_checked(existing, job, label="작업", client_key=DUP_KEY)


class ConflictStore(LocalStore):
    """UNIQUE 위반(409)을 돌려주는 원격 저장소 흉내 — 조회는 기존 행을 찾는다."""

    is_local = False

    def submit(self, *a, **kw):
        raise SupabaseError("POST jobs 실패(409): duplicate key value violates "
                            "unique constraint \"jobs_client_key_uniq\"",
                            status=409, definite_failure=True)

    def find_by_client_key(self, client_key: str):
        return existing.find_by_client_key(client_key)


r_409 = queue_store.submit_checked(ConflictStore(root=tmp_409), job,
                                   label="실전 실행 작업", client_key=DUP_KEY)
check("UNIQUE 위반(409) → 기존 작업을 되찾아 성공",
      r_409.ok and r_409.job_id == first.job_id, repr(r_409))
check("409 상황에서 작업이 늘지 않는다",
      len(list((tmp_409 / "pending").glob("*.json"))) == 1,
      str(len(list((tmp_409 / "pending").glob("*.json")))))

# ── 옛 Agent 호환 — payload 에 멱등성 키가 끼어도 괜찮은가 ──────────
#   ★설치본 Agent 는 자기 안에 얼린 `Job` 을 쓴다. 모르는 키를 만나면
#     `Job.from_dict` 가 **버리는지** 확인한다(터지면 발행이 멈춘다).
mixed = {**job.to_dict(), "client_key": "abc123", "모르는키": 1}
try:
    revived = Job.from_dict(mixed)
    check("옛 Agent 의 Job.from_dict 가 모르는 키를 버린다",
          revived.media == "GFA" and not hasattr(revived, "client_key"))
except Exception as exc:                                       # noqa: BLE001
    check("옛 Agent 의 Job.from_dict 가 모르는 키를 버린다", False,
          f"{type(exc).__name__}: {exc}")
check("to_dict 에는 멱등성 키가 들어가지 않는다(Job 자체는 무변경)",
      "client_key" not in job.to_dict())

# ── Agent 가 그 작업을 그대로 받아 갈 수 있는지(수신 회귀) ──────────
#   ★앞 단계에서 '확인 불가' 를 흉내낸 가짜 store 도 실제로는 한 건을 넣었다.
#     수신 테스트는 섞이지 않게 **새 임시 큐**에서 한다.
tmp2 = Path(tempfile.mkdtemp(prefix="blog_q_test2_"))
sandbox = LocalStore(root=tmp2)
_r = queue_store.submit_checked(sandbox, job, title="수신 테스트",
                                label="수신 테스트 작업")
jid = _r.job_id
check("수신 테스트용 작업 제출", _r.ok and bool(jid), repr(_r))
claimed = sandbox.claim("test-agent")
check("Agent 가 작업을 집어 간다(claim)",
      claimed is not None and claimed.get("id") == jid,
      str(claimed and claimed.get("id")))
check("집어 간 작업에 Job 내용이 그대로 있다",
      bool(claimed) and (claimed.get("job") or {}).get("media") == "GFA",
      str((claimed or {}).get("job", {}).get("media")))
sandbox.append_log(jid, "[agent] 테스트 로그")
sandbox.append_event(jid, {"event": "run_started"})
sandbox.finish(jid, queue_store.DONE, 0)
check("끝난 작업은 done 으로 남는다",
      (sandbox.get(jid) or {}).get("status") == queue_store.DONE)
log_text, _ = sandbox.read_log(jid)
check("Agent 로그를 다시 읽을 수 있다", "테스트 로그" in log_text)
check("더 집을 작업이 없으면 None", sandbox.claim("test-agent") is None)

# ══════════════════════════════════════════════════════════════════
print()
print("5) 시각 표기 — 저장은 UTC, 표시는 KST")
check("tz 없는 PC 로그 시각은 KST 로 본다",
      timeutil.fmt("2026-10-30T13:14:03", "%H:%M:%S", "") == "13:14:03")
check("UTC(+00:00) → KST(+9)",
      timeutil.fmt("2026-10-30T04:14:03+00:00", "%H:%M:%S", "") == "13:14:03")
check("…Z 형식도 같다",
      timeutil.fmt("2026-10-30T04:14:03Z", "%H:%M:%S", "") == "13:14:03")
check("KST 꼬리말이 붙는다", timeutil.fmt("2026-10-30T04:14:03Z").endswith(" KST"))
check("빈 값은 빈 문자열", timeutil.fmt("") == "" and timeutil.fmt(None) == "")
check("읽을 수 없는 값은 원문 보존", timeutil.fmt("언제인지 모름") == "언제인지 모름")
_now = timeutil.now_kst()
check("now_kst 는 +09:00",
      _now.utcoffset() is not None and _now.utcoffset().total_seconds() == 32400)
check("ago 가 사람 말로 나온다",
      timeutil.ago((_now.replace(microsecond=0)).isoformat()) in ("방금", "0초 전"),
      timeutil.ago(_now.isoformat()))

# ══════════════════════════════════════════════════════════════════
if "--no-app" not in sys.argv:
    print()
    print("6) 화면(AppTest) — 연결 상태 패널 · [연결 다시 맺기]")
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    PENDING = ROOT / "queue" / "pending"
    before = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0

    def open_app(timeout: int = 90):
        a = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=timeout)
        a.session_state["authed"] = True       # 비밀번호 게이트 건너뛰기
        a.run()
        return a

    st.cache_resource.clear()
    app = open_app()
    labels = [b.label for b in app.button]
    heads = [h.value for h in app.header] if hasattr(app, "header") else []
    check("앱이 예외 없이 뜬다", not app.exception, str(app.exception)[:120])
    check("사이드바에 '연결 상태' 패널", "연결 상태" in heads, str(heads))
    check("[연결 다시 맺기] 버튼이 있다", "연결 다시 맺기" in labels, str(labels)[:160])
    panel = " ".join(str(m.value) for m in app.markdown)
    check("큐 마지막 통신 시각을 보여 준다", "큐 마지막 통신" in panel)
    check("다시 맺는 범위를 적어 둔다",
          any("다시 맺는 범위" in str(c.value) for c in app.caption))
    oks = " ".join(str(s.value) for s in app.success)
    warns = " ".join(str(s.value) for s in app.warning) + \
            " ".join(str(s.value) for s in app.error)
    check("로컬 실행에서는 '로컬 큐' 로 안내", "로컬 큐" in (oks + warns), (oks + warns)[:120])

    [b for b in app.button if b.label == "연결 다시 맺기"][0].click().run()
    check("[연결 다시 맺기] 후에도 앱이 정상", not app.exception, str(app.exception)[:120])
    after = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0
    check("다시 맺기가 작업을 새로 넣지 않는다", after == before, f"{before} → {after}")
    check("다시 맺기가 기존 작업을 지우지 않는다", after >= before)

    # ── '등록 여부 확인 불가' 가 남아 있을 때의 화면 ─────────────────
    st.cache_resource.clear()
    app3 = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=90)
    app3.session_state["authed"] = True
    app3.session_state["_pending_submit"] = {
        "key": "testkey-not-registered", "label": "실전 실행 작업",
        "at": timeutil.now_kst().isoformat(timespec="seconds")}
    app3.run()
    warn3 = " ".join(str(w.value) for w in app3.warning)
    labels3 = [b.label for b in app3.button]
    check("확인 불가 상태를 화면에 띄운다", "등록 여부가 확인되지 않았습니다" in warn3,
          warn3[:100])
    check("[등록 여부 다시 확인] 버튼이 생긴다", "등록 여부 다시 확인" in labels3,
          str(labels3)[:140])
    before3 = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0
    [b for b in app3.button if b.label == "등록 여부 다시 확인"][0].click().run()
    after3 = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0
    check("확인 버튼은 작업을 새로 넣지 않는다", after3 == before3,
          f"{before3} → {after3}")
    info3 = " ".join(str(i.value) for i in app3.info)
    check("없으면 '등록되지 않았다' 고 알려 준다", "등록되지 않은 것으로 확인" in info3,
          info3[:120])
    try:
        left = bool(app3.session_state["_pending_submit"])
    except (KeyError, AttributeError):
        left = False
    check("확인이 끝나면 경고를 지운다", left is False)

    # ── 새로고침(세션 초기화) 후에도 멱등성 키가 살아 있는가 ──────────
    st.cache_resource.clear()
    app4 = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=90)
    app4.session_state["authed"] = True
    app4.query_params["pending"] = "survives-refresh-0001"     # 주소에만 남은 상태
    app4.run()
    warn4 = " ".join(str(w.value) for w in app4.warning)
    labels4 = [b.label for b in app4.button]
    check("세션이 비어도 주소의 키로 경고를 되살린다",
          "등록 여부가 확인되지 않았습니다" in warn4, warn4[:90])
    check("새로고침 전 제출이라고 알려 준다", "새로고침 전에 제출한 작업" in warn4)
    check("되살린 상태에서도 [등록 여부 다시 확인] 이 있다",
          "등록 여부 다시 확인" in labels4, str(labels4)[:120])
    def qp(app, name):
        got = app.query_params.get(name)
        if isinstance(got, (list, tuple)):
            got = got[0] if got else ""
        return str(got or "")

    check("주소에 키가 유지된다", qp(app4, "pending") == "survives-refresh-0001",
          qp(app4, "pending"))
    before4 = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0
    [b for b in app4.button if b.label == "등록 여부 다시 확인"][0].click().run()
    after4 = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0
    check("되살린 뒤 확인해도 작업을 넣지 않는다", after4 == before4,
          f"{before4} → {after4}")
    check("확인이 끝나면 주소에서도 지운다", qp(app4, "pending") == "",
          qp(app4, "pending"))

    # ── 원격 큐에 못 붙는 상황 — 조용한 폴백이 아니라 화면이 멈춰야 한다
    st.cache_resource.clear()
    real_get = queue_store.get_store

    def fake_get(backend: str = "", strict: bool = False):
        if strict:
            raise QueueBackendError("테스트: 원격 큐 연결 실패")
        return real_get(backend)

    queue_store.get_store = fake_get                           # type: ignore[assignment]
    env = with_env(BLOG_QUEUE_BACKEND="supabase")              # 원격을 쓰려는 설정
    env.__enter__()
    try:
        app2 = open_app()
        errs = " ".join(str(e.value) for e in app2.error)
        labels2 = [b.label for b in app2.button]
        check("연결 실패 시 빨간 안내가 뜬다", "연결하지 못했습니다" in errs,
              errs[:120])
        check("'PC 가 보지 못합니다' 를 알려 준다", "보지 못합니다" in errs)
        check("원인을 그대로 보여 준다", "테스트: 원격 큐 연결 실패" in errs)
        check("그 화면에서도 [연결 다시 맺기] 를 누를 수 있다",
              "연결 다시 맺기" in labels2, str(labels2)[:120])
        check("실행 버튼은 열지 않는다(멈춤)",
              "실전 실행" not in labels2 and "Dry-run (발행 없음)" not in labels2,
              str(labels2)[:120])
        after2 = len(list(PENDING.glob("*.json"))) if PENDING.exists() else 0
        check("연결 실패 상황에서도 큐가 변하지 않는다", after2 == before,
              f"{before} → {after2}")
    finally:
        env.__exit__(None, None, None)
        queue_store.get_store = real_get                       # type: ignore[assignment]
        st.cache_resource.clear()
else:
    print()
    print("6) 화면(AppTest) — 생략(--no-app)")

# ══════════════════════════════════════════════════════════════════
print()
if fails:
    print(f"❌ 실패 {len(fails)}건: " + " / ".join(fails))
    sys.exit(1)
print("✅ 전부 통과")
