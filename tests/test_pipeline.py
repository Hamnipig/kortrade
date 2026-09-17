"""오프라인 검증 — API 호출 없이 파싱/저장/분석 로직을 전부 통과시킨다.

실행:  python -m pytest tests/ -q     또는   python tests/test_pipeline.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import analyze, kpi
from kortrade.client import (CustomsAPIError, chunk_periods, month_range,
                             normalize_period, parse_response)
from kortrade.collect import latest_available_yymm, shift_yymm
from kortrade.store import Store

CONFIG = Path(__file__).resolve().parent.parent / "config"
import yaml
API_CFG = yaml.safe_load((CONFIG / "api.yaml").read_text(encoding="utf-8"))
SIGUNGU_FIELDS = API_CFG["endpoints"]["sigungu_item"]["item_fields"]
NITEM_FIELDS = API_CFG["endpoints"]["item_country"]["item_fields"]
ITEM_FIELDS = API_CFG["endpoints"]["item"]["item_fields"]

# ---------------------------------------------------------------- fixtures

SIGUNGU_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
  <body>
    <items>
      <item>
        <cmtrBlncAmt>12345678</cmtrBlncAmt><expCnt>412</expCnt>
        <expUsdAmt>13000000</expUsdAmt><hsSgn>330499</hsSgn>
        <impCnt>9</impCnt><impUsdAmt>654322</impUsdAmt>
        <korePrlstNm>기타 미용·메이크업용 제품류</korePrlstNm>
        <priodTitle>2025.01</priodTitle><sggNm>강릉시</sggNm>
      </item>
      <item>
        <cmtrBlncAmt>-500</cmtrBlncAmt><expCnt>3</expCnt>
        <expUsdAmt>1500</expUsdAmt><hsSgn>330499</hsSgn>
        <impCnt>1</impCnt><impUsdAmt>2000</impUsdAmt>
        <korePrlstNm>기타 미용·메이크업용 제품류</korePrlstNm>
        <priodTitle>2025.01</priodTitle><sggNm>원주시</sggNm>
      </item>
    </items>
    <totalCount>2</totalCount>
  </body>
</response>"""

NITEM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>
  <body><items>
    <item><year>2025.01</year><statCdCntnKor1>미국</statCdCntnKor1><statCd>US</statCd>
      <statKor>기초화장품</statKor><hsCd>330499</hsCd>
      <expWgt>1800300</expWgt><expDlr>150822000</expDlr>
      <impWgt>14809</impWgt><impDlr>9375411</impDlr>
      <balPayments>141446589</balPayments></item>
  </items><totalCount>1</totalCount></body>
</response>"""

ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse><cmmMsgHeader>
  <returnReasonCode>30</returnReasonCode>
  <returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""

EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>
<body><items/><totalCount>0</totalCount></body></response>"""


# ---------------------------------------------------------------- 파싱

def test_parse_sigungu():
    rows = parse_response(SIGUNGU_XML, SIGUNGU_FIELDS)
    assert len(rows) == 2
    r = rows[0]
    assert r["sigungu_name"] == "강릉시"
    assert r["hs_code"] == "330499"
    assert r["exp_usd"] == "13000000"
    assert r["period"] == "2025.01"
    print("  ✓ 시군구 응답 파싱")


def test_parse_nitem():
    rows = parse_response(NITEM_XML, NITEM_FIELDS)
    assert len(rows) == 1
    assert rows[0]["country_code"] == "US"
    assert rows[0]["exp_usd"] == "150822000"
    print("  ✓ 품목-국가 응답 파싱")


def test_parse_error_is_fatal():
    try:
        parse_response(ERROR_XML, SIGUNGU_FIELDS)
    except CustomsAPIError as e:
        assert e.fatal, "인증키 오류는 재시도해도 소용없으므로 fatal 이어야 한다"
        print("  ✓ 인증키 오류를 fatal 로 분류")
        return
    raise AssertionError("오류 응답인데 예외가 발생하지 않았다")


def test_parse_empty():
    assert parse_response(EMPTY_XML, SIGUNGU_FIELDS) == []
    print("  ✓ 빈 응답 처리")


# ---------------------------------------------------------------- 기간

def test_periods():
    assert month_range("202311", "202402") == ["202311", "202312", "202401", "202402"]
    chunks = list(chunk_periods("202001", "202212", 12))
    assert chunks == [("202001", "202012"), ("202101", "202112"), ("202201", "202212")]
    assert normalize_period("2016.01") == "2016-01"
    assert normalize_period("201601") == "2016-01"
    assert shift_yymm("202601", -1) == "202512"
    assert shift_yymm("202512", 2) == "202602"
    import datetime as dt
    assert latest_available_yymm(dt.date(2026, 9, 16)) == "202608"
    assert latest_available_yymm(dt.date(2026, 9, 3)) == "202607"
    assert latest_available_yymm(dt.date(2026, 1, 3)) == "202511"
    print("  ✓ 기간 분할·시프트·공표시차")


# ---------------------------------------------------------------- 저장/정정

def test_upsert_and_revision():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        rows = parse_response(SIGUNGU_XML, SIGUNGU_FIELDS)
        recs = [{**r, "period": normalize_period(r["period"]),
                 "sido_cd": "42", "sido_name": "강원"} for r in rows]

        s1 = st.upsert_region(recs)
        assert s1["inserted"] == 2, s1

        s2 = st.upsert_region(recs)                      # 동일 데이터 재수집
        assert s2 == {"inserted": 0, "updated": 0, "unchanged": 2}, s2

        recs[0]["exp_usd"] = "13500000"                  # 관세청 소급 정정 시뮬레이션
        s3 = st.upsert_region(recs)
        assert s3["updated"] == 1, s3

        rev = st.frame("SELECT * FROM revisions")
        assert len(rev) == 1
        assert rev.iloc[0]["field"] == "exp_usd"
        assert rev.iloc[0]["old_value"] == "13000000"
        assert rev.iloc[0]["new_value"] == "13500000"

        cov = st.coverage()
        assert cov["region_trade"]["rows"] == 2 and cov["revisions"] == 1
        st.close()
    print("  ✓ UPSERT 멱등성 + 소급 정정 감지")


def test_fetch_log_dedup():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        p = {"strtYymm": "202401", "endYymm": "202412", "HsSgn": "330499", "sidoCd": "42"}
        assert not st.already_fetched("sigungu_item", p)
        st.mark_fetched("sigungu_item", p, rows=12)
        assert st.already_fetched("sigungu_item", p)
        assert not st.already_fetched("sigungu_item", {**p, "sidoCd": "41"})
        st.close()
    print("  ✓ fetch_log 중복 호출 차단")


def test_sido_resolution():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        st.save_sido_codes({"42": "강원특별자치도", "11": "서울특별시"}, valid_from="1900-01")
        from kortrade.codes import sido_code_for
        assert sido_code_for(st, "강원") == "42"
        assert sido_code_for(st, "강원특별자치도") == "42"
        assert sido_code_for(st, "서울") == "11"
        st.close()
    print("  ✓ 시도명 표기 흔들림 흡수")


# ---------------------------------------------------------------- 분석

def _synthetic_sector(store: Store):
    """3년치 합성 데이터: 330499 는 최근 가속, 330510 은 감속."""
    periods = pd.period_range("2023-01", "2025-12", freq="M").astype(str)
    rows = []
    for i, p in enumerate(periods):
        base_a = 100_000_000 * (1 + 0.004 * i)
        if i >= 24:
            base_a *= 1 + 0.02 * (i - 23)        # 2025년부터 가속
        base_b = 50_000_000 * (1 + 0.010 * i)
        if i >= 24:
            base_b *= 1 - 0.012 * (i - 23)       # 2025년부터 둔화
        for hs, v, nm in (("330499", base_a, "기초화장품"), ("330510", base_b, "샴푸")):
            rows.append({"period": p, "hs_code": hs, "hs_name": nm,
                         "country_code": "US", "country_name": "미국",
                         "exp_usd": int(v), "exp_wgt": 1, "imp_usd": 0,
                         "imp_wgt": 0, "bal_usd": int(v)})
    store.upsert_sector(rows)


def test_momentum_and_rotation():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        _synthetic_sector(st)
        df = analyze.sector_monthly(st)
        tbl = analyze.momentum_table(df, key="hs_code",
                                     labels={"330499": "기초", "330510": "샴푸"})
        assert not tbl.empty
        a = tbl[tbl["hs_code"] == "330499"].iloc[0]
        b = tbl[tbl["hs_code"] == "330510"].iloc[0]
        assert a["가속_%p"] > 0 > b["가속_%p"], (a["가속_%p"], b["가속_%p"])
        assert a["비중변화_%p"] > 0
        assert tbl.iloc[0]["hs_code"] == "330499", "가속 상위가 먼저 와야 한다"

        scored = analyze.rotation_score(tbl, min_size_usd=1_000_000)
        assert scored.iloc[0]["hs_code"] == "330499"
        assert scored.iloc[0]["로테이션점수"] > scored.iloc[1]["로테이션점수"]
        st.close()
    print("  ✓ 모멘텀/가속/비중변화/로테이션 점수")


def test_concentration_and_purity():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        rows = []
        for p in pd.period_range("2025-01", "2025-12", freq="M").astype(str):
            rows += [
                {"period": p, "hs_code": "300610", "hs_name": "생체재료", "sido_cd": "42",
                 "sido_name": "강원", "sigungu_name": "강릉시", "exp_cnt": 40,
                 "exp_usd": 9_000_000, "imp_cnt": 0, "imp_usd": 0, "bal_usd": 9_000_000},
                {"period": p, "hs_code": "300610", "hs_name": "생체재료", "sido_cd": "41",
                 "sido_name": "경기", "sigungu_name": "화성시", "exp_cnt": 5,
                 "exp_usd": 600_000, "imp_cnt": 0, "imp_usd": 0, "bal_usd": 600_000},
            ]
        st.upsert_region(rows)
        c = analyze.concentration(st, "300610", months=12)
        assert c.iloc[0]["sigungu_name"] == "강릉시"
        assert c.attrs["top1_share"] > 90
        assert analyze.purity_grade(c.attrs["HHI"], c.attrs["top1_share"]) == "A"

        rep = __import__("kortrade.discover", fromlist=["purity_report"]).purity_report(
            st, "300610", "강릉시")
        assert rep["제안등급"] == "A" and rep["전국_순위"] == 1
        st.close()
    print("  ✓ 지역 집중도 → 신호 순도 등급")


# ---------------------------------------------------------------- KPI

def test_leadlag_detects_true_lead():
    """프록시가 1분기 선행하도록 만든 합성 데이터에서 lag=1 이 최선으로 뽑혀야 한다."""
    rng = np.random.default_rng(7)
    quarters = [f"{y}-Q{q}" for y in range(2018, 2026) for q in range(1, 5)]
    shock = rng.normal(0, 0.10, len(quarters))
    proxy = [100 * (1.03 ** i) * (1 + shock[i]) for i in range(len(quarters))]
    # 매출은 직전 분기 프록시 충격을 따라간다 (+ 소량 노이즈)
    revenue = [200 * (1.03 ** i) * (1 + 0.9 * shock[i - 1] + rng.normal(0, 0.012))
               if i > 0 else 200.0 for i in range(len(quarters))]

    monthly = {}
    for q, v in zip(quarters, proxy):
        y, n = int(q[:4]), int(q[-1])
        for m in range(3 * (n - 1) + 1, 3 * n + 1):
            monthly[f"{y}-{m:02d}"] = v / 3
    s = pd.Series(monthly).sort_index()

    kdf = pd.DataFrame({"period": quarters, "metric": "매출액", "value": revenue})
    aligned = kpi.align(s, kdf)
    assert len(aligned) == len(quarters)

    ll = kpi.leadlag(aligned, max_lag=3)
    valid = ll.dropna(subset=["R2"])
    best = int(valid.sort_values("R2", ascending=False)["lag_분기"].iloc[0])
    assert best == 1, f"lag=1 이 최선이어야 하는데 {best}\n{ll}"
    assert kpi.best_lag(ll) == 1
    print(f"  ✓ 선행성 검증이 lag=1 을 정확히 식별 (R2={valid.set_index('lag_분기').loc[1,'R2']})")


def test_leadlag_rejects_noise():
    """상관 없는 계열에서는 best_lag 가 None 을 돌려줘야 한다 (오탐 방지)."""
    rng = np.random.default_rng(3)
    quarters = [f"{y}-Q{q}" for y in range(2018, 2026) for q in range(1, 5)]
    monthly = {}
    for q in quarters:
        y, n = int(q[:4]), int(q[-1])
        v = 100 * (1 + rng.normal(0, 0.2))
        for m in range(3 * (n - 1) + 1, 3 * n + 1):
            monthly[f"{y}-{m:02d}"] = v / 3
    s = pd.Series(monthly).sort_index()
    kdf = pd.DataFrame({"period": quarters, "metric": "매출액",
                        "value": 200 * (1 + rng.normal(0, 0.2, len(quarters)))})
    ll = kpi.leadlag(kpi.align(s, kdf), max_lag=3)
    assert kpi.best_lag(ll) is None, f"노이즈인데 lag 를 채택했다\n{ll}"
    print("  ✓ 무관한 계열은 채택 거부 (오탐 방지)")


def test_quarter_run_rate():
    """분기 2개월치만 있을 때 계절 진척률로 분기 총액을 추정한다."""
    monthly = {}
    for y in (2022, 2023, 2024):
        for m, w in ((1, 0.20), (2, 0.30), (3, 0.50)):   # 분기 말 집중 출하 패턴
            monthly[f"{y}-{m:02d}"] = 300.0 * w
    monthly["2025-01"] = 80.0
    monthly["2025-02"] = 120.0                            # 2개월차, 누적 200
    s = pd.Series(monthly).sort_index()
    rr = kpi.quarter_run_rate(s, "2025-Q1")
    assert rr["status"] == "partial" and rr["months_in"] == 2
    assert abs(rr["progress_ratio"] - 0.5) < 1e-6, rr     # 과거 (0.2+0.3)/1.0 = 0.5
    assert abs(rr["estimate_usd"] - 400.0) < 1e-6, rr
    # 단순 n/3 이었다면 200/(2/3)=300 → 계절성 보정이 실제로 작동했음
    assert kpi.to_quarter("2025-02") == "2025-Q1"
    print("  ✓ 분기 진척률 나우캐스팅 (계절성 보정)")


# ---------------------------------------------------------------- config

def test_configs_load():
    import yaml
    hs = yaml.safe_load((CONFIG / "hs_cosmetics.yaml").read_text(encoding="utf-8"))
    comp = yaml.safe_load((CONFIG / "companies.yaml").read_text(encoding="utf-8"))
    from kortrade.collect import hs_codes, hs_labels
    core = hs_codes(hs, ("core",))
    allc = hs_codes(hs, ("core", "probe"))
    assert "330499" in core and len(core) >= 15
    assert "300610" in allc and "300610" not in core
    assert all(len(c) == 6 for c in allc), "시군구 API 는 HS6 만 받는다"
    assert len(hs_labels(hs)) == len(allc)
    for name, c in comp["companies"].items():
        assert c["purity"] in ("A", "B", "C", "?"), name
        assert all(len(h) == 6 for h in c["hs_watch"]), name
    for ep in API_CFG["endpoints"].values():
        assert ep["path"].startswith("/") and "serviceKey" in ep["params"]
    print(f"  ✓ 설정 파일 정합성 (core {len(core)}개 / 전체 {len(allc)}개 HS)")



# ---------------------------------------------------------------- 실데이터에서 발견된 버그

REAL_TOTAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode><resultMsg>정상서비스.</resultMsg></header>
<body><items>
  <item><year>2025.01</year><hsCode>3304991000</hsCode><statKor>기초</statKor>
    <expWgt>10672681</expWgt><expDlr>314778843</expDlr>
    <impWgt>1</impWgt><impDlr>2</impDlr><balPayments>3</balPayments></item>
  <item><year>총계</year><hsCode>-</hsCode><statKor>-</statKor>
    <expWgt>131194311</expWgt><expDlr>4120149974</expDlr>
    <impWgt>0</impWgt><impDlr>0</impDlr><balPayments>0</balPayments></item>
</items><totalCount>2</totalCount></body></response>"""

REAL_SGG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode><resultMsg>정상서비스.</resultMsg></header>
<body><items>
  <item><cmtrBlncAmt>3,791</cmtrBlncAmt><expCnt>52</expCnt><expUsdAmt>3,989</expUsdAmt>
    <hsSgn>330499</hsSgn><impCnt>9</impCnt><impUsdAmt>198</impUsdAmt>
    <korePrlstNm>기타</korePrlstNm><priodTitle>2025.01</priodTitle>
    <sggNm>강원특별자치도 강릉시</sggNm></item>
</items><totalCount>1</totalCount></body></response>"""


def test_total_row_is_filtered():
    """응답에 섞여 오는 기간='총계' 합계 행을 저장하면 금액이 2배가 된다."""
    rows = parse_response(REAL_TOTAL_XML, ITEM_FIELDS)
    assert len(rows) == 1, f"총계 행이 걸러지지 않았다: {rows}"
    assert rows[0]["period"] == "2025.01"
    from kortrade.client import is_total_row
    assert is_total_row({"period": "총계"})
    assert not is_total_row({"period": "2025.01"})
    print("  ✓ '총계' 합계 행 제외")


def test_hs6_rollup():
    """품목별 API 는 hsSgn=330499(6단위)로 요청해도 10단위로 쪼개 응답한다."""
    from kortrade.client import hs6
    assert hs6("3304991000") == "330499"
    assert hs6("330499") == "330499"
    assert hs6("-") == ""
    rows = parse_response(REAL_TOTAL_XML, ITEM_FIELDS)
    assert rows[0]["hs_code"] == "3304991000"
    assert hs6(rows[0]["hs_code"]) == "330499"
    print("  ✓ HS 10단위 → 6단위 롤업")


def test_sigungu_name_split():
    """sggNm 은 '강원특별자치도 강릉시' 형태로 온다."""
    from kortrade.client import split_sigungu
    assert split_sigungu("강원특별자치도 강릉시") == ("강원특별자치도", "강릉시")
    assert split_sigungu("경기도 화성시") == ("경기도", "화성시")
    assert split_sigungu("강릉시") == ("", "강릉시")
    print("  ✓ 시군구명에서 시도 접두 분리")


def test_region_unit_normalized_to_usd():
    """시군구 API 는 천달러 단위. 달러로 정규화하지 않으면 레이어 비교가 1000배 틀어진다."""
    from kortrade.collect import _scale, REGION_AMOUNT_UNIT_USD
    assert REGION_AMOUNT_UNIT_USD == 1000
    assert _scale("3,989", 1000) == "3989000"
    assert _scale("", 1000) is None
    rows = parse_response(REAL_SGG_XML, SIGUNGU_FIELDS)
    assert rows[0]["exp_usd"] == "3,989"          # 원본은 천달러, 콤마 포함
    assert _scale(rows[0]["exp_usd"], 1000) == "3989000"
    print("  ✓ 시군구 금액 천달러 → 달러 정규화")


def test_verified_sido_codes():
    """42(강원도)/45(전라북도)는 관세청 API 에 존재하지 않는다 — 51/52 만 유효."""
    from kortrade.codes import VERIFIED_SIDO_CODES
    assert VERIFIED_SIDO_CODES["51"] == "강원특별자치도"
    assert VERIFIED_SIDO_CODES["52"] == "전북특별자치도"
    assert "42" not in VERIFIED_SIDO_CODES and "45" not in VERIFIED_SIDO_CODES
    assert len(VERIFIED_SIDO_CODES) == 17
    print("  ✓ 실측 시도코드 17개 (42/45 부재)")



def test_baseline_subtraction():
    """HS 6단위에 섞인 형제 10단위 코드 혼입분을 상수로 제거하는 보정."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        rows = []
        # 2년간 월 300(혼입분만) -> 이후 2년간 월 300+2000(목표기업 기여)
        for i, p_ in enumerate(pd.period_range("2022-01", "2025-12", freq="M").astype(str)):
            v = 300_000 if i < 24 else 2_300_000
            rows.append(dict(period=p_, hs_code="330499", hs_name="x", sido_cd="51",
                             sido_name="강원특별자치도", sigungu_name="강릉시",
                             exp_cnt=10, exp_usd=v, imp_cnt=0, imp_usd=0, bal_usd=v))
        st.upsert_region(rows)

        raw = kpi.export_proxy(st, "강릉시", ["330499"])
        est = kpi.estimate_baseline(raw, before="2024-01")
        assert est["status"] == "ok" and est["n"] == 24
        assert abs(est["baseline_usd_month"] - 300_000) < 1, est
        assert est["cv"] == 0.0 and "타당" in est["판정"], est

        adj = kpi.export_proxy(st, "강릉시", ["330499"], baseline_usd=est["baseline_usd_month"])
        assert adj.loc["2025-06"] == 2_000_000, adj.loc["2025-06"]
        assert adj.loc["2022-06"] == 0.0, "혼입 구간은 0 으로 클리핑되어야 한다"
        assert (adj >= 0).all(), "음수가 나오면 안 된다"

        # 변동이 큰 구간은 상수 가정을 거부해야 한다
        noisy = pd.Series([100, 900, 50, 1200, 80, 1500, 40, 1100],
                          index=[f"2022-{m:02d}" for m in range(1, 9)], dtype=float)
        assert "부적절" in kpi.estimate_baseline(noisy, before="2023-01")["판정"]
        st.close()
    print("  ✓ 혼입 상수 보정 + 상수 가정 타당성 판정")


def test_sigungu_api_rejects_10digit_is_documented():
    """시군구 API 는 HS 6단위만 받는다 — 설정에 그 제약이 명시돼 있어야 한다."""
    spec = API_CFG["endpoints"]["sigungu_item"]
    assert "6단위" in spec["params"]["HsSgn"]["desc"]
    from kortrade.collect import load_hs_config, hs_codes
    allc = hs_codes(load_hs_config(), ("core", "probe"))
    assert all(len(c) == 6 for c in allc), "10단위 코드가 섞이면 런타임에 resultCode 99"
    print("  ✓ HS 6단위 제약이 설정에 고정됨")



# ---------------------------------------------------------------- 행정구역 개편

def test_boundary_change_handling():
    """2026-07 인천 개편: 중구 -> 제물포구. 보정 없으면 -47% 가짜 급감이 나온다."""
    from kortrade import regions
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.sqlite")
        rows = []
        for i, p_ in enumerate(pd.period_range("2025-01", "2026-06", freq="M").astype(str)):
            rows.append(dict(period=p_, hs_code="330499", hs_name="x", sido_cd="28",
                             sido_name="인천광역시", sigungu_name="중구", exp_cnt=100,
                             exp_usd=(60 + i * 4) * 1_000_000, imp_cnt=0, imp_usd=0, bal_usd=0))
        for i, p_ in enumerate(["2026-07", "2026-08"]):
            rows.append(dict(period=p_, hs_code="330499", hs_name="x", sido_cd="28",
                             sido_name="인천광역시", sigungu_name="제물포구", exp_cnt=100,
                             exp_usd=(140 - 33 * i) * 1_000_000, imp_cnt=0, imp_usd=0, bal_usd=0))
        st.upsert_region(rows)

        raw = kpi.export_proxy(st, "중구", ["330499"], sido="인천광역시", canonical=False)
        assert raw.index[-1] == "2026-06", "보정 없으면 개편 시점에서 계열이 끊긴다"

        adj = kpi.export_proxy(st, "중구", ["330499"], sido="인천광역시")
        assert adj.index[-1] == "2026-08" and len(adj) == 20
        # 어느 이름으로 물어도 같은 계열이 나와야 한다
        assert adj.equals(kpi.export_proxy(st, "제물포구", ["330499"], sido="인천광역시"))

        # 다른 시도의 동명 시군구는 영향을 받으면 안 된다
        assert regions.canonical_aliases("서울특별시", "중구") == ["중구"]
        assert set(regions.canonical_aliases("인천광역시", "중구")) == {"중구", "제물포구"}

        found = regions.detect_boundary_changes(st, min_total_usd=1_000_000)
        assert any(r["사라진_시군구"] == "중구" and r["등장_시군구"] == "제물포구"
                   and r["확신도"] == "높음" for r in found), found

        df = analyze.region_monthly(st, sigungu="중구", sido="인천광역시")
        assert "2026-07" in (df.attrs.get("break_warning") or ""), df.attrs
        st.close()
    print("  ✓ 행정구역 개편 병합 + 자동 탐지 + 단절 경고")


def test_regions_config_is_evidence_based():
    """regions.yaml 의 모든 항목은 실측 근거(evidence)를 달아야 한다."""
    from kortrade import regions
    cfg = regions._cfg()
    for sido, entries in (cfg.get("sigungu_aliases") or {}).items():
        for name, spec in entries.items():
            assert spec.get("evidence"), f"{sido}/{name} 에 근거 없음"
            assert spec.get("effective", "").count("-") == 1
    for r in regions.sido_reorgs():
        assert r.get("evidence") and r.get("before") and r.get("after")
    assert regions.breaks(), "단절 구간이 하나도 등록돼 있지 않다"
    print("  ✓ 개편 매핑에 실측 근거 강제")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nkortrade 오프라인 검증 — {len(tests)}개\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"  ✗ {t.__name__}: {exc}")
            import traceback; traceback.print_exc()
    print(f"\n{'실패 ' + str(failed) if failed else '전부 통과'} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
