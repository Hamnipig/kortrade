# 변경분만 — 2026-09-28 · 최종 수요 축 (미국 수입 · 미국 설치용량)

레포 루트에 **덮어쓰기** 하면 됩니다. 아래 파일 외에는 건드리지 않았습니다.

## 새 파일 (4)

| 파일 | 무엇 |
|---|---|
| `config/demand.yaml` | 미국 HTS 통계품목(전부 draft) · Census/EIA 라우트 · 판정 문턱 |
| `kortrade/demand.py` | 판정 로직 — 현지화 / 점유율 상실 / 수요 위축 분리, 지수화, 점유율 |
| `scripts/run_demand.py` | 수집기. **키 없으면 0 으로 정상 종료.** EIA 연료코드는 API 에게 물어서 발견 |
| `tests/test_demand.py` | 회귀 7개 |

## 수정 파일 (7)

| 파일 | 무엇이 바뀌었나 |
|---|---|
| `kortrade/store.py` | `demand_series` 표 신설(관세청 표와 **분리**) · `upsert_demand()` / `demand()` · coverage 에 추가 |
| `scripts/build_battery.py` | `build_demand()` 추가 → `battery.json` 의 `demand` 키 |
| `scripts/selfcheck.py` | demand 모듈·설정·스크립트 등록 |
| `site/index.html` | `renderDemand()` 패널 + `#bdem` 을 `display:contents` 에 추가 |
| `.github/workflows/update.yml` | `수집 — 최종 수요` 단계 (continue-on-error, 10분) |
| `tests/test_battery.py` | `display:contents` 검사 보강 |
| `README.md` | '최종 수요 축' 절 + 출처 3건 |

## GitHub 시크릿 2개 (둘 다 무료, 없어도 나머지는 그대로 돕니다)

```
CENSUS_API_KEY   https://api.census.gov/data/key_signup.html
EIA_API_KEY      https://www.eia.gov/opendata/register.php
```

## 첫 실행 뒤 확인할 것

`data/demand_verify.json` 이 생깁니다. 여기서 두 가지를 봅니다.

1. `census.codes.8507600030.matched` 가 `true` 인가 — 미국 HTS 코드가 맞는지
   응답의 품목 설명으로 대조한 결과입니다. `false` 면 코드를 고쳐야 합니다.
2. `eia.found` 에 라우트와 배터리 연료코드가 찍혔는가 — `null` 이면 EIA 축은
   비고 화면도 비웁니다.

## 검증

69 → **76개 테스트 통과** (test_demand 7 신규).
Playwright 헤드리스 렌더 — 2차전지 탭 JS 오류 0건, 수요 데이터 있을 때/없을 때 모두.
