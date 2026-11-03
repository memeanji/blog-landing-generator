r"""운영 Supabase **읽기 전용** 사전 확인 (2026-11-03).

    .\.venv\Scripts\python.exe scripts\verify_supabase_readonly.py

확인하는 것 — 전부 GET 한 번씩. **쓰기·DDL·작업 집기(claim) 없음.**
    ① 연결되는가(어떤 키로 붙었는가)
    ② `jobs.payload->>client_key` 중복이 있는가            (UNIQUE 인덱스 사전 조건)
    ③ `client_key` 가 들어 있는 행이 몇 건인가             (인덱스 대상 행 수)
    ④ `jobs.status` 에 지금까지 어떤 값이 쓰였는가         (held 를 쓸 수 있는지 단서)
    ⑤ `status=eq.held` 조회가 되는가                       (컬럼 타입/필터 확인)

★키·토큰·쿠키 값은 **절대 출력하지 않는다**(어떤 종류의 키인지만 표시).
★실패해도 아무것도 바꾸지 않는다 — '확인 불가' 로 보고하고 끝낸다.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

import requests                                                # noqa: E402

try:                                                           # .env 읽기
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEYS = [
    ("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_SERVICE_KEY") or ""),
    ("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""),
    ("SUPABASE_PUBLISHABLE_KEY", os.getenv("SUPABASE_PUBLISHABLE_KEY") or ""),
    ("SUPABASE_ANON_KEY", os.getenv("SUPABASE_ANON_KEY") or ""),
]
KEYS = [(n, v) for n, v in KEYS if v.strip()]

print("=" * 74)
print(" 운영 Supabase 읽기 전용 사전 확인 (쓰기·DDL 없음)")
print("=" * 74)
if not URL:
    print("  [!] SUPABASE_URL 이 없습니다 — 확인 불가")
    raise SystemExit(2)
print(f"  대상: {URL.split('//')[-1].split('.')[0]}….supabase.co  (프로젝트 이름만 표시)")
print(f"  쓸 수 있는 키: {[n for n, _ in KEYS] or '(없음)'}")
if not KEYS:
    print("  [!] 이 PC 에 읽기용 키가 없습니다 — 확인 불가")
    raise SystemExit(2)


def get(key: str, path: str, prefer: str = ""):
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if prefer:
        h["Prefer"] = prefer
    try:
        r = requests.get(f"{URL}/rest/v1/{path}", headers=h, timeout=20)
    except Exception as exc:                                   # noqa: BLE001
        return None, f"요청 실패({type(exc).__name__})"
    if not r.ok:
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    try:
        return r.json(), ""
    except Exception:                                          # noqa: BLE001
        return None, "응답을 읽지 못했습니다"


name, key = "", ""
for n, v in KEYS:                                 # 읽을 수 있는 키를 찾는다
    rows, why = get(v, "jobs?select=job_id&limit=1")
    print("")
    print(f"  ① {n} 으로 jobs 읽기 → "
          + ("가능" if rows is not None else f"불가 ({why})"))
    if rows is not None:
        name, key = n, v
        break

if not key:
    print("")
    print("  [결론] 이 PC 의 키로는 jobs 를 읽을 수 없습니다(RLS 로 막혀 있음).")
    print("         서비스 키는 Streamlit Cloud 비밀값에만 있습니다 —")
    print("         아래 SQL 을 Supabase SQL 편집기에서 **읽기 전용**으로 돌려 주세요:")
    print("")
    print("    -- ② client_key 중복 (0건이어야 인덱스 생성 가능)")
    print("    select payload->>'client_key' as k, count(*)")
    print("      from public.jobs where payload ? 'client_key'")
    print("     group by 1 having count(*) > 1;")
    print("")
    print("    -- ③ 인덱스 대상 행 수")
    print("    select count(*) from public.jobs where payload ? 'client_key';")
    print("")
    print("    -- ④ status 에 CHECK 제약이 있는가(held 를 쓸 수 있는지)")
    print("    select c.conname, pg_get_constraintdef(c.oid)")
    print("      from pg_constraint c join pg_class t on t.oid = c.conrelid")
    print("     where t.relname = 'jobs' and c.contype = 'c';")
    print("")
    print("    -- ⑤ status 컬럼 타입(enum 이면 held 추가가 필요하다)")
    print("    select column_name, data_type, udt_name")
    print("      from information_schema.columns")
    print("     where table_name = 'jobs' and column_name = 'status';")
    raise SystemExit(3)

print("")
print(f"  ② {name} 으로 client_key 중복 확인")
rows, why = get(key, "jobs?select=payload&limit=10000")
if rows is None:
    print(f"      확인 불가 — {why}")
    raise SystemExit(3)
keys = [str((r.get("payload") or {}).get("client_key") or "")
        for r in rows if isinstance(r.get("payload"), dict)]
keys = [k for k in keys if k]
dup = {k: n for k, n in Counter(keys).items() if n > 1}
print(f"      전체 작업 {len(rows)}건 · client_key 있는 행 {len(keys)}건 · "
      f"중복 {len(dup)}종")
print("      → " + ("중복 없음 ✅ UNIQUE 인덱스 생성 가능"
                    if not dup else f"중복 있음 ⚠ {list(dup)[:3]}"))

print("")
print("  ④ status 에 실제로 쓰인 값")
rows, why = get(key, "jobs?select=status&limit=10000")
if rows is None:
    print(f"      확인 불가 — {why}")
else:
    cnt = Counter(str(r.get("status") or "") for r in rows)
    for k, n in cnt.most_common():
        print(f"      {k or '(빈값)':<12} {n}건")

print("")
print("  ⑤ status=held 조회")
rows, why = get(key, "jobs?status=eq.held&select=job_id&limit=1")
if rows is None:
    print(f"      조회 불가 — {why}")
    print("      → status 가 enum 일 가능성이 있습니다(그러면 held 추가가 필요).")
else:
    print(f"      조회 가능 ✅ (지금 held 인 작업 {len(rows)}건) — "
          f"필터가 통하므로 text 계열입니다")
    print("      ※ 단, **쓸 수 있는지**는 쓰기 권한이 있는 쪽에서 확인해야 합니다"
          "(여기서는 쓰지 않았습니다).")

print("")
print("=" * 74)
print(" 끝 — 읽기 전용으로만 확인했고 아무것도 바꾸지 않았습니다.")
print("=" * 74)
