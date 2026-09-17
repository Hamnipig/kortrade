"""시도코드 자동 탐색.

관세청 API 는 sidoCd(2자리)를 요구하지만 코드표를 API 로 제공하지 않는다.
참고문서 `관세청조회코드_v1.3.xlsx` 에 있으나 다운로드·파싱 의존성을 만들지 않기 위해,
getSidotradeList(sidoCd 옵션)에 후보 코드를 넣어보고 응답의 sidoNm 을 읽어 역으로 표를 만든다.

  - 전수 탐색은 00~99 = 100콜. 일 10,000콜 예산 대비 무시할 수준이고 1회만 하면 된다.
  - 2026-07-01 지방행정체계 개편(전남광주 통합, 인천 개편)으로 코드 체계가 바뀌었으므로
    개편 전/후 각각 기준월을 잡아 두 벌의 표를 만든다.
"""

from __future__ import annotations

import logging

from .client import CustomsClient, CustomsAPIError, normalize_period
from .store import Store

log = logging.getLogger(__name__)

# 2026-09 실측으로 확인한 관세청 시도코드 17개.
# ※ 행정표준코드와 대체로 같지만 **42(강원도), 45(전라북도)는 존재하지 않는다.**
#    강원은 51(강원특별자치도), 전북은 52(전북특별자치도)로만 조회된다.
#    잘못된 코드를 넣으면 resultCode 99 '존재하지 않는 시도코드입니다'가 돌아온다.
VERIFIED_SIDO_CODES = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시",
    "29": "광주광역시", "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시",
    "41": "경기도", "43": "충청북도", "44": "충청남도", "46": "전라남도",
    "47": "경상북도", "48": "경상남도", "50": "제주특별자치도",
    "51": "강원특별자치도", "52": "전북특별자치도",
}

# 2026-07-01 전남광주 통합 이후 (실측: 코드 29/46 은 2026-06 까지만 응답,
# 코드 12 는 2026-08 부터 응답). 개편 전후로 표가 달라지므로 둘 다 관리한다.
VERIFIED_SIDO_CODES_POST_202607 = {
    **{k: v for k, v in VERIFIED_SIDO_CODES.items() if k not in ("29", "46")},
    "12": "전남광주통합특별시",
}

# 탐색 순서를 앞당기기 위한 힌트. 12 는 2026-07 개편(전남광주 통합) 대비.
LIKELY_CODES = list(VERIFIED_SIDO_CODES) + ["12", "42", "45"]

# 행정체계 개편 경계
REORG_EFFECTIVE = "2026-07"


def _candidates() -> list[str]:
    rest = [f"{i:02d}" for i in range(100) if f"{i:02d}" not in LIKELY_CODES]
    return LIKELY_CODES + rest


def discover_sido_codes(client: CustomsClient, ref_yymm: str,
                        exhaustive: bool = True) -> dict[str, str]:
    """ref_yymm(YYYYMM) 시점 기준으로 sidoCd -> 시도명 매핑을 만든다."""
    found: dict[str, str] = {}
    cands = _candidates() if exhaustive else LIKELY_CODES

    for code in cands:
        try:
            rows = client.call("sido", strtYymm=ref_yymm, endYymm=ref_yymm, sidoCd=code)
        except CustomsAPIError as exc:
            if exc.fatal:
                raise
            log.warning("시도코드 %s 탐색 실패: %s", code, exc)
            continue
        names = {r.get("sido_name", "").strip() for r in rows if r.get("sido_name")}
        names.discard("")
        if len(names) == 1:
            found[code] = names.pop()
        elif len(names) > 1:
            # sidoCd 가 무시되고 전체가 돌아온 경우 — 그 코드는 유효하지 않다고 본다
            log.debug("코드 %s 응답에 시도명이 %d개 → 무시", code, len(names))

    log.info("%s 기준 시도코드 %d개 확인", ref_yymm, len(found))
    return found


def bootstrap(client: CustomsClient, store: Store,
              pre_reorg_yymm: str = "202601",
              post_reorg_yymm: str | None = None) -> dict[str, dict[str, str]]:
    """개편 전/후 두 벌의 시도코드 표를 만들어 저장한다."""
    out: dict[str, dict[str, str]] = {}

    pre = discover_sido_codes(client, pre_reorg_yymm)
    if pre:
        store.save_sido_codes(pre, valid_from="1900-01")
        out["1900-01"] = pre

    if post_reorg_yymm:
        post = discover_sido_codes(client, post_reorg_yymm)
        if post:
            store.save_sido_codes(post, valid_from=REORG_EFFECTIVE)
            out[REORG_EFFECTIVE] = post

    return out


# 시도명 표기는 소스마다 흔들린다: '강원' / '강원도' / '강원특별자치도',
# '충북' / '충청북도'. 정식명칭으로 정규화한 뒤 비교한다.
_SUFFIXES = ("특별자치도", "특별자치시", "광역시", "특별시", "자치도", "도", "시")
_ABBREV = {
    "충북": "충청북도", "충남": "충청남도", "전북": "전라북도", "전남": "전라남도",
    "경북": "경상북도", "경남": "경상남도", "강원": "강원도", "제주": "제주도",
    "경기": "경기도", "서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천",
    "광주": "광주", "대전": "대전", "울산": "울산", "세종": "세종",
}


def canon_sido(name: str) -> str:
    """시도명을 비교 가능한 축약 형태로 정규화한다. '충청북도'->'충북', '강원특별자치도'->'강원'."""
    s = name.replace(" ", "")
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    # 정식명칭 -> 축약 (충청북도 -> 충북)
    for abbr, full in _ABBREV.items():
        if full.replace("도", "") == s or full == s or full == name.replace(" ", ""):
            return abbr
    if s in _ABBREV:
        return s
    # '충청북'처럼 접미사만 떨어진 경우
    if len(s) >= 3:
        cand = s[0] + s[2]
        if cand in _ABBREV:
            return cand
    return s


def sido_code_for(store: Store, name: str, period: str | None = None) -> str:
    """시도명 -> 코드. period('YYYY-MM')를 주면 개편 전/후 표를 알아서 고른다."""
    valid_from = REORG_EFFECTIVE if (period and period >= REORG_EFFECTIVE) else "1900-01"
    table = store.sido_codes(valid_from=valid_from) or store.sido_codes()
    needle = canon_sido(name)
    for cd, nm in table.items():
        if canon_sido(nm) == needle:
            return cd
    raise KeyError(
        f"시도명 '{name}'(정규화: '{needle}') 에 해당하는 코드를 찾지 못했습니다. "
        f"scripts/bootstrap_codes.py 를 먼저 실행하세요. "
        f"(보유 표: { {cd: nm for cd, nm in table.items()} })"
    )
