"""모멘텀 / 섹터 로테이션 분석.

핵심 지표
  yoy_3m   : 최근 3개월 합계의 전년 동기 대비 증감률.  월별 변동성과 조업일수 효과를 흡수한다.
  yoy_1m   : 최근 1개월 YoY. 가장 빠르지만 노이즈가 크다.
  accel    : yoy_3m - 직전 3개월 구간의 yoy_3m.  "좋아지는 속도가 빨라지는가" = 로테이션 신호.
  share    : 섹터/전체 내 비중과 그 변화.
  z12      : 최근 값이 과거 12개월 분포에서 몇 시그마인지.

주도 섹터 판단은 레벨(yoy)이 아니라 **가속(accel)** 과 **비중 변화** 를 같이 볼 때 유효하다.
높은 성장률이 이미 알려진 것이라면 가격에 반영되어 있고, 새로 꺾여 올라오는 것이 알파다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import regions
from .store import Store


# ------------------------------------------------------------------ 로드

def sector_monthly(store: Store, country: str | None = None) -> pd.DataFrame:
    """period x hs_code 월별 수출액. country=None 이면 국가 합산(ALL 제외 후 합산)."""
    if country:
        df = store.frame(
            "SELECT period, hs_code, hs_name, exp_usd FROM sector_trade WHERE country_code=?",
            (country,),
        )
    else:
        df = store.frame(
            "SELECT period, hs_code, MAX(hs_name) hs_name, SUM(exp_usd) exp_usd"
            " FROM sector_trade WHERE country_code<>'ALL'"
            " GROUP BY period, hs_code"
        )
    return _clean(df)


def region_monthly(store: Store, sigungu: str | None = None,
                   hs_code: str | None = None, sido: str | None = None,
                   canonical: bool = True) -> pd.DataFrame:
    """시군구 월별 수출.

    canonical=True(기본)이면 행정구역 개편으로 이름이 바뀐 시군구를 하나로 합쳐 조회한다.
    (예: '인천광역시 중구' 와 '제물포구' → 한 계열). regions.yaml 참조.
    원본 저장값은 바뀌지 않으며, 조회 시점에만 접는다.
    """
    sql = ("SELECT period, sido_name, sigungu_name, hs_code, MAX(hs_name) hs_name,"
           " SUM(exp_usd) exp_usd, SUM(exp_cnt) exp_cnt FROM region_trade WHERE 1=1")
    params: list = []
    if sigungu:
        names = regions.canonical_aliases(sido or "", sigungu) if canonical else [sigungu]
        sql += " AND sigungu_name IN (%s)" % ",".join("?" * len(names))
        params.extend(names)
    if sido:
        sql += " AND sido_name LIKE ?"
        params.append(sido[:2] + "%")
    if hs_code:
        sql += " AND hs_code=?"
        params.append(hs_code)
    sql += " GROUP BY period, sido_name, sigungu_name, hs_code"
    df = _clean(store.frame(sql, params))
    if canonical and not df.empty:
        df["sigungu_name"] = [
            regions.canonical_sigungu(s, g)
            for s, g in zip(df["sido_name"].fillna(""), df["sigungu_name"])
        ]
        df = (df.groupby(["period", "sido_name", "sigungu_name", "hs_code"], as_index=False)
                .agg(hs_name=("hs_name", "max"), exp_usd=("exp_usd", "sum"),
                     exp_cnt=("exp_cnt", "sum")))
        df.attrs["break_warning"] = regions.break_warning(sorted(df["period"].unique()), sido)
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["exp_usd"] = pd.to_numeric(df["exp_usd"], errors="coerce").fillna(0.0)
    df["period"] = df["period"].astype(str)
    return df.sort_values("period")


# ------------------------------------------------------------------ 지표

def _series(df: pd.DataFrame, key: str, value: str = "exp_usd") -> pd.DataFrame:
    """long -> wide(period x key) 매트릭스, 결측 월은 0 으로 채운다."""
    wide = df.pivot_table(index="period", columns=key, values=value, aggfunc="sum")
    if wide.empty:
        return wide
    full = pd.period_range(wide.index.min(), wide.index.max(), freq="M").astype(str)
    return wide.reindex(full).fillna(0.0).sort_index()


def momentum_table(df: pd.DataFrame, key: str = "hs_code",
                   labels: dict[str, str] | None = None) -> pd.DataFrame:
    """키별 모멘텀 요약표. 최신 월 기준."""
    wide = _series(df, key)
    if wide.empty or len(wide) < 15:
        return pd.DataFrame()

    r3 = wide.rolling(3).sum()
    r12 = wide.rolling(12).sum()

    last = wide.index[-1]
    prev_y = _shift_label(last, -12)
    if prev_y not in wide.index:
        return pd.DataFrame()

    def pct(a, b):
        return np.where(np.asarray(b) > 0, (np.asarray(a) / np.asarray(b) - 1.0) * 100, np.nan)

    yoy_1m = pct(wide.loc[last], wide.loc[prev_y])
    yoy_3m = pct(r3.loc[last], r3.loc[prev_y])
    yoy_12m = pct(r12.loc[last], r12.loc[prev_y])

    lab3 = _shift_label(last, -3)
    prev_y3 = _shift_label(lab3, -12)
    if lab3 in r3.index and prev_y3 in r3.index:
        yoy_3m_prev = pct(r3.loc[lab3], r3.loc[prev_y3])
        accel = yoy_3m - yoy_3m_prev
    else:
        accel = np.full(len(wide.columns), np.nan)

    hist = wide.iloc[-13:-1]
    z12 = (wide.loc[last] - hist.mean()) / hist.std(ddof=0).replace(0, np.nan)

    total_now = r3.loc[last].sum()
    total_prev = r3.loc[prev_y].sum()
    share_now = r3.loc[last] / total_now * 100 if total_now else np.nan
    share_prev = r3.loc[prev_y] / total_prev * 100 if total_prev else np.nan

    out = pd.DataFrame({
        key: wide.columns,
        "최근월": last,
        "수출_최근3M_USD": r3.loc[last].values,
        "YoY_1M_%": np.round(yoy_1m, 1),
        "YoY_3M_%": np.round(yoy_3m, 1),
        "YoY_12M_%": np.round(yoy_12m, 1),
        "가속_%p": np.round(accel, 1),
        "비중_%": np.round(np.asarray(share_now, dtype=float), 2),
        "비중변화_%p": np.round(np.asarray(share_now, dtype=float)
                            - np.asarray(share_prev, dtype=float), 2),
        "z12": np.round(z12.values.astype(float), 2),
    })
    if labels:
        out.insert(1, "품목명", out[key].map(labels))
    return out.sort_values("가속_%p", ascending=False, na_position="last").reset_index(drop=True)


def _shift_label(label: str, months: int) -> str:
    y, m = int(label[:4]), int(label[5:7])
    t = y * 12 + (m - 1) + months
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def rotation_score(table: pd.DataFrame, min_size_usd: float = 3_000_000) -> pd.DataFrame:
    """가속 + 비중변화 + z12 을 표준화해 합성한 로테이션 점수.

    min_size_usd 미만(최근 3개월 수출)은 노이즈이므로 제외한다.
    """
    if table.empty:
        return table
    t = table[table["수출_최근3M_USD"] >= min_size_usd].copy()
    if t.empty:
        return t

    def z(col):
        s = pd.to_numeric(t[col], errors="coerce")
        sd = s.std(ddof=0)
        return (s - s.mean()) / sd if sd and not np.isnan(sd) else s * 0.0

    t["로테이션점수"] = (0.45 * z("가속_%p") + 0.35 * z("비중변화_%p")
                    + 0.20 * z("z12")).round(2)
    return t.sort_values("로테이션점수", ascending=False).reset_index(drop=True)


def country_breakdown(store: Store, hs_code: str, months: int = 3) -> pd.DataFrame:
    """특정 HS 코드의 국가별 최근 모멘텀. 어느 시장이 끌고 있는지 본다."""
    df = store.frame(
        "SELECT period, country_code, MAX(country_name) country_name, SUM(exp_usd) exp_usd"
        " FROM sector_trade WHERE hs_code=? AND country_code<>'ALL'"
        " GROUP BY period, country_code",
        (hs_code,),
    )
    if df.empty:
        return df
    df = _clean(df)
    names = df.groupby("country_code")["country_name"].last().to_dict()
    tbl = momentum_table(df, key="country_code", labels=names)
    if not tbl.empty:
        tbl = tbl.rename(columns={"품목명": "국가명"})
    return tbl


def concentration(store: Store, hs_code: str, sido_name: str | None = None,
                  months: int = 12) -> pd.DataFrame:
    """HS 코드별 시군구 집중도. 신호 순도(purity) 판정의 1차 근거.

    HHI 가 높고 1위 시군구 비중이 크면 그 지역 데이터는 특정 기업 프록시로 쓸 수 있다.
    """
    sql = ("SELECT sigungu_name, sido_name, SUM(exp_usd) exp_usd, SUM(exp_cnt) exp_cnt"
           " FROM region_trade WHERE hs_code=?")
    params: list = [hs_code]
    if sido_name:
        sql += " AND sido_name=?"
        params.append(sido_name)
    sql += (" AND period >= (SELECT MAX(period) FROM region_trade) "
            " GROUP BY sigungu_name, sido_name")
    # 최근 N개월로 제한
    sql = sql.replace(
        "AND period >= (SELECT MAX(period) FROM region_trade)",
        "AND period >= (SELECT MIN(period) FROM (SELECT DISTINCT period FROM region_trade"
        f" ORDER BY period DESC LIMIT {int(months)}))",
    )
    df = store.frame(sql, params)
    if df.empty:
        return df
    df["exp_usd"] = pd.to_numeric(df["exp_usd"], errors="coerce").fillna(0.0)
    total = df["exp_usd"].sum()
    df["비중_%"] = (df["exp_usd"] / total * 100).round(2) if total else 0.0
    df = df.sort_values("exp_usd", ascending=False).reset_index(drop=True)
    df.attrs["HHI"] = round(float(((df["비중_%"] / 100) ** 2).sum()), 4)
    df.attrs["top1_share"] = float(df["비중_%"].iloc[0]) if len(df) else 0.0
    return df


def purity_grade(hhi: float, top1_share: float) -> str:
    """집중도로부터 신호 순도 등급을 제안한다 (최종 판단은 공장 소재지 확인 후)."""
    if top1_share >= 70 and hhi >= 0.5:
        return "A"
    if top1_share >= 40 or hhi >= 0.25:
        return "B"
    return "C"
