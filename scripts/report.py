#!/usr/bin/env python3
"""수집된 데이터로 섹터 로테이션 / 기업 프록시 리포트를 뽑는다. (API 호출 없음)

사용법:
    python scripts/report.py rotation                       # 섹터 로테이션 랭킹
    python scripts/report.py country --hs 330499            # 330499 국가별 모멘텀
    python scripts/report.py region --sigungu 강릉시         # 강릉시 HS별 수출
    python scripts/report.py purity --hs 330499 --sigungu 강릉시
    python scripts/report.py leadlag --company 파마리서치 --metric 매출액
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import analyze, kpi
from kortrade.collect import hs_labels, load_companies, load_hs_config
from kortrade.discover import purity_report
from kortrade.store import Store

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)
pd.set_option("display.unicode.east_asian_width", True)


def cmd_rotation(store: Store, args) -> None:
    cfg = load_hs_config()
    labels = hs_labels(cfg)
    df = analyze.sector_monthly(store, country=args.country)
    if df.empty:
        print("데이터 없음. run_update.py 를 먼저 실행하세요.")
        return
    tbl = analyze.momentum_table(df, key="hs_code", labels=labels)
    scored = analyze.rotation_score(tbl, min_size_usd=args.min_size)
    if scored.empty:
        print("유효 표본 없음 (수집 기간이 15개월 미만이거나 규모 필터에 전부 걸림)")
        return
    title = f"섹터 로테이션 랭킹 ({args.country or '전세계'} / 기준월 {scored['최근월'].iloc[0]})"
    print(f"\n{title}\n" + "=" * len(title) * 2)
    cols = ["hs_code", "품목명", "수출_최근3M_USD", "YoY_3M_%", "가속_%p",
            "비중_%", "비중변화_%p", "z12", "로테이션점수"]
    print(scored[cols].to_string(index=False))
    print("\n해석: 가속(%p)이 양수이고 비중변화가 플러스인 품목이 '새로 강해지는' 축입니다.")


def cmd_country(store: Store, args) -> None:
    tbl = analyze.country_breakdown(store, args.hs)
    if tbl.empty:
        print("데이터 없음")
        return
    print(f"\nHS {args.hs} 국가별 모멘텀")
    cols = ["country_code", "국가명", "수출_최근3M_USD", "YoY_3M_%", "가속_%p",
            "비중_%", "비중변화_%p"]
    print(tbl[[c for c in cols if c in tbl.columns]].head(args.top).to_string(index=False))


def cmd_region(store: Store, args) -> None:
    df = analyze.region_monthly(store, sigungu=args.sigungu)
    if df.empty:
        print("데이터 없음")
        return
    cfg = load_hs_config()
    tbl = analyze.momentum_table(df, key="hs_code", labels=hs_labels(cfg))
    print(f"\n{args.sigungu} HS별 수출 모멘텀")
    if tbl.empty:
        piv = df.pivot_table(index="period", columns="hs_code", values="exp_usd", aggfunc="sum")
        print("(모멘텀 계산에 15개월 이상 필요) 원시 시계열 최근 12개월:")
        print(piv.tail(12).to_string())
        return
    print(tbl.to_string(index=False))


def cmd_purity(store: Store, args) -> None:
    rep = purity_report(store, args.hs, args.sigungu)
    print(f"\n신호 순도 진단: HS {args.hs} x {args.sigungu}")
    for k, v in rep.items():
        print(f"  {k:>20} : {v}")


def cmd_leadlag(store: Store, args) -> None:
    comp = load_companies()["companies"].get(args.company)
    if not comp:
        print(f"companies.yaml 에 '{args.company}' 가 없습니다.")
        return
    proxy = kpi.export_proxy(store, comp["sigungu"], comp["hs_watch"], comp.get("sido"))
    if proxy.empty:
        print("프록시 데이터 없음")
        return
    kdf = store.frame("SELECT * FROM company_kpi WHERE company=?", (args.company,))
    if kdf.empty:
        print(f"'{args.company}' 의 KPI 가 없습니다. "
              f"kpi.load_kpi_csv() 로 분기 실적을 먼저 적재하세요.")
        print(f"\n(참고) 프록시 최근 12개월:\n{proxy.tail(12).to_string()}")
        return

    aligned = kpi.align(proxy, kdf, metric=args.metric)
    print(f"\n{args.company} — 프록시({comp['sigungu']} / {comp['hs_watch']}) vs {args.metric}")
    print(aligned.round(1).to_string())

    ll = kpi.leadlag(aligned)
    print("\n선행성 검증 (YoY 기준)")
    print(ll.to_string(index=False))

    lag = kpi.best_lag(ll)
    if lag is None:
        print("\n⚠ 어떤 lag 에서도 검증 기준(n>=8, R2>=0.3)을 통과하지 못했습니다.")
        print("  이 프록시는 아직 실적 예측에 쓸 근거가 없습니다. HS 코드 조합을 재탐색하세요.")
        return
    print(f"\n채택 lag = {lag}분기")
    last_q = kpi.to_quarter(proxy.index[-1])
    nc = kpi.nowcast_kpi(aligned, proxy, last_q, lag=lag)
    print(f"\n{last_q} 나우캐스트")
    for k, v in nc.items():
        print(f"  {k:>22} : {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("rotation"); p.add_argument("--country", default=None)
    p.add_argument("--min-size", type=float, default=3_000_000); p.set_defaults(fn=cmd_rotation)

    p = sub.add_parser("country"); p.add_argument("--hs", required=True)
    p.add_argument("--top", type=int, default=15); p.set_defaults(fn=cmd_country)

    p = sub.add_parser("region"); p.add_argument("--sigungu", required=True)
    p.set_defaults(fn=cmd_region)

    p = sub.add_parser("purity"); p.add_argument("--hs", required=True)
    p.add_argument("--sigungu", required=True); p.set_defaults(fn=cmd_purity)

    p = sub.add_parser("leadlag"); p.add_argument("--company", required=True)
    p.add_argument("--metric", default="매출액"); p.set_defaults(fn=cmd_leadlag)

    args = ap.parse_args()
    with Store(args.db) as store:
        args.fn(store, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
