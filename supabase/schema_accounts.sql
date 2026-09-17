-- ══════════════════════════════════════════════════════════════════
--  블로그 계정 중앙관리 (2026-09-17)
--
--  왜 있는가
--    PC 마다 `%APPDATA%\BlogLandingAgent\accounts.json` 을 사람이 넣어야 했다.
--    담당자 PC 에는 화면(브라우저)과 Agent 두 개뿐이라 파일을 손으로 옮기는 게
--    현실적이지 않았다. 계정 목록을 여기 한 곳에 두고 Agent 가 읽어 가게 한다.
--
--  원칙
--    · Agent 는 **읽기 전용**. service_role key 를 넣지 않는다.
--      기존 device 토큰 방식을 그대로 쓴다(`_device_of` 재사용, schema.sql 에 있음).
--    · 쓰기는 service key 를 가진 쪽(Supabase 콘솔 / 화면)만 한다.
--    · `id` 는 **세션 폴더 이름**이다(`sessions/<id>/`). 절대 바꾸지 않는다 —
--      바꾸면 모든 PC 의 저장된 네이버 로그인이 고아가 되어 전원 재로그인이 된다.
--    · 구버전의 탭이름/해시 기반 자동 생성은 복구하지 않는다.
--
--  적용 순서
--    ① 이 파일을 Supabase SQL Editor 에서 실행(테이블·뷰·함수·권한)
--    ② 아래 '초기 적재' 블록 실행(현재 8건)
--    ③ '확인' 블록으로 결과 점검
-- ══════════════════════════════════════════════════════════════════

-- ── 1. 테이블 ─────────────────────────────────────────────────────
create table if not exists public.blog_accounts (
    id          text primary key,              -- ★sessions/<id> 와 1:1. 변경 금지
    label       text not null default '',      -- 화면 표시 이름
    login_id    text not null default '',      -- 네이버 로그인 ID = 계정 선택 1순위 근거
    blog_id     text not null default '',      -- 실제 블로그 주소(로그인 후 대조)
    brand       text not null default '',      -- '' = 기본 브랜드(리퓨어리)
    ref_tab     text not null default '',      -- login_id 없는 계정의 유일한 단서
    note        text not null default '',
    active      boolean not null default true, -- accounts.json 의 enabled
    updated_at  timestamptz not null default now()
);

comment on column public.blog_accounts.id is
    '세션 폴더 이름(sessions/<id>). 기존 값을 그대로 유지할 것 — 바꾸면 재로그인 발생';
comment on column public.blog_accounts.ref_tab is
    'login_id 가 없는 계정은 (ref_tab + brand) 조합으로만 선택된다';

-- ── 2. 중복 방지 / 진단 ───────────────────────────────────────────
--  login_id 가 겹치면 "어느 계정인지 정할 수 없음" 으로 작업이 멈춘다
--  (v2/accounts.py find_by_identity) → DB 에서 미리 막는다. 현재 중복 0건.
create unique index if not exists blog_accounts_login_idx
    on public.blog_accounts (lower(login_id))
 where active and login_id <> '';

--  ★blog_id 에는 UNIQUE 를 걸지 않는다.
--    같은 블로그를 브랜드별로 나눠 등록한 정상 구조라 지금 데이터가 이미 중복이다
--    (hangbokhaseo7 ×2 · seoyoungene_ ×2 · iammeanji ×2, 각자 세션 폴더가 따로 있다).
--    정리가 끝난 뒤에 켜고 싶으면 아래 주석을 풀면 된다.
-- create unique index blog_accounts_blog_idx
--     on public.blog_accounts (lower(blog_id)) where active and blog_id <> '';

--  대신 중복을 언제든 확인할 수 있게 진단 뷰를 둔다(운영자 전용)
create or replace view public.blog_accounts_dupes as
    select 'login_id' as field, lower(login_id) as value, count(*) as n,
           array_agg(id order by id) as ids
      from public.blog_accounts
     where active and login_id <> ''
     group by 1, 2
    having count(*) > 1
    union all
    select 'blog_id', lower(blog_id), count(*), array_agg(id order by id)
      from public.blog_accounts
     where active and blog_id <> ''
     group by 1, 2
    having count(*) > 1;

-- ── 3. updated_at 자동 갱신(변경 감지용) ──────────────────────────
create or replace function public._touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists blog_accounts_touch on public.blog_accounts;
create trigger blog_accounts_touch
    before update on public.blog_accounts
    for each row execute function public._touch_updated_at();

-- ── 4. 권한 — 테이블은 완전 차단, 토큰 검증 함수만 연다 ───────────
alter table public.blog_accounts enable row level security;
revoke all on public.blog_accounts       from anon, public;
revoke all on public.blog_accounts_dupes from anon, public;

--  ★비활성 계정까지 **원본 그대로** 돌려준다.
--    파이썬이 `load_accounts(include_disabled=True)` 로 읽고 있어서,
--    여기서 걸러 버리면 지금 동작이 달라진다. 필터는 파이썬이 한다.
create or replace function public.accounts_list(p_token text)
returns table (id text, label text, login_id text, blog_id text,
               brand text, ref_tab text, note text,
               active boolean, updated_at timestamptz)
language sql stable security definer set search_path = public, extensions as $$
    select a.id, a.label, a.login_id, a.blog_id, a.brand, a.ref_tab,
           a.note, a.active, a.updated_at
      from public.blog_accounts a
     where public._device_of(p_token) is not null   -- 토큰이 없으면 0행
     order by a.id;
$$;

--  ★Postgres 는 새 함수의 EXECUTE 를 PUBLIC 에 기본 부여한다.
--    anon 만 회수하면 PUBLIC 경로로 여전히 호출된다 → PUBLIC 까지 회수한 뒤 anon 만 준다.
revoke execute on function public.accounts_list(text)  from public, anon;
grant  execute on function public.accounts_list(text)  to anon;
revoke execute on function public._touch_updated_at()  from public, anon;

-- ══════════════════════════════════════════════════════════════════
--  초기 적재 — 현재 accounts.json 8건 (id/label/login_id/blog_id 원본 그대로)
--   ⚠️`민지  테스트 계정` 의 가운데 두 칸 공백은 기준시트 탭 이름과 정확히
--     일치해야 하므로 그대로 둔다. 다듬지 말 것.
-- ══════════════════════════════════════════════════════════════════
insert into public.blog_accounts (id, label, login_id, blog_id, brand, ref_tab, active)
values
  ('smile_hyunmi', '스마일 현미',         'scwscw0615',           'myloveeee207',  '',               '스마일 현미 기준랜딩',       true),
  ('iammeanji',    'iammeanji (구·숨김)',  '',                     'iammeanji',     '',               '',                        false),
  ('seoyeon',      '행복하서연',           'rhksrhf6996',          'hangbokhaseo7', '',               '행복하서연 기준랜딩',        true),
  ('tab_caa3dca8', '행복하서연',           '',                     'hangbokhaseo7', 'doctor_nuscent', '행복하서연 기준랜딩',        true),
  ('shin894894',   '행복하서연 대표님',     'shin894894@naver.com', 'seoyoungene_',  'repurely',       '행복하서연 대표님 기준랜딩',   true),
  ('tab_0cfc92b8', '민지  테스트 계정',     '',                     'iammeanji',     'repurely',       '민지  테스트 계정 기준랜딩',   true),
  ('tab_7605d481', '행복하서연대표님',      '',                     'seoyoungene_',  'doctor_nuscent', '행복하서연대표님 기준랜딩',    true),
  ('tab_0585c723', '민지 테스트',          '',                     'iammeanji',     'doctor_nuscent', '민지 테스트 기준랜딩',       true)
on conflict (id) do update set
    label    = excluded.label,
    login_id = excluded.login_id,
    blog_id  = excluded.blog_id,
    brand    = excluded.brand,
    ref_tab  = excluded.ref_tab,
    active   = excluded.active;

-- ══════════════════════════════════════════════════════════════════
--  확인
-- ══════════════════════════════════════════════════════════════════
-- select id, login_id, blog_id, brand, active, updated_at
--   from public.blog_accounts order by id;            -- 8행이어야 한다
-- select * from public.blog_accounts_dupes;           -- login_id 행은 0건이어야 정상
--                                                     -- blog_id 행 3건은 정상(브랜드 분리)

-- ══════════════════════════════════════════════════════════════════
--  되돌리기(필요할 때만)
--   drop trigger if exists blog_accounts_touch on public.blog_accounts;
--   drop function if exists public.accounts_list(text), public._touch_updated_at();
--   drop view if exists public.blog_accounts_dupes;
--   drop table if exists public.blog_accounts;
-- ══════════════════════════════════════════════════════════════════
