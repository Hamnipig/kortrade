"""역탐색: 신호가 실제로 어느 HS 코드 / 어느 시군구에 실려 있는지 찾는다.

왜 필요한가
  기업이 무엇을 만드는지 안다고 해서 그것이 어느 HS 코드로 수출신고되는지는 알 수 없다.
  리쥬란은 '조직수복용생체재료'(의료기기)이지 화장품이 아니므로 HS 3304 에 없을 수 있고,
  ODM 생산품은 브랜드사가 수출신고하면 제조장소가 아닌 다른 지역으로 잡힐 수도 있다.
  그래서 후보 HS 코드를 넓게 스캔한 뒤, 금액이 실제로 존재하는 코드를 데이터로 확정한다.

두 방향
  scan_sigungu : 지역을 고정하고 HS 후보를 훑는다  ("강릉시에서 뭐가 나가나")
  scan_hs      : HS 를 고정하고 지역 순위를 본다   ("330499 는 어디서 나가나")
"""

from __future__ import annotations

import logging

import pandas as pd

from .analyze import concentration, purity_grade
from .client import CustomsAPIError
from .codes import sido_code_for
from .collect import Collector
from .store import Store

log = logging.getLogger(__name__)


def scan_sigungu(collector: Collector, sido_name: str, sigungu_name: str,
                 candidate_hs: list[str], start: str, end: str) -> pd.DataFrame:
    """특정 시군구에서 후보 HS 코드별 수출액을 스캔해 큰 것부터 보여준다.

    반환: hs_code | hs_name | 수출액합계 | 관측월수 | 최근12M | 최근12M_YoY_%
    """
    store = collector.store
    rows = []
    for hs in candidate_hs:
        if len(hs) != 6:
            continue
        try:
            collector.collect_region(sido_name, [hs], start, end)
        except CustomsAPIError as exc:
            if exc.fatal:
                raise
            log.warning("스캔 실패 %s: %s", hs, exc)
            continue

    df = store.frame(
        "SELECT hs_code, MAX(hs_name) hs_name, period, SUM(exp_usd) exp_usd"
        " FROM region_trade WHERE sido_name=? AND sigungu_name=? AND hs_code IN (%s)"
        " GROUP BY hs_code, period" % ",".join("?" * len(candidate_hs)),
        [sido_name, sigungu_name, *candidate_hs],
    )
    if df.empty:
        return df

    df["exp_usd"] = pd.to_numeric(df["exp_usd"], errors="coerce").fillna(0.0)
    periods = sorted(df["period"].unique())
    last12 = set(periods[-12:])
    prev12 = set(periods[-24:-12])

    for hs, g in df.groupby("hs_code"):
        cur = g.loc[g["period"].isin(last12), "exp_usd"].sum()
        prv = g.loc[g["period"].isin(prev12), "exp_usd"].sum()
        rows.append({
            "hs_code": hs,
            "hs_name": g["hs_name"].dropna().iloc[0] if g["hs_name"].notna().any() else "",
            "수출액합계_USD": int(g["exp_usd"].sum()),
            "관측월수": int((g["exp_usd"] > 0).sum()),
            "최근12M_USD": int(cur),
            "최근12M_YoY_%": round((cur / prv - 1) * 100, 1) if prv > 0 else None,
        })
    return (pd.DataFrame(rows)
            .sort_values("최근12M_USD", ascending=False)
            .reset_index(drop=True))


def scan_hs(collector: Collector, hs_code: str, sido_names: list[str],
            start: str, end: str, top_n: int = 20) -> pd.DataFrame:
    """HS 코드 하나에 대해 여러 시도를 훑어 시군구 수출 순위를 만든다."""
    for sido in sido_names:
        try:
            collector.collect_region(sido, [hs_code], start, end)
        except CustomsAPIError as exc:
            if exc.fatal:
                raise
            log.warning("스캔 실패 %s/%s: %s", sido, hs_code, exc)

    df = concentration(collector.store, hs_code, months=12)
    if df.empty:
        return df
    df = df.head(top_n)
    df.attrs["purity_hint"] = purity_grade(df.attrs.get("HHI", 0.0),
                                           df.attrs.get("top1_share", 0.0))
    return df


def purity_report(store: Store, hs_code: str, sigungu_name: str,
                  months: int = 12) -> dict:
    """특정 (HS, 시군구) 조합의 신호 순도 진단."""
    conc = concentration(store, hs_code, months=months)
    if conc.empty:
        return {"status": "no_data"}
    row = conc[conc["sigungu_name"] == sigungu_name]
    share = float(row["비중_%"].iloc[0]) if len(row) else 0.0
    rank = int(row.index[0]) + 1 if len(row) else -1

    ts = store.frame(
        "SELECT period, SUM(exp_usd) exp_usd, SUM(exp_cnt) exp_cnt FROM region_trade"
        " WHERE hs_code=? AND sigungu_name=? GROUP BY period ORDER BY period",
        (hs_code, sigungu_name),
    )
    ts["exp_usd"] = pd.to_numeric(ts["exp_usd"], errors="coerce").fillna(0.0)

    # 신고건수가 많을수록 여러 주체가 섞여 있을 가능성이 높다
    med_cnt = float(pd.to_numeric(ts["exp_cnt"], errors="coerce").median() or 0)

    return {
        "hs_code": hs_code,
        "sigungu": sigungu_name,
        "전국내_비중_%": round(share, 2),
        "전국_순위": rank,
        "HHI": conc.attrs.get("HHI"),
        "1위_시군구": conc["sigungu_name"].iloc[0],
        "1위_비중_%": conc.attrs.get("top1_share"),
        "월평균_수출신고건수": med_cnt,
        "관측월수": int((ts["exp_usd"] > 0).sum()),
        "제안등급": purity_grade(conc.attrs.get("HHI", 0.0), share),
        "해석": _interpret(share, med_cnt),
    }


def _interpret(share: float, med_cnt: float) -> str:
    if share >= 70 and med_cnt <= 60:
        return "단일 주체 가능성 높음 — 금액 레벨을 기업 실적 프록시로 사용 가능"
    if share >= 40:
        return "소수 주체 혼재 — 방향성(YoY/가속)은 유효, 레벨 귀속은 주의"
    if med_cnt > 300:
        return "신고건수가 많아 다수 주체 혼재 — 기업 귀속 불가, 지역 총계로만 사용"
    return "비중이 낮음 — 기업 프록시로 부적합. 다른 HS 코드를 탐색할 것"
