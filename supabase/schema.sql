-- ══════════════════════════════════════════════════════════════════
-- 블로그 랜딩 생성기 — 원격 작업 큐 (Supabase)
--
--   Streamlit Cloud ──service key──▶ 테이블 직접
--   Windows Agent   ──publishable key + device_token──▶ RPC 함수만
--
-- ★Agent 에는 service key 를 절대 넣지 않는다. Agent 가 쓰는 값은
--   ① 공개해도 되는 publishable(anon) key ② 자기 device_token 뿐이다.
--   모든 테이블은 RLS 로 막혀 있어 anon 키로는 직접 읽고 쓸 수 없고,
--   아래 SECURITY DEFINER 함수(토큰 검증 포함)로만 접근된다.
--   → 남의 device 작업은 구조적으로 가져갈 수 없다.
--
-- 적용: Supabase 대시보드 → SQL Editor 에 이 파일 전체를 붙여넣고 Run
-- 되돌리기: 맨 아래 주석의 DROP 문 참고
-- ══════════════════════════════════════════════════════════════════

-- ★Supabase 는 확장을 `extensions` 스키마에 둔다. 이미 설치돼 있으면 그대로 둔다.
create extension if not exists pgcrypto with schema extensions;

-- ── 테이블 ────────────────────────────────────────────────────────
create table if not exists public.devices (
    device_id    uuid primary key default gen_random_uuid(),
    label        text not null default '',          -- PC 이름(DESKTOP-XXXX)
    token_hash   text not null,                     -- device_token 의 sha256 (원본 저장 안 함)
    agent_version text not null default '',
    state        text not null default 'idle',      -- idle | busy | stopped
    current_job  uuid,
    last_seen    timestamptz not null default now(),
    created_at   timestamptz not null default now()
);
create index if not exists devices_token_idx on public.devices (token_hash);

create table if not exists public.pairings (
    code        text primary key,                   -- 6자리
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,
    used_at     timestamptz,                        -- 1회용
    device_id   uuid references public.devices (device_id)
);

create table if not exists public.jobs (
    job_id      uuid primary key default gen_random_uuid(),
    device_id   uuid not null references public.devices (device_id) on delete cascade,
    kind        text not null default 'run',        -- run | session
    title       text not null default '',
    brand       text not null default '',
    payload     jsonb not null default '{}'::jsonb, -- Job.to_dict()
    status      text not null default 'pending',    -- pending|running|done|failed|canceled
    exit_code   int,
    error       text not null default '',
    total       int  not null default 0,
    made        int  not null default 0,
    published   jsonb not null default '[]'::jsonb,
    cancel_requested boolean not null default false,
    command     text not null default '',
    created_at  timestamptz not null default now(),
    started_at  timestamptz,
    finished_at timestamptz
);
create index if not exists jobs_claim_idx on public.jobs (device_id, status, created_at);

create table if not exists public.job_logs (
    id      bigserial primary key,
    job_id  uuid not null references public.jobs (job_id) on delete cascade,
    line    text not null,
    at      timestamptz not null default now()
);
create index if not exists job_logs_job_idx on public.job_logs (job_id, id);

create table if not exists public.job_events (
    id      bigserial primary key,
    job_id  uuid not null references public.jobs (job_id) on delete cascade,
    event   jsonb not null,
    at      timestamptz not null default now()
);
create index if not exists job_events_job_idx on public.job_events (job_id, id);

-- ── RLS: 전면 차단(정책 없음 = anon 은 아무것도 못 한다) ──────────
--    service key(Streamlit 서버)는 RLS 를 우회한다.
alter table public.devices    enable row level security;
alter table public.pairings   enable row level security;
alter table public.jobs       enable row level security;
alter table public.job_logs   enable row level security;
alter table public.job_events enable row level security;

-- ── Agent 전용 RPC (SECURITY DEFINER + 토큰 검증) ─────────────────
create or replace function public._device_of(p_token text)
returns uuid language sql stable security definer set search_path = public, extensions as $$
    select device_id from public.devices
     where token_hash = encode(digest(p_token, 'sha256'), 'hex')
     limit 1;
$$;

-- ① 페어링: 6자리 코드 → device 생성 + token 발급(1회용)
create or replace function public.pair_device(p_code text, p_label text,
                                              p_version text default '')
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_row public.pairings%rowtype; v_token text; v_id uuid;
begin
    select * into v_row from public.pairings
     where code = p_code and used_at is null and expires_at > now()
     for update;
    if not found then
        raise exception '페어링 코드가 없거나 만료되었습니다';
    end if;

    v_token := encode(gen_random_bytes(32), 'hex');
    insert into public.devices (label, token_hash, agent_version)
         values (coalesce(p_label, ''),
                 encode(digest(v_token, 'sha256'), 'hex'),
                 coalesce(p_version, ''))
      returning device_id into v_id;

    update public.pairings set used_at = now(), device_id = v_id where code = p_code;
    return json_build_object('device_id', v_id, 'device_token', v_token);
end;
$$;

-- ② heartbeat — 살아 있음 표시
create or replace function public.agent_heartbeat(p_token text, p_state text,
                                                  p_job uuid default null,
                                                  p_version text default '')
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    update public.devices
       set last_seen = now(), state = coalesce(p_state, 'idle'), current_job = p_job,
           agent_version = case when p_version = '' then agent_version else p_version end
     where device_id = v_id;
    return json_build_object('device_id', v_id, 'ok', true);
end;
$$;

-- ③ claim — **자기 device 의 pending 1건만** 원자적으로 가져간다
create or replace function public.claim_job(p_token text)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid; v_job public.jobs%rowtype;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;

    update public.jobs j set status = 'running', started_at = now()
     where j.job_id = (select job_id from public.jobs
                        where device_id = v_id and status = 'pending'
                        order by created_at
                        limit 1 for update skip locked)
     returning j.* into v_job;

    if not found then return null; end if;
    return row_to_json(v_job);
end;
$$;

-- ④ 진행 상황 갱신
create or replace function public.update_job(p_token text, p_job uuid,
                                             p_fields jsonb)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    update public.jobs
       set total     = coalesce((p_fields->>'total')::int, total),
           made      = coalesce((p_fields->>'made')::int, made),
           published = coalesce(p_fields->'published', published),
           error     = coalesce(p_fields->>'error', error),
           command   = coalesce(p_fields->>'command', command)
     where job_id = p_job and device_id = v_id;      -- ★내 작업만
    return json_build_object('ok', found);
end;
$$;

-- ⑤ 종료 처리
create or replace function public.finish_job(p_token text, p_job uuid,
                                             p_status text, p_exit int)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    update public.jobs
       set status = p_status, exit_code = p_exit, finished_at = now(),
           cancel_requested = false
     where job_id = p_job and device_id = v_id;
    return json_build_object('ok', found);
end;
$$;

-- ⑥ 로그 / 이벤트 적재(배치)
create or replace function public.append_log(p_token text, p_job uuid, p_lines text[])
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    if not exists (select 1 from public.jobs where job_id = p_job and device_id = v_id)
    then raise exception '내 작업이 아닙니다'; end if;
    insert into public.job_logs (job_id, line)
        select p_job, unnest(p_lines);
    return json_build_object('ok', true);
end;
$$;

create or replace function public.append_event(p_token text, p_job uuid, p_event jsonb)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    if not exists (select 1 from public.jobs where job_id = p_job and device_id = v_id)
    then raise exception '내 작업이 아닙니다'; end if;
    insert into public.job_events (job_id, event) values (p_job, p_event);
    return json_build_object('ok', true);
end;
$$;

-- ⑦ 중단 요청 확인
create or replace function public.cancel_requested(p_token text, p_job uuid)
returns boolean language plpgsql security definer set search_path = public, extensions as $$
declare v_id uuid; v_flag boolean;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    select cancel_requested into v_flag from public.jobs
     where job_id = p_job and device_id = v_id;
    return coalesce(v_flag, false);
end;
$$;

-- ── 권한: anon 은 함수 실행만, 테이블 직접 접근은 불가 ────────────
--   ★이 프로젝트에는 다른 서비스(광고 레퍼런스 등)의 테이블도 있다.
--     그래서 `all tables in schema public` 이 아니라 **우리 테이블 5개만** 회수한다.
revoke all on public.devices, public.pairings, public.jobs,
                public.job_logs, public.job_events from anon;
grant execute on function public.pair_device(text, text, text)          to anon;
grant execute on function public.agent_heartbeat(text, text, uuid, text) to anon;
grant execute on function public.claim_job(text)                        to anon;
grant execute on function public.update_job(text, uuid, jsonb)          to anon;
grant execute on function public.finish_job(text, uuid, text, int)      to anon;
grant execute on function public.append_log(text, uuid, text[])         to anon;
grant execute on function public.append_event(text, uuid, jsonb)        to anon;
grant execute on function public.cancel_requested(text, uuid)           to anon;
-- ★Postgres 는 새 함수의 EXECUTE 를 PUBLIC 에 기본 부여한다.
--   anon 만 회수하면 PUBLIC 경로로 여전히 호출된다 → PUBLIC 까지 회수한다.
revoke execute on function public._device_of(text) from public, anon;   -- 내부용

-- ══════════════════════════════════════════════════════════════════
-- 되돌리기(필요할 때만)
-- drop function if exists public.pair_device, public.agent_heartbeat,
--      public.claim_job, public.update_job, public.finish_job,
--      public.append_log, public.append_event, public.cancel_requested,
--      public._device_of;
-- drop table if exists public.job_events, public.job_logs, public.jobs,
--      public.pairings, public.devices;
-- ══════════════════════════════════════════════════════════════════
