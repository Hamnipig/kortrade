# 변경분만 — 2026-09-28 (2차) · 커밋 실패 수정

> **이전 변경분(`kortrade-changed-2026-09-28.zip`, 최종 수요 축)을 먼저 적용한 뒤
> 이 파일들을 덮어써 주십시오.** 겹치는 파일은 `scripts/selfcheck.py` 하나뿐이고,
> 이 zip 의 것이 최신입니다.

## 무엇이 터졌나

```
warning: Cannot merge binary files: data/kortrade.sqlite
CONFLICT (content): Merge conflict in data/kortrade.sqlite
fatal: You are not currently on a branch.      ← rebase 중단 → detached HEAD
Error: Process completed with exit code 128
```

`update.yml` 과 `flash.yml` 이 **같은 `data/kortrade.sqlite` 를 같은 브랜치에 커밋**합니다.
커밋 직전에 상대가 먼저 push 하면 `git pull --rebase` 가 걸리는데,
**sqlite 는 바이너리라 텍스트 merge 가 원리적으로 불가능합니다.** rebase 가 중단되면
detached HEAD 가 되고 `git push` 가 exit 128 로 죽습니다.

`--ours` / `--theirs` 로 한쪽을 버리는 것도 답이 아닙니다 — 버리는 쪽이 방금 쓴
수백~수천 API 콜이기 때문입니다(`fetch_log` 가 사라지면 다음 실행이 다시 받습니다).

**레포는 멀쩡합니다.** 아무것도 push 되지 않았고 러너 작업공간만 깨졌습니다.
그 실행의 수집분만 날아갔고, `fetch_log` 도 같이 날아갔으니 다음 실행이 다시 받습니다.

## 어떻게 고쳤나 — git 이 아니라 SQLite 레벨에서 병합

모든 표가 자연키 UNIQUE + UPSERT 구조라 **행 단위로 합칠 수 있습니다.**

| | |
|---|---|
| 양쪽에 다 있는 행 | **내 값이 남는다** (이번 실행이 방금 수집한 최신값) |
| 상대에만 있는 행 | 추가된다 (상대가 쓴 API 콜을 잃지 않는다) |
| `revisions` (UNIQUE 없음) | `EXCEPT` 로 완전 중복만 걸러 붙인다 |
| 상대에 없는 표·컬럼 | 건너뛴다 (양쪽 컬럼 **교집합만** 옮긴다) |

병합 뒤 `site/data/*.json` 은 DB 파생물이므로 다시 만듭니다. 최대 5회 재시도합니다.

## 파일

| 파일 | |
|---|---|
| `scripts/merge_db.py` | **새 파일.** 두 sqlite 를 행 단위로 병합 |
| `scripts/commit_push.sh` | **새 파일.** 커밋 → fetch → (충돌 시) 병합 → 재생성 → push, 5회 재시도 |
| `.github/workflows/update.yml` | 커밋 단계를 `commit_push.sh` 호출로 교체 (8분) |
| `.github/workflows/flash.yml` | 같은 교체 · 잡 한도 35 → 45분 (커밋 단계가 3→8분이라) |
| `tests/test_pipeline.py` | 회귀 테스트 1개 추가 — 충돌 병합 결과 + rebase 부활 차단 |
| `scripts/selfcheck.py` | `merge_db` 등록 |

## 확인

77개 테스트 통과 (test_pipeline 29 · test_demand 7 · test_battery 8 · test_pq 8 ·
test_flash 15 · test_sectors_site 10).

테스트가 못박는 것: 충돌 행은 내 값이 이기고, 상대 행은 보존되며,
**두 워크플로 어디에도 `pull --rebase` 가 되살아나지 않는다.**

## 다음 실행

워크플로를 다시 돌리면 됩니다. 지난 실행이 날린 구간은 `fetch_log` 가 비어 있으므로
자동으로 다시 수집합니다 — 따로 하실 일은 없습니다.
