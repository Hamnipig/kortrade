"""수출 프록시 ↔ 실적 KPI 연결.

세 가지를 한다.
  1. align()     월별 수출 프록시를 분기로 집계해 분기 실적과 같은 축에 올린다.
  2. leadlag()   lag 0/1/2 분기에서 상관·회귀를 구해 선행성이 실제로 있는지 검증한다.
  3. nowcast()   진행 중인 분기의 부분 수출 데이터로 분기 매출을 추정한다.

선행성 검증 없이 쓰지 말 것. 프록시는 "그럴듯해서" 쓰는 게 아니라
과거 구간에서 통계적으로 성립했기 때문에 쓴다. R²와 표본수를 항상 같이 본다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import regions
from .store import Store


def to_quarter(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


def quarter_progress(period: str) -> int:
    """분기 내 몇 번째 달인지 (1~3)."""
    return (int(period[5:7]) - 1) % 3 + 1


# ------------------------------------------------------------------ 프록시 구성

def export_proxy(store: Store, sigungu: str, hs_codes: list[str],
                 sido: str | None = None, baseline_usd: float = 0.0,
                 canonical: bool = True) -> pd.Series:
    """월별 수출 프록시 시계열 (index='YYYY-MM', value=USD).

    baseline_usd: 같은 (시군구, HS6)에 섞여 있는 **타사/타제품의 월간 상수 성분**.
      관세청 시군구 API 는 HS 6단위까지만 조회되므로, 목표 기업이 실제로는 10단위 중
      한 코드에만 실려 있어도 나머지 형제 코드가 함께 잡힌다. 그 혼입분이 시간에 대해
      대체로 일정하다면 상수로 빼는 것이 레벨 정확도를 크게 개선한다.
      값은 estimate_baseline() 으로 구하거나 companies.yaml 의 baseline_usd_month 에 둔다.
    """
    # 행정구역 개편으로 이름이 바뀐 경우 개편 전후를 한 계열로 묶는다 (regions.yaml)
    names = regions.canonical_aliases(sido or "", sigungu) if canonical else [sigungu]
    sql = ("SELECT period, SUM(exp_usd) v FROM region_trade"
           " WHERE sigungu_name IN (%s) AND hs_code IN (%s)"
           % (",".join("?" * len(names)), ",".join("?" * len(hs_codes))))
    params: list = [*names, *hs_codes]
    if sido:
        sql += " AND sido_name LIKE ?"
        params.append(sido[:2] + "%")
    sql += " GROUP BY period ORDER BY period"
    df = store.frame(sql, params)
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(pd.to_numeric(df["v"], errors="coerce").fillna(0.0).values,
                  index=df["period"].astype(str)).sort_index()
    if baseline_usd:
        s = (s - float(baseline_usd)).clip(lower=0.0)
    return s


def estimate_baseline(monthly: pd.Series, before: str) -> dict:
    """구조적 변곡 이전 구간의 월평균을 '혼입 상수'로 추정한다.

    before: 'YYYY-MM'. 이 시점 **이전** 을 목표 기업의 기여가 거의 없던 구간으로 본다.
      (강릉/파마리서치의 경우 필러 수출이 본격화되기 전 = '2023-01')

    변곡 이전 구간이 실제로 평평한지 확인하지 않고 쓰면 추세를 상수로 오인하므로,
    변동계수(cv)를 함께 돌려준다. cv 가 크면 상수 가정이 성립하지 않는다.
    """
    pre = monthly[monthly.index < before]
    if len(pre) < 6:
        return {"status": "insufficient", "n": len(pre)}
    mean, sd = float(pre.mean()), float(pre.std(ddof=0))
    cv = sd / mean if mean else float("nan")
    return {
        "status": "ok", "n": len(pre), "window": f"{pre.index[0]}~{pre.index[-1]}",
        "baseline_usd_month": round(mean, 0),
        "sd_usd": round(sd, 0),
        "cv": round(cv, 2),
        "판정": "상수 가정 타당" if cv < 0.8 else "변동이 커서 상수 가정 부적절 — 그대로 쓰지 말 것",
    }


def sector_proxy(store: Store, hs_codes: list[str],
                 countries: list[str] | None = None) -> pd.Series:
    sql = ("SELECT period, SUM(exp_usd) v FROM sector_trade"
           " WHERE hs_code IN (%s) AND country_code <> 'ALL'" % ",".join("?" * len(hs_codes)))
    params: list = list(hs_codes)
    if countries:
        sql += " AND country_code IN (%s)" % ",".join("?" * len(countries))
        params += countries
    sql += " GROUP BY period ORDER BY period"
    df = store.frame(sql, params)
    if df.empty:
        return pd.Series(dtype=float)
    return pd.Series(pd.to_numeric(df["v"], errors="coerce").fillna(0.0).values,
                     index=df["period"].astype(str)).sort_index()


# ------------------------------------------------------------------ 정렬 / 검증

def align(monthly: pd.Series, kpi: pd.DataFrame,
          metric: str = "매출액") -> pd.DataFrame:
    """월별 프록시를 분기로 합산해 KPI 와 조인. 완결된 분기(3개월 모두 존재)만 남긴다."""
    if monthly.empty:
        return pd.DataFrame()
    q = monthly.groupby(monthly.index.map(to_quarter))
    agg = pd.DataFrame({"proxy_usd": q.sum(), "months": q.size()})
    agg = agg[agg["months"] == 3].drop(columns="months")

    k = kpi[kpi["metric"] == metric][["period", "value"]].rename(
        columns={"period": "quarter", "value": "kpi"}
    ).set_index("quarter")

    out = agg.join(k, how="inner").sort_index()
    out["proxy_yoy_%"] = out["proxy_usd"].pct_change(4) * 100
    out["kpi_yoy_%"] = out["kpi"].pct_change(4) * 100
    return out


def leadlag(aligned: pd.DataFrame, max_lag: int = 3,
            on: str = "yoy") -> pd.DataFrame:
    """프록시를 k분기 앞세웠을 때의 상관·회귀. on='yoy' 권장(레벨은 추세 때문에 과대평가됨)."""
    if aligned.empty:
        return pd.DataFrame()
    if on == "yoy":
        x_all, y_all = aligned["proxy_yoy_%"], aligned["kpi_yoy_%"]
    else:
        x_all, y_all = aligned["proxy_usd"], aligned["kpi"]

    rows = []
    for lag in range(0, max_lag + 1):
        # lag=1 → 이번 분기 프록시가 다음 분기 실적을 설명
        x = x_all.shift(lag)
        pair = pd.concat([x, y_all], axis=1).dropna()
        pair.columns = ["x", "y"]
        n = len(pair)
        if n < 6:
            rows.append({"lag_분기": lag, "n": n, "corr": None, "R2": None,
                         "beta": None, "alpha": None})
            continue
        corr = float(pair["x"].corr(pair["y"]))
        beta, alpha = np.polyfit(pair["x"], pair["y"], 1)
        pred = beta * pair["x"] + alpha
        ss_res = float(((pair["y"] - pred) ** 2).sum())
        ss_tot = float(((pair["y"] - pair["y"].mean()) ** 2).sum())
        rows.append({
            "lag_분기": lag, "n": n,
            "corr": round(corr, 3),
            "R2": round(1 - ss_res / ss_tot, 3) if ss_tot else None,
            "beta": round(float(beta), 3),
            "alpha": round(float(alpha), 2),
        })
    df = pd.DataFrame(rows)
    df.attrs["basis"] = on
    return df


def best_lag(ll: pd.DataFrame, min_n: int = 8, min_r2: float = 0.3) -> int | None:
    """검증 기준을 통과한 lag 중 R²가 가장 높은 것. 통과 못하면 None (= 쓰지 말 것)."""
    if ll.empty:
        return None
    ok = ll.dropna(subset=["R2"])
    ok = ok[(ok["n"] >= min_n) & (ok["R2"] >= min_r2)]
    if ok.empty:
        return None
    return int(ok.sort_values("R2", ascending=False)["lag_분기"].iloc[0])


# ------------------------------------------------------------------ 나우캐스팅

def quarter_run_rate(monthly: pd.Series, quarter: str,
                     lookback_years: int = 3) -> dict:
    """진행 중인 분기의 부분 데이터로 분기 수출 총액을 추정한다.

    과거 같은 분기에서 '1개월차까지의 누적 / 분기 전체' 비율의 중앙값을 진척률로 쓴다.
    (조업일수·선적 스케줄의 계절성을 흡수하기 위해 단순 n/3 을 쓰지 않는다.)
    """
    if monthly.empty:
        return {"status": "no_data"}
    qs = monthly.index.map(to_quarter)
    cur = monthly[qs == quarter]
    if cur.empty:
        return {"status": "no_data", "quarter": quarter}
    n_months = len(cur)
    if n_months >= 3:
        return {"status": "complete", "quarter": quarter,
                "estimate_usd": float(cur.sum()), "months_in": 3, "progress_ratio": 1.0}

    qnum = int(quarter[-1])
    year = int(quarter[:4])
    ratios = []
    for back in range(1, lookback_years + 1):
        hq = f"{year - back}-Q{qnum}"
        h = monthly[monthly.index.map(to_quarter) == hq]
        if len(h) == 3 and h.sum() > 0:
            ratios.append(float(h.iloc[:n_months].sum() / h.sum()))
    if not ratios:
        ratio = n_months / 3
        basis = "fallback(n/3)"
    else:
        ratio = float(np.median(ratios))
        basis = f"median of {len(ratios)}y same-quarter"

    return {
        "status": "partial", "quarter": quarter, "months_in": n_months,
        "actual_usd": float(cur.sum()),
        "progress_ratio": round(ratio, 4),
        "estimate_usd": round(float(cur.sum()) / ratio, 0) if ratio else None,
        "basis": basis,
    }


def nowcast_kpi(aligned: pd.DataFrame, monthly: pd.Series, quarter: str,
                lag: int = 0, min_n: int = 8) -> dict:
    """프록시 추정치 + 회귀계수로 분기 KPI 를 추정한다.

    lag=0 : 같은 분기 프록시로 같은 분기 실적 설명 (동행)
    lag=1 : 이번 분기 프록시로 다음 분기 실적 설명 (선행)
    """
    rr = quarter_run_rate(monthly, quarter)
    if rr.get("estimate_usd") is None:
        return {"status": "no_proxy", **rr}

    hist = aligned.dropna(subset=["proxy_yoy_%", "kpi_yoy_%"])
    if lag:
        hist = hist.assign(x=hist["proxy_yoy_%"].shift(lag)).dropna(subset=["x"])
    else:
        hist = hist.assign(x=hist["proxy_yoy_%"])
    if len(hist) < min_n:
        return {"status": "insufficient_history", "n": len(hist), **rr}

    beta, alpha = np.polyfit(hist["x"], hist["kpi_yoy_%"], 1)

    # 추정 분기의 프록시 YoY
    prev_q = _shift_quarter(quarter, -4)
    prev = aligned["proxy_usd"].get(prev_q)
    if prev is None or prev <= 0:
        return {"status": "no_base_quarter", "base": prev_q, **rr}
    proxy_yoy = (rr["estimate_usd"] / float(prev) - 1) * 100

    kpi_yoy_hat = beta * proxy_yoy + alpha
    base_kpi = aligned["kpi"].get(prev_q if lag == 0 else _shift_quarter(quarter, -4))

    resid = hist["kpi_yoy_%"] - (beta * hist["x"] + alpha)
    se = float(resid.std(ddof=2))

    return {
        "status": "ok",
        "quarter": quarter,
        "months_in": rr["months_in"],
        "proxy_estimate_usd": rr["estimate_usd"],
        "proxy_yoy_%": round(proxy_yoy, 1),
        "kpi_yoy_hat_%": round(float(kpi_yoy_hat), 1),
        "kpi_hat": round(float(base_kpi) * (1 + kpi_yoy_hat / 100), 1)
                   if base_kpi is not None else None,
        "±1σ_%p": round(se, 1),
        "n_history": len(hist),
        "lag_분기": lag,
        "progress_basis": rr.get("basis"),
    }


def _shift_quarter(q: str, k: int) -> str:
    y, n = int(q[:4]), int(q[-1])
    t = y * 4 + (n - 1) + k
    return f"{t // 4}-Q{t % 4 + 1}"


# ------------------------------------------------------------------ KPI 입력

def load_kpi_csv(store: Store, path: str, company: str,
                 source: str = "manual") -> int:
    """분기 실적 CSV 를 적재.  컬럼: period(YYYY-Qn), metric, value[, unit]"""
    df = pd.read_csv(path)
    need = {"period", "metric", "value"}
    if not need.issubset(df.columns):
        raise ValueError(f"CSV 컬럼 부족: {need - set(df.columns)}")
    rows = [{"company": company, "period": str(r["period"]).strip(),
             "metric": str(r["metric"]).strip(), "value": float(r["value"]),
             "unit": str(r.get("unit", "억원")), "source": source}
            for _, r in df.iterrows()]
    return store.upsert_kpi(rows)
