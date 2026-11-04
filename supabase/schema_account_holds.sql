-- ══════════════════════════════════════════════════════════════════
--  ④ 로그아웃 ↔ 신규 배치 생성 경쟁을 **DB 수준에서** 닫는 방안
--     작성 2026-11-04 · ★제안서입니다. **실행하지 않았습니다.**
--     (이번 승인 범위는 코드 수정과 오프라인 검증까지이고, DDL 은 제외됩니다)
-- ══════════════════════════════════════════════════════════════════
--
--  지금 상태(코드로 할 수 있는 최선)
--    화면은 ①대기 작업 보류 → ②로그아웃 작업 등록 → ③2차 스윕 순으로 움직이고,
--    제출 때마다 그 계정의 보류 여부를 다시 읽어 막는다. 그래도 남는 창이 있다:
--
--      t1  화면 A: 보류 여부 읽음 → "없음"
--      t2  화면 B: 보류 실행(①) + 로그아웃 등록(②) + 2차 스윕(③)
--      t3  화면 A: INSERT  ← 이 작업은 ①②③ 모두를 비켜 간다(pending 으로 남는다)
--
--    피해 범위는 제한적이다. 그 작업이 실행될 때는 이미 로그아웃된 상태라
--    발행 전 로그인·blog_id 검사에서 멈춘다(= 엉뚱한 블로그에 올라가지 않는다).
--    하지만 '멈춘 실패 작업' 이 남고, 로그아웃이 아직 실행되지 않은 틈이면
--    사용자 의도와 반대로 **발행이 한 건 더 나갈 수** 있다.
--
--  근본 해결 = "보류 중인 계정" 을 **DB 가 알고** INSERT·claim 을 거부하게 한다.
--  아래 셋을 함께 넣으면 t1~t3 창이 사라진다(원자성은 Postgres 가 보장).
--
--  ★영향 범위 / 되돌리기
--    · 새 테이블 1개 + 트리거 1개 + `claim_job` 함수 1개 교체.
--    · 기존 행·컬럼은 바꾸지 않는다. Agent(1.2.5) 는 `claim_job` 만 부르므로
--      **설치본을 바꾸지 않아도** 그대로 동작한다(거부된 작업은 '없음' 으로 보인다).
--    · 되돌리기는 맨 아래 3줄.
--    · 넣기 전에 반드시 확인: `account_holds` 가 비어 있어야 평시 동작이 같다.

-- ──────────────────────────────────────────────────────────────────
-- ① 보류 중인 계정 표 — 화면이 로그아웃을 시작할 때 한 줄 넣고, 끝나면 지운다
-- ──────────────────────────────────────────────────────────────────
create table if not exists public.account_holds (
    hold_key   text primary key,          -- 정규화한 login_id, 없으면 'acct:'||account
    login_id   text not null default '',
    account    text not null default '',
    device_id  uuid,
    since      timestamptz not null default now(),
    reason     text not null default '사용자 요청'
);
alter table public.account_holds enable row level security;   -- 정책 없음 = anon 차단

-- ──────────────────────────────────────────────────────────────────
-- ② 보류 중인 계정의 **발행 작업 INSERT 를 거부**한다
--    ★kind='run' 만 막는다. 로그인·세션확인·로그아웃(kind='session')은
--      복구 수단이므로 절대 막지 않는다.
--    ★login_id 표기 차이(`a` / `a@naver.com`)는 코드와 같은 규칙으로 정규화한다.
-- ──────────────────────────────────────────────────────────────────
create or replace function public.norm_login(p text)
returns text language sql immutable as $$
    select lower(regexp_replace(coalesce(p, ''),
                 '@(naver\.com|mail\.naver\.com|naver\.net)$', '', 'i'))
$$;

create or replace function public.jobs_reject_when_held()
returns trigger language plpgsql as $$
declare v_key text;
begin
    if coalesce(new.kind, 'run') <> 'run' then
        return new;                       -- 복구 수단은 막지 않는다
    end if;
    v_key := case
        when coalesce(new.payload->>'login_id', '') <> ''
            then public.norm_login(new.payload->>'login_id')
        when coalesce(new.payload->>'account', '') <> ''
            then 'acct:' || (new.payload->>'account')
        else null end;
    if v_key is null then
        return new;                       -- 식별자가 없으면 추측하지 않는다
    end if;
    if exists (select 1 from public.account_holds h where h.hold_key = v_key) then
        raise exception
            '그 계정은 로그아웃 대기 중입니다 — 새 발행 작업을 만들 수 없습니다(%).',
            v_key using errcode = '23514';   -- 4xx 로 내려가 '실패 확정' 이 된다
    end if;
    return new;
end;
$$;

drop trigger if exists jobs_reject_when_held_tg on public.jobs;
create trigger jobs_reject_when_held_tg
    before insert on public.jobs
    for each row execute function public.jobs_reject_when_held();

-- ──────────────────────────────────────────────────────────────────
-- ③ 보류 중인 계정의 작업은 **집히지도 않게** 한다
--    (지금은 status='held' 로 바꿔 막는다. 아래가 있으면 status 를 못 바꾼
--     찰나의 작업도 집히지 않는다.)
-- ──────────────────────────────────────────────────────────────────
create or replace function public.claim_job(p_token text)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid; v_job public.jobs%rowtype;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;

    update public.jobs j set status = 'running', started_at = now()
     where j.job_id = (select job_id from public.jobs j2
                        where j2.device_id = v_id and j2.status = 'pending'
                          and not exists (
                              select 1 from public.account_holds h
                               where h.hold_key = case
                                   when coalesce(j2.payload->>'login_id','') <> ''
                                       then public.norm_login(j2.payload->>'login_id')
                                   else 'acct:' || coalesce(j2.payload->>'account','')
                                   end)
                        order by j2.created_at
                        limit 1 for update skip locked)
     returning j.* into v_job;

    if not found then return null; end if;
    return row_to_json(v_job);
end;
$$;
grant execute on function public.claim_job(text) to anon;

--  화면이 써야 하는 순서(코드 변경이 함께 필요하다 — 이번 범위 아님)
--    ① insert into account_holds …   ← 이 순간부터 INSERT·claim 이 막힌다
--    ② hold_account(pending→held)
--    ③ 로그아웃 작업 등록
--    ④ (Agent 가 로그아웃 완료) → delete from account_holds where hold_key = …
--    ⑤ [보류 작업 재개] → delete from account_holds … + resume_account
--  ★④⑤에서 지우지 못하면 그 계정의 발행이 계속 막힌다 → 화면에 '보류 해제'
--    버튼과 `since` 표시가 반드시 함께 있어야 한다.

-- ── 되돌리기 ──────────────────────────────────────────────────────
--   drop trigger if exists jobs_reject_when_held_tg on public.jobs;
--   drop function if exists public.jobs_reject_when_held();
--   drop table if exists public.account_holds;
--   ※ claim_job 은 supabase/schema.sql 의 원본으로 다시 실행하면 복구된다.
