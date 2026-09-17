#!/usr/bin/env python3
"""카테고리 로테이션 + 비기초 전문 시군구 탐지. (수집된 DB만 사용, API 호출 없음)

두 가지를 본다.

1) 카테고리 로테이션 — 화장품 하위 카테고리 중 어디가 비중을 가져가고 어디가 뺏기는가.
   레벨(YoY)이 아니라 **비중변화**와 **가속**이 판단 기준이다.

2) 비기초 전문 시군구 — 기초(330499)는 전국 수출의 ~88%라 거의 모든 시군구에 깔려 있다.
   그래서 기초를 빼고 봤을 때 특정 카테고리에 집중된 시군구가 곧 **전문 기업 후보**다.
   '주력집중도'(비기초 수출 중 1위 카테고리 비중)가 높을수록 단일 기업일 가능성이 높다.

사용법:
    python scripts/sector_scan.py rotation
    python scripts/sector_scan.py specialists --min-usd 5000000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import regions
from kortrade.collect import hs_labels, load_hs_config
from kortrade.store import Store

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)
pd.set_option("display.unicode.east_asian_width", True)

BASIC = "330499"   # 기초화장품. 전국 수출의 대부분을 차지해 별도 취급한다.


def _window_sums(df: pd.DataFrame, cur_end: str, months: int) -> pd.DataFrame:
    """최근 N개월과 전년 동기 합계를 구한다."""
    periods = sorted(df["period"].unique())
    cur = [p for p in periods if p <= cur_end][-months:]
    prev = [_shift(p, -12) for p in cur]
    a = df[df["period"].isin(cur)].groupby(["key"], as_index=False)["exp_usd"].sum()
    b = df[df["period"].isin(prev)].groupby(["key"], as_index=False)["exp_usd"].sum()
    return a.merge(b, on="key", how="outer", suffixes=("_cur", "_prev")).fillna(0.0)


def _shift(p: str, k: int) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _load(store: Store) -> pd.DataFrame:
    df = store.frame(
        "SELECT period, hs_code, sido_name, sigungu_name, SUM(exp_usd) exp_usd"
        " FROM region_trade GROUP BY period, hs_code, sido_name, sigungu_name"
    )
    if df.empty:
        return df
    df["exp_usd"] = pd.to_numeric(df["exp_usd"], errors="coerce").fillna(0.0)
    # 행정구역 개편으로 이름이 바뀐 시군구를 하나로 접는다
    df["sigungu_name"] = [regions.canonical_sigungu(s or "", g)
                          for s, g in zip(df["sido_name"].fillna(""), df["sigungu_name"])]
    df["place"] = df["sido_name"].fillna("").str[:2] + " " + df["sigungu_name"]
    return df


def _pct(cur, prev):
    return round((cur / prev - 1) * 100, 0) if prev > 0 else None


def cmd_rotation(store: Store, args) -> None:
    df = _load(store)
    if df.empty:
        print("데이터 없음. run_update.py 를 먼저 실행하세요.")
        return
    labels = hs_labels(load_hs_config())
    end = df["period"].max()

    df["key"] = df["hs_code"]
    ytd = _window_sums(df, end, args.ytd_months)
    q3 = _window_sums(df, end, 3)

    tot_c, tot_p = ytd["exp_usd_cur"].sum(), ytd["exp_usd_prev"].sum()
    rows = []
    for _, r in ytd.iterrows():
        hs = r["key"]
        q = q3[q3["key"] == hs]
        q_c = float(q["exp_usd_cur"].iloc[0]) if len(q) else 0.0
        q_p = float(q["exp_usd_prev"].iloc[0]) if len(q) else 0.0
        ytd_yoy, q_yoy = _pct(r["exp_usd_cur"], r["exp_usd_prev"]), _pct(q_c, q_p)
        rows.append({
            "hs": hs, "카테고리": labels.get(hs, "")[:14],
            f"{args.ytd_months}M_백만$": round(r["exp_usd_cur"] / 1e6),
            "비중_%": round(r["exp_usd_cur"] / tot_c * 100, 1) if tot_c else None,
            "비중변화_%p": round((r["exp_usd_cur"] / tot_c - r["exp_usd_prev"] / tot_p) * 100, 2)
                          if tot_c and tot_p else None,
            "YoY_%": ytd_yoy, "3M_YoY_%": q_yoy,
            "가속_%p": (q_yoy - ytd_yoy) if (q_yoy is not None and ytd_yoy is not None) else None,
        })
    out = pd.DataFrame(rows).sort_values(f"{args.ytd_months}M_백만$", ascending=False)
    print(f"\n■ 카테고리 로테이션  (기준월 {end}, 최근 {args.ytd_months}개월)")
    print(f"  전체 {out[f'{args.ytd_months}M_백만$'].sum():,.0f} 백만$\n")
    print(out.to_string(index=False))
    print("\n  비중변화가 플러스이면서 가속도 플러스인 카테고리가 '새로 강해지는' 축입니다.")
    w = regions.break_warning(sorted(df["period"].unique()))
    if w:
        print(f"  {w}")


def cmd_specialists(store: Store, args) -> None:
    df = _load(store)
    if df.empty:
        print("데이터 없음.")
        return
    labels = hs_labels(load_hs_config())
    end = df["period"].max()

    df["key"] = df["place"] + "|" + df["hs_code"]
    ytd = _window_sums(df, end, args.ytd_months)
    q3 = _window_sums(df, end, 3)
    q3i = q3.set_index("key")

    ytd[["place", "hs"]] = ytd["key"].str.split("|", expand=True)
    rows = []
    for place, g in ytd.groupby("place"):
        total = g["exp_usd_cur"].sum()
        nb = g[g["hs"] != BASIC]
        nb_total = nb["exp_usd_cur"].sum()
        if nb_total < args.min_usd:
            continue
        top = nb.sort_values("exp_usd_cur", ascending=False).iloc[0]
        key = top["key"]
        q_c = float(q3i.loc[key, "exp_usd_cur"]) if key in q3i.index else 0.0
        q_p = float(q3i.loc[key, "exp_usd_prev"]) if key in q3i.index else 0.0
        ytd_yoy, q_yoy = _pct(top["exp_usd_cur"], top["exp_usd_prev"]), _pct(q_c, q_p)
        rows.append({
            "시군구": place, "주력": labels.get(top["hs"], top["hs"])[:10], "hs": top["hs"],
            "주력_백만$": round(top["exp_usd_cur"] / 1e6, 1),
            "비기초비중_%": round(nb_total / total * 100) if total else None,
            "주력집중도_%": round(top["exp_usd_cur"] / nb_total * 100) if nb_total else None,
            "YoY_%": ytd_yoy, "3M_YoY_%": q_yoy,
            "가속_%p": (q_yoy - ytd_yoy) if (q_yoy is not None and ytd_yoy is not None) else None,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        print("조건을 만족하는 시군구 없음. --min-usd 를 낮춰보세요.")
        return
    focused = out[out["주력집중도_%"] >= args.min_focus]

    print(f"\n■ 비기초 전문 시군구  (기준월 {end}, 주력집중도 ≥{args.min_focus}%)")
    print("  기초(330499)는 전국의 ~88%라 어디에나 깔려 있다. 기초를 뺀 뒤 한 카테고리에")
    print("  집중된 시군구가 전문 기업 후보다.\n")
    print("[가속 상위]")
    print(focused.sort_values("가속_%p", ascending=False, na_position="last")
          .head(args.top).to_string(index=False))
    print("\n[비기초 규모 상위]")
    print(out.sort_values("주력_백만$", ascending=False).head(args.top).to_string(index=False))
    print("\n  주력집중도가 높고 규모가 적당한 곳일수록 단일 기업일 가능성이 높습니다.")
    print("  후보를 찾으면 report.py purity 로 순도를 확인하세요.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--ytd-months", type=int, default=8, help="비교 창 길이(개월)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("rotation"); p.set_defaults(fn=cmd_rotation)

    p = sub.add_parser("specialists")
    p.add_argument("--min-usd", type=float, default=5_000_000, help="비기초 수출 하한")
    p.add_argument("--min-focus", type=int, default=50, help="주력집중도 하한(%)")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(fn=cmd_specialists)

    args = ap.parse_args()
    with Store(args.db) as store:
        args.fn(store, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
