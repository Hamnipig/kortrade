# 자동 갱신 사이트 구축 (GitHub Actions + Pages)

매월 관세청 공표 직후 **수집 → 집계 → 배포**가 사람 손 없이 돌아가는 구성. 전부 무료 티어.

```
매월 17일 07:00 KST
   └─ GitHub Actions runner (관세청 API 접근 가능)
        ├─ run_update.py --sectors all     수집 → data/kortrade.sqlite
        ├─ build_site.py                   집계 → site/data/*.json
        ├─ git commit                      DB·JSON 커밋 (다음 달 증분 수집의 기준)
        └─ deploy-pages                    site/ → GitHub Pages
                                              ↓
                                   https://<계정>.github.io/<레포>/
```

> **왜 GitHub Actions인가**: Claude의 클라우드 환경은 `data.go.kr` 로 나가는 트래픽이
> 정책상 차단되어 있어 예약 작업으로 수집할 수 없다. Actions 러너는 제한이 없다.

---

## 1. 레포 만들기

```bash
cd kortrade
git init && git add -A
git commit -m "init: 관세청 수출 프록시 파이프라인 + 대시보드"
gh repo create kortrade --private --source=. --push      # 또는 웹에서 생성 후 push
```

`.gitignore` 에 `data/*.sqlite` 가 **없어야** 한다. DB를 커밋해야 다음 달 실행이
`fetch_log` 로 확정 구간을 건너뛴다(커밋 안 하면 매달 3,000콜 전량 재수집).

## 2. 인증키를 시크릿에 넣기

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `DATA_GO_KR_SERVICE_KEY` | 공공데이터포털 일반 인증키 |

```bash
gh secret set DATA_GO_KR_SERVICE_KEY   # CLI로도 가능
```

키를 코드나 커밋에 절대 넣지 말 것. 워크플로는 `secrets.` 로만 읽는다.

## 3. Pages 켜기

**Settings → Pages → Source: `GitHub Actions`**

(`Deploy from a branch` 아님. 워크플로가 `deploy-pages` 로 직접 배포한다.)

## 4. 첫 수집 실행

**Actions → Update trade data → Run workflow**

- `start`: `202001` (기본) — 더 긴 이력이 필요하면 `201901`
- 첫 실행은 시도코드 부트스트랩 100콜 + 수집 약 3,000콜, **20~40분**
- 2회차부터는 최근 6개월 + 신규 1개월만 재수집 → 수 분

끝나면 `https://<계정>.github.io/<레포>/` 에 대시보드가 뜬다.

---

## 갱신 주기를 바꾸려면

`.github/workflows/update.yml` 의 cron(UTC)만 고친다.

```yaml
- cron: "0 22 16 * *"   # 매월 17일 07:00 KST  (현재)
- cron: "0 22 * * 0"    # 매주 월요일 07:00 KST
- cron: "0 22 * * *"    # 매일 07:00 KST — 공표가 월 1회라 낭비. 권장하지 않음
```

관세청은 **월 1회** 공표하므로 그보다 자주 돌릴 이유가 없다. 다만 과거 월이 소급
정정되므로, 월 1회 실행이 최근 6개월을 매번 다시 받아 정정을 따라잡는다.

## 섹터 추가하기

`config/sectors/<key>.yaml` 하나를 더 쓰면 탭이 늘어난다. 코드 수정은 없다.

```yaml
key: display
name: 디스플레이
status: draft          # ← 처음엔 반드시 draft
dominant: "901380"
categories:
  "901380": {label: OLED 패널, group: 패널}
countries: [CN, VN, US]
```

`draft` 는 수집에서 제외되고 사이트에 "미수집" 탭으로만 보인다. **HS 코드를 데이터로
확인한 뒤** `active` 로 올린다:

```bash
python scripts/discover_hs.py --mode where --hs 901380 --sido 경기 충남 경북 --start 202401
```

금액이 실제로 잡히고 어느 시군구에 집중되는지 확인되면 승격한다. 검증 없이 active로
올리면 빈 데이터나 엉뚱한 품목이 조용히 쌓인다. 동봉된 `semiconductor`·`battery` 는
그래서 draft 상태다.

## 비용

| 항목 | 한도 | 이 워크플로 |
|---|---|---|
| Actions (퍼블릭 레포) | 무제한 | — |
| Actions (프라이빗) | 월 2,000분 | 월 1회 × 20~40분 |
| Pages | 1GB, 월 100GB 전송 | JSON 수십 KB |
| 관세청 개발계정 | 일 10,000콜 | 첫 실행 3,100 / 이후 수백 |

프라이빗 레포 + Pages 는 GitHub 유료 플랜이 필요하다. 무료로 쓰려면 **퍼블릭 레포**로
두되, 관세청 데이터는 공개 데이터이므로 문제는 분석 로직 노출뿐이다. 그게 걸리면
수집은 프라이빗 레포에서 하고 `site/` 만 별도 퍼블릭 레포로 push 하는 방법이 있다.

## 점검

```bash
python scripts/check_key.py         # 인증키 / 활용신청 상태
python tests/test_pipeline.py       # 오프라인 검증
python scripts/build_site.py        # 사이트 JSON 재생성 (API 호출 없음)
python -m http.server -d site 8000  # 로컬 미리보기
```

워크플로가 실패하면 Actions 로그의 "인증키 확인" 단계를 먼저 본다. 대부분 키 만료나
활용신청 누락이다.
