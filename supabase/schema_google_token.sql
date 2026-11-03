-- ═══════════════════════════════════════════════════════════════════════════
--  google-token Edge Function 용 스키마 보강 (2026-09-10)
--
--  ★새로 anon 에게 여는 함수는 **하나도 없다.** 구글 인증은 전부 Edge Function 안에서
--    service_role 로 일어나고, 바깥으로는 1시간짜리 access token 만 나간다.
--
--  ★device_token 이 어디에 있는가(정확히)
--      · 이 DB 의 devices.token_hash : **SHA-256 만**            (원문 없음)
--      · 페어링된 PC 의 device.json  : **원문**  ← Agent 의 bearer 라 정상이다
--      · job payload / 큐 레코드      : 없음
--      · Edge Function 로그           : 없음 (해시 앞 12자만)
--      · Agent 로그                   : 없음
--    유출 대응은 아래 revoked_at 이다(그 PC 만 즉시 차단, 다른 기기 영향 없음).
--  ★Google private_key 는 DB 에 넣지 않는다 — Edge Function Secret(GOOGLE_SA_JSON)에만 둔다.
--  ★rate limit 은 **DB 에 둔다** — Edge Function 메모리는 인스턴스마다 따로이고 재시작하면
--    사라져서, 보안용 제한의 근거로 삼을 수 없다(2026-09-10 사용자 지적).
--
--  Supabase Dashboard → SQL Editor 에 그대로 붙여넣어 실행하세요. 여러 번 실행해도 안전합니다.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── ① 기기 해제(revoke) 표시 ────────────────────────────────────────────────
--    값이 채워지면 그 기기는 새 토큰을 못 받는다. 행을 지워도 되지만 지우면 jobs 까지
--    함께 사라지므로(on delete cascade) 보통은 이 컬럼만 채워서 '해제' 한다.
alter table public.devices
    add column if not exists revoked_at timestamptz;

comment on column public.devices.revoked_at is
    '값이 있으면 연결 해제된 기기 — google-token Edge Function 이 즉시 401 을 준다';

-- ── ② 인증 시도 스로틀(공용) ────────────────────────────────────────────────
--    key 예:  'gt:<token_hash 앞 16자>'   구글 토큰 요청
--            'pair'                        페어링 코드 시도(전역)
create table if not exists public.auth_throttle (
    key           text primary key,
    window_start  timestamptz not null default now(),
    fail_count    int         not null default 0,
    ok_count      int         not null default 0,
    last_ok       timestamptz,
    updated_at    timestamptz not null default now()
);
alter table public.auth_throttle
    add column if not exists ok_count int not null default 0;

comment on table public.auth_throttle is
    '잘못된 인증 시도 제한 — Edge Function 인스턴스가 여러 개여도 같은 값을 본다';

-- ── ③ 발급 감사 로그 ────────────────────────────────────────────────────────
--    토큰 원문은 남기지 않고 '누가 언제 어떤 권한을' 만 남긴다.
create table if not exists public.google_token_audit (
    id          bigserial primary key,
    device_id   uuid references public.devices (device_id) on delete cascade,
    operation   text not null,                    -- readonly | write
    ok          boolean not null default true,
    created_at  timestamptz not null default now()
);
create index if not exists google_token_audit_idx
    on public.google_token_audit (device_id, created_at desc);

-- ── ④ 관문 함수 — Edge Function 이 service_role 로만 부른다 ─────────────────
--    기기 확인 + 스로틀 + 최소 간격을 **한 번의 왕복, 한 트랜잭션**으로 처리한다.
--    반환 {allowed, reason, device_id}
--      reason: ok | unknown_device | rate_limited
create or replace function public.google_token_gate(p_token_hash text,
                                                    p_operation text)
returns json language plpgsql security definer
set search_path = public, extensions as $$
declare
    -- ★키에 operation 을 넣는다 — readonly 와 write 는 서로의 한도를 잡아먹지 않는다.
    --   (조회 직후 기록으로 넘어가는 정상 흐름이 막히면 안 된다)
    v_key       text := 'gt:' || left(coalesce(p_token_hash, ''), 16)
                        || ':' || coalesce(p_operation, '');
    v_row       public.auth_throttle%rowtype;
    v_device    uuid;
    v_win       interval := interval '1 minute';   -- 카운트 창
    v_fail_max  int      := 10;                    -- 창 안 허용 실패
    v_ok_max    int      := 20;                    -- 창 안 허용 발급(정상 사용엔 넉넉)
begin
    if p_operation not in ('readonly', 'write') then
        return json_build_object('allowed', false, 'reason', 'unknown_operation');
    end if;

    insert into public.auth_throttle (key) values (v_key)
        on conflict (key) do nothing;
    select * into v_row from public.auth_throttle where key = v_key for update;

    -- 창이 지났으면 카운트를 접는다
    if v_row.window_start < now() - v_win then
        update public.auth_throttle
           set window_start = now(), fail_count = 0, ok_count = 0, updated_at = now()
         where key = v_key
        returning * into v_row;
    end if;

    if v_row.fail_count >= v_fail_max or v_row.ok_count >= v_ok_max then
        return json_build_object('allowed', false, 'reason', 'rate_limited');
    end if;

    select device_id into v_device from public.devices
     where token_hash = p_token_hash and revoked_at is null
     limit 1;

    if v_device is null then
        update public.auth_throttle
           set fail_count = v_row.fail_count + 1, updated_at = now()
         where key = v_key;
        return json_build_object('allowed', false, 'reason', 'unknown_device');
    end if;

    update public.auth_throttle
       set last_ok = now(), ok_count = v_row.ok_count + 1,
           fail_count = 0, updated_at = now()
     where key = v_key;

    insert into public.google_token_audit (device_id, operation, ok)
         values (v_device, p_operation, true);

    return json_build_object('allowed', true, 'reason', 'ok',
                             'device_id', v_device);
end;
$$;

-- ── ⑤ 페어링 무차별 대입 방지 ───────────────────────────────────────────────
--    `pair_device` 는 anon 이 부를 수 있는 함수라 시도 제한이 필요하다.
--    코드 공간이 100만이라 제한이 없으면 10분 창 안에 전부 훑을 수 있다.
--
--  ★두 겹으로 막는다(2026-09-10 사용자 지적 — 전역 하나만 두면 한 명이 실패를 퍼부어
--    **다른 사람의 정상 페어링까지 막는 DoS** 가 된다).
--      ⓐ IP 별   : 1분에 실패 10회   ← 평소 걸리는 건 이쪽뿐이다
--      ⓑ 전역    : 1분에 실패 200회  ← 분산 폭주용 backstop. 넉넉하게 둔다
--
--  ★IP 는 PostgREST 가 넘겨 주는 요청 헤더에서 읽는다
--      current_setting('request.headers', true)::json ->> 'x-forwarded-for'
--    · 이 값은 Supabase 엣지가 채워 준다. 헤더를 못 읽는 환경이면 **자동으로 전역만** 적용된다
--      (아래 v_ip is null 분기) — 그때는 ⓑ 가 유일한 방어선이므로 그 점을 알고 있어야 한다.
--    · XFF 는 이론상 위조될 수 있어 ⓐ 만 믿지 않고 ⓑ 를 함께 둔다.
create or replace function public.pair_device(p_code text, p_label text,
                                              p_version text default '')
returns json language plpgsql security definer
set search_path = public, extensions as $$
declare
    v_row     public.pairings%rowtype;
    v_thr     public.auth_throttle%rowtype;
    v_token   text;
    v_id      uuid;
    v_ip      text;
    v_ip_key  text;
    v_win     interval := interval '1 minute';
    v_ip_max  int := 10;
    v_all_max int := 200;
begin
    -- 요청 IP(있으면). 'a, b' 처럼 여러 개면 맨 앞(원 클라이언트)만 쓴다.
    begin
        v_ip := split_part(
            coalesce(nullif(current_setting('request.headers', true), ''), '{}')::json
                ->> 'x-forwarded-for', ',', 1);
        v_ip := nullif(btrim(coalesce(v_ip, '')), '');
    exception when others then
        v_ip := null;
    end;
    v_ip_key := case when v_ip is null then null else 'pair:ip:' || v_ip end;

    -- ⓐ IP 별 스로틀
    if v_ip_key is not null then
        insert into public.auth_throttle (key) values (v_ip_key)
            on conflict (key) do nothing;
        select * into v_thr from public.auth_throttle where key = v_ip_key for update;
        if v_thr.window_start < now() - v_win then
            update public.auth_throttle set window_start = now(), fail_count = 0,
                   ok_count = 0, updated_at = now()
             where key = v_ip_key returning * into v_thr;
        end if;
        if v_thr.fail_count >= v_ip_max then
            raise exception '페어링 시도가 너무 많습니다. 1분 뒤에 다시 시도해 주세요.';
        end if;
    end if;

    -- ⓑ 전역 backstop
    insert into public.auth_throttle (key) values ('pair')
        on conflict (key) do nothing;
    select * into v_thr from public.auth_throttle where key = 'pair' for update;
    if v_thr.window_start < now() - v_win then
        update public.auth_throttle set window_start = now(), fail_count = 0,
               ok_count = 0, updated_at = now() where key = 'pair' returning * into v_thr;
    end if;
    if v_thr.fail_count >= v_all_max then
        raise exception '페어링 요청이 일시적으로 몰려 있습니다. 잠시 뒤 다시 시도해 주세요.';
    end if;

    select * into v_row from public.pairings
     where code = p_code and used_at is null and expires_at > now()
     for update;
    if not found then
        update public.auth_throttle set fail_count = fail_count + 1, updated_at = now()
         where key = 'pair' or (v_ip_key is not null and key = v_ip_key);
        raise exception '페어링 코드가 없거나 만료되었습니다';
    end if;

    v_token := encode(gen_random_bytes(32), 'hex');   -- 256비트 CSPRNG(기존과 동일)
    insert into public.devices (label, token_hash, agent_version)
         values (coalesce(p_label, ''),
                 encode(digest(v_token, 'sha256'), 'hex'),
                 coalesce(p_version, ''))
      returning device_id into v_id;

    update public.pairings set used_at = now(), device_id = v_id where code = p_code;
    update public.auth_throttle set fail_count = 0, last_ok = now(), updated_at = now()
     where key = 'pair' or (v_ip_key is not null and key = v_ip_key);
    -- ★device_token 원문은 **여기서 한 번만** 밖으로 나가고, 저장은 그 PC 의 device.json 뿐이다.
    --   DB 에는 위에서 넣은 sha256 해시만 남는다.
    return json_build_object('device_id', v_id, 'device_token', v_token);
end;
$$;

-- ── ⑤-2 PC 이름 바꾸기 ──────────────────────────────────────────────────────
--    트레이의 [이름 바꾸기] 와 `v2.pairing.rename()` 이 이 함수를 부른다.
--    ★지금까지 서버에 이 함수가 없어서 **PC 에서 이름을 바꿔도 화면에는 반영되지 않았다**
--      (device.json 만 바뀌고 devices.label 은 페어링 때 값 그대로). 같은 이름이 여러 개
--      보이는 원인이기도 하다. 자기 토큰으로 **자기 행만** 바꾼다.
create or replace function public.rename_device(p_token text, p_label text)
returns json language plpgsql security definer
set search_path = public, extensions as $$
declare v_id uuid;
begin
    v_id := public._device_of(p_token);
    if v_id is null then raise exception '알 수 없는 device_token'; end if;
    if coalesce(btrim(p_label), '') = '' then
        raise exception '이름이 비어 있습니다';
    end if;
    update public.devices set label = left(btrim(p_label), 60)
     where device_id = v_id;
    return json_build_object('device_id', v_id, 'label', left(btrim(p_label), 60));
end;
$$;

-- ── ⑥ 권한 ──────────────────────────────────────────────────────────────────
--    anon 은 새 테이블·새 함수에 손댈 수 없다. google_token_gate 는 Edge Function 이
--    service_role 로만 부른다(PUBLIC 기본 EXECUTE 까지 회수).
revoke all on public.auth_throttle, public.google_token_audit from anon;
revoke execute on function public.google_token_gate(text, text) from public, anon;
grant execute on function public.pair_device(text, text, text) to anon;   -- 기존 유지
grant execute on function public.rename_device(text, text) to anon;       -- 토큰으로 자기 행만

-- ═══════════════════════════════════════════════════════════════════════════
--  운영용 한 줄 명령
-- ═══════════════════════════════════════════════════════════════════════════
-- ▸ 연결된 기기 보기 (device_token 원문은 DB 에 없다 — 해시만 있다)
--     select device_id, label, agent_version, state, last_seen, revoked_at
--       from public.devices order by last_seen desc;
--
-- ▸ ★같은 이름이 여러 개일 때 — 어느 것이 '지금 살아 있는' 기기인가
--   (Agent 는 5초마다 하트비트를 보낸다 → last_seen 이 방금이면 그게 진짜다)
--     select device_id, label, agent_version, state,
--            now() - last_seen as 마지막신호_경과, created_at, revoked_at
--       from public.devices
--      where label = '표님'
--      order by last_seen desc;
--
-- ▸ 헷갈리지 않게 이름 바꾸기(서버에서 바로)
--     update public.devices set label = '표님 노트북' where device_id = '<uuid>';
--
-- ▸ 안 쓰는 옛 기기 정리 — 지우지 말고 revoke 를 권한다
--   (행을 지우면 그 기기의 jobs 이력까지 함께 사라진다: on delete cascade)
--     update public.devices set revoked_at = now() where device_id = '<uuid>';
--
-- ▸ 기기 연결 해제 — 이 순간부터 그 PC 는 새 access token 을 못 받는다
--   (이미 받아 간 토큰은 최대 1시간 살아 있다 — Google 이 정하는 수명이라 회수 불가)
--     update public.devices set revoked_at = now() where device_id = '<uuid>';
--
-- ▸ 해제 취소
--     update public.devices set revoked_at = null where device_id = '<uuid>';
--
-- ▸ 발급 이력 / 스로틀 상태
--     select * from public.google_token_audit order by created_at desc limit 50;
--     select * from public.auth_throttle order by updated_at desc;
--
-- ▸ 스로틀이 잘못 걸렸을 때 풀기
--     update public.auth_throttle set fail_count = 0 where key = 'pair';
