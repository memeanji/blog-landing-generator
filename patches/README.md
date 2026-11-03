# 단계별 분리 패치 (2026-11-03)

같은 파일에 여러 단계의 변경이 섞여 있어, **단계별로 따로 배포**할 수 있게 쪼개 둔 것입니다.
전부 `HEAD`(= 원격 `main`, 2026-09-17 `05052a6`) 기준이고, 아래 순서대로 적용하면
현재 작업 트리와 **완전히 같아집니다**(검증 완료).

```
git apply --check patches/stage1_only.patch      # 먼저 --check 로 확인
git apply         patches/stage1_only.patch
git apply         patches/stage2_only.patch
git apply         patches/help_removal_only.patch
git apply         patches/stage3_fastwait_only.patch
```

| 패치 | 내용 | 단독 적용 |
|---|---|---|
| `stage1_only.patch` | 연결 안정화·멱등성 (streamlit_app · queue_store · supabase_store · timeutil) | HEAD 에 바로 ✅ |
| `stage2_only.patch` | 계정 세션/로그아웃 중 **공유 파일 부분**(위 3개 파일) | Stage 1 뒤 ✅ |
| `help_removal_only.patch` | '❓ 처음 사용하시나요? — 도움말 보기' UI 제거(streamlit_app 1곳) | Stage 2 뒤 ✅ |
| `stage3_fastwait_only.patch` | 발행 속도 — **글 화면 진입 대기 2곳만**(writer · edit_post). 9/14 미커밋 변경과 분리됨 | HEAD 에 바로 ✅ |
| `preexisting_0914_writer_editpost.patch` | ★제 작업이 **아닌** 기존 9/14 미커밋 변경(참고용 보관) | — |

## 주의

- `stage2_only.patch` 에는 **새 파일이 들어 있지 않습니다.** Stage 2 는 아래 파일도
  함께 올려야 동작합니다: `v2/login_state.py`(신규) · `v2/session_store.py` ·
  `v2/session.py` · `v2/browser.py` · `v2/pairing.py` ·
  `installer/BlogLandingAgent.iss`. 이 파일들은 다른 단계와 섞이지 않아
  패치로 쪼갤 필요가 없습니다(그대로 커밋하면 됩니다).

- 왕복 검증(2026-11-03 16:00): 임시 저장소에서 HEAD 에 다섯 패치를
  `stage1 → stage2 → help_removal → preexisting_0914 → stage3` 순으로 적용한 결과가
  현재 작업 트리의 다섯 파일과 **md5 완전 일치**했습니다.
- `stage3_fastwait_only.patch` 는 `v2/fastwait.py` 가 함께 있어야 동작합니다.
- `preexisting_0914_writer_editpost.patch` 는 **배포 대상이 아닙니다.** 9/14 에 누가
  무엇을 고쳤는지 확인하고 따로 판단하기 위한 기록입니다.
