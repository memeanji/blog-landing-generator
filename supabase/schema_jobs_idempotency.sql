-- ══════════════════════════════════════════════════════════════════
--  작업 중복 등록을 **서버에서** 막는다 (멱등성 키 UNIQUE)
--    작성 2026-11-03 · ★아직 실행하지 않았음 — 사용자 승인 후 SQL 편집기에서 실행
-- ══════════════════════════════════════════════════════════════════
--  왜 필요한가
--    화면은 제출 1회마다 `client_key`(uuid hex)를 만들어 `jobs.payload.client_key`
--    에 넣는다. 응답이 유실되면 그 키로 **조회**해서 되찾는다(v2/queue_store.py
--    submit_checked). 그런데 조회는 '이미 들어갔는지' 를 확인하는 것일 뿐,
--    두 요청이 **동시에** 들어오는 경쟁 상태를 막지는 못한다.
--    아래 인덱스가 있으면 같은 키의 두 번째 INSERT 가 **DB 에서 거부**되어
--    (PostgREST 가 409 를 돌려준다) 작업이 한 건만 생긴다 = 원자적 보장.
--
--  영향 범위
--    · 테이블·컬럼 추가 없음. 기존 행 변경 없음. 읽기 경로 변경 없음.
--    · `client_key` 가 없는 과거 행은 인덱스 대상이 아니다(부분 인덱스).
--    · Agent 는 jobs 에 INSERT 하지 않으므로 Agent 영향 없음(claim/update 만 한다).
--    · 되돌리기 = 맨 아래 DROP 한 줄.
--
--  실행 전 확인(중복이 있으면 인덱스 생성이 실패한다 — 지금은 0건이어야 정상)
--    select payload->>'client_key' as k, count(*)
--      from public.jobs
--     where payload ? 'client_key'
--     group by 1 having count(*) > 1;

create unique index if not exists jobs_client_key_uniq
    on public.jobs ((payload->>'client_key'))
 where payload ? 'client_key';

--  확인: 인덱스가 생겼는지
--    select indexname from pg_indexes
--     where tablename = 'jobs' and indexname = 'jobs_client_key_uniq';

--  ── 되돌리기 ──────────────────────────────────────────────────────
--    drop index if exists public.jobs_client_key_uniq;
