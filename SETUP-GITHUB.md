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

## 두 가지 배포 경로

소스는 `site/index.html` 하나다. 배포 대상에 따라 데이터를 얻는 방식만 다르다.

| | GitHub Pages | Claude Artifact |
|---|---|---|
| 데이터 | `data/*.json` 을 fetch | `window.__BOOTSTRAP__` 로 인라인 |
| 갱신 | **자동** (매월 Actions) | 수동 (재퍼블리시) |
| 빌드 | `build_site.py` | `build_site.py` → `build_artifact.py` |

```bash
python scripts/build_site.py                   # site/data/*.json 생성
python scripts/build_artifact.py               # build/artifact.html (인라인 단일 파일)
```

Artifact 는 doctype/head/body 스켈레톤을 퍼블리시 시점에 씌우므로 `build_artifact.py` 가
래퍼를 벗기고 데이터를 인라인한다. Pages 용 `site/index.html` 은 건드리지 않는다.

**자동 갱신이 목적이면 Pages 가 정답이다.** Artifact 는 링크를 바로 열 수 있는 대신
데이터가 퍼블리시 시점에 고정된다.

## 러너에서 관세청 서버에 연결이 안 될 때

로그가 이렇게 끝나면 **키 문제가 아니다.**

```
✗ item  연결 불가: ConnectTimeout
키 문제가 아닙니다. apis.data.go.kr (27.101.236.63) 서버에 연결 자체가 되지 않았습니다
```

`apis.data.go.kr` 은 A 레코드가 `27.101.236.63` 하나뿐인 국내 서버다(AAAA 없음 →
IPv6 문제는 아니다). GitHub Actions 러너는 해외(Azure) IP라서, 같은 키·같은 코드가
한국에서는 정상 응답하는데 러너에서는 SYN 이 드롭돼 타임아웃이 나는 일이 있다.
상시 차단은 아니고 간헐적이다 — 같은 워크플로가 다른 날 정상 수집한 기록이 있다.

1. **먼저 재실행.** 30분~2시간 뒤 `Run workflow`. 대부분 이걸로 지나간다.
2. **계속 실패하면 국내 IP에서 돌린다** — 자체 호스팅 러너.

### 자체 호스팅 러너 (국내 IP)

**Settings → Actions → Runners → New self-hosted runner** 에서 나오는 명령을
본인 PC에서 그대로 실행한 뒤, 워크플로의 `runs-on` 만 바꾼다.

```yaml
jobs:
  collect:
    runs-on: self-hosted     # ubuntu-latest → self-hosted
```

- 장점: 국내 IP라 연결 문제가 없다. 수집도 더 빠르다.
- 단점: **수집 시각에 PC가 켜져 있어야 한다.** 월 1회(17일 07:00)뿐이므로
  꺼져 있었다면 나중에 수동으로 `Run workflow` 하면 그대로 따라잡는다
  (`fetch_log` 가 이미 받은 구간을 건너뛴다).
- `deploy` 잡은 `runs-on: ubuntu-latest` 로 두어도 된다. Pages 배포는 국내 IP가 필요 없다.

## 점검

```bash
python scripts/check_key.py         # 인증키 / 활용신청 상태
python tests/test_pipeline.py       # 오프라인 검증
python scripts/build_site.py        # 사이트 JSON 재생성 (API 호출 없음)
python -m http.server -d site 8000  # 로컬 미리보기
```

워크플로가 실패하면 Actions 로그의 "인증키 확인" 단계를 먼저 본다. 대부분 키 만료나
활용신청 누락이다.
