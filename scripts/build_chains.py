#!/usr/bin/env python3
"""밸류체인 → site/data/chains.json  (API 호출 없음)

체인마다:
  전체(ALL)   완제품/부품/소재/장비 단계별 금액·YoY, 체인 합계, 현지화지수
  시장별      config 의 markets 각각에 대해 같은 계산 (現地化는 시장 단위로 일어난다)
  월별        완제품·상류·현지화지수 시계열 (차트용)

현지화는 '어느 나라에 공장을 지었나'의 문제라 **국가별로 봐야** 보인다.
전체 합계만 보면 미국 현지화가 유럽 수출 증가에 묻힌다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import chains as C
from kortrade import watchlist as W
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))

WINDOW = 8
RECENT = 3
HIST = 26


def _shift(p: str, k: int) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def build(store: Store, cs: C.Chains) -> dict | None:
    codes = cs.all_codes()
    if not codes:
        return None
    q = ("SELECT period, hs_code, country_code, SUM(exp_usd) usd"
         " FROM sector_trade WHERE hs_code IN (%s)"
         " GROUP BY period, hs_code, country_code" % ",".join("?" * len(codes)))
    df = store.frame(q, codes)
    if df.empty:
        return None
    df["usd"] = pd.to_numeric(df["usd"], errors="coerce").fillna(0.0)

    months = sorted(df["period"].unique())[-HIST:]
    df = df[df["period"].isin(months)]
    cur = months[-WINDOW:]
    prev = [_shift(p, -12) for p in cur]
    q3 = months[-RECENT:]
    q3p = [_shift(p, -12) for p in q3]

    out = []
    for ch in cs.chains:
        # 시장 = 'ALL'(전체) + config 의 markets
        views = []
        for mkt in ["ALL"] + list(ch.markets):
            sub = df[df["country_code"] == mkt]
            if sub.empty:
                continue

            def s(stage, ps):
                cc = ch.codes(stage)
                if not cc:
                    return 0.0
                return float(sub[sub["hs_code"].isin(cc) & sub["period"].isin(ps)]["usd"].sum())

            st_now = {k: s(k, cur) for k in C.STAGES}
            st_prev = {k: s(k, prev) for k in C.STAGES}
            up_now = sum(st_now[k] for k in ("component", "material"))
            up_prev = sum(st_prev[k] for k in ("component", "material"))
            tot_now = sum(st_now[k] for k in C.DEMAND_STAGES)
            tot_prev = sum(st_prev[k] for k in C.DEMAND_STAGES)
            if tot_now < 5_000_000 and tot_prev < 5_000_000:
                continue

            loc_now = C.localization(st_now["final"], up_now)
            loc_prev = C.localization(st_prev["final"], up_prev)
            f_yoy = W.pct(st_now["final"], st_prev["final"], W.MIN_BASE_USD)
            t_yoy = W.pct(tot_now, tot_prev, W.MIN_BASE_USD)

            # 최근 3개월 기준 현지화지수 — 전환 초기를 잡으려면 이쪽이 필요하다
            def s3(stage, ps):
                cc = ch.codes(stage)
                if not cc:
                    return 0.0
                return float(sub[sub["hs_code"].isin(cc) & sub["period"].isin(ps)]["usd"].sum())
            up_q3 = sum(s3(k, q3) for k in ("component", "material"))
            up_q3p = sum(s3(k, q3p) for k in ("component", "material"))
            loc_q3 = C.localization(s3("final", q3), up_q3)
            loc_q3p = C.localization(s3("final", q3p), up_q3p)

            def mser(stage):
                cc = ch.codes(stage)
                if not cc:
                    return [0.0] * len(months)
                g = (sub[sub["hs_code"].isin(cc)].groupby("period")["usd"].sum()
                     .reindex(months, fill_value=0.0))
                return [round(x / 1e6, 1) for x in g.tolist()]

            mf, mc, mm, me = (mser(k) for k in C.STAGES)
            mu = [round(a + b, 1) for a, b in zip(mc, mm)]
            mloc = [round(u / f, 2) if f > 0 else None for u, f in zip(mu, mf)]

            views.append({
                "market": mkt,
                "stages": {k: round(st_now[k] / 1e6, 1) for k in C.STAGES},
                "stagesYoy": {k: W.pct(st_now[k], st_prev[k], W.MIN_BASE_USD)
                              for k in C.STAGES},
                "final": round(st_now["final"] / 1e6, 1),
                "upstream": round(up_now / 1e6, 1),
                "total": round(tot_now / 1e6, 1),
                "totalPrev": round(tot_prev / 1e6, 1),
                "finalYoy": f_yoy, "totalYoy": t_yoy,
                "upstreamYoy": W.pct(up_now, up_prev, W.MIN_BASE_USD),
                "loc": loc_now, "locPrev": loc_prev,
                "locQ3": loc_q3, "locQ3Prev": loc_q3p,
                "verdict": C.verdict(f_yoy, t_yoy, loc_q3, loc_q3p),
                # 완제품 **품목별** 판정. 체인 합계는 성장이어도 특정 품목만
                # 줄어드는 경우가 있다(실측: ESS셀 −13% / 체인 +13%).
                # 워치리스트 배지는 이 품목별 판정을 쓴다.
                "finals": [{
                    "code": c, "label": ch.label(c),
                    "usd": round(float(sub[(sub["hs_code"] == c)
                                           & sub["period"].isin(cur)]["usd"].sum()) / 1e6, 1),
                    "yoy": W.pct(float(sub[(sub["hs_code"] == c)
                                           & sub["period"].isin(cur)]["usd"].sum()),
                                 float(sub[(sub["hs_code"] == c)
                                           & sub["period"].isin(prev)]["usd"].sum()),
                                 W.MIN_BASE_USD),
                    "verdict": C.verdict(
                        W.pct(float(sub[(sub["hs_code"] == c)
                                        & sub["period"].isin(cur)]["usd"].sum()),
                              float(sub[(sub["hs_code"] == c)
                                        & sub["period"].isin(prev)]["usd"].sum()),
                              W.MIN_BASE_USD),
                        t_yoy, loc_q3, loc_q3p),
                } for c in ch.codes("final")],
                "mFinal": mf, "mUpstream": mu, "mEquip": me, "mLoc": mloc,
            })

        if not views:
            continue
        items = [{"code": c, "label": ch.label(c), "stage": ch.stage_of(c)}
                 for c in ch.codes()]
        out.append({
            "key": ch.key, "name": ch.name, "thesis": ch.thesis.strip(),
            "evidence": " ".join(ch.evidence.split()),
            "markets": ch.markets, "items": items, "views": views,
        })

    if not out:
        return None
    return {"asOf": months[-1], "months": months,
            "window": WINDOW, "recent": RECENT,
            "version": cs.version, "chains": out,
            "builtAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default=str(SITE / "data"))
    args = ap.parse_args()

    cs = C.load()
    errs = cs.validate()
    if errs:
        print("체인 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        payload = build(store, cs)
    if not payload:
        print("체인 데이터가 없습니다. scripts/run_watchlist.py 를 먼저 실행하세요.")
        return 1
    (out / "chains.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"chains.json — 체인 {len(payload['chains'])}개 · 기준월 {payload['asOf']}")
    for ch in payload["chains"]:
        for v in ch["views"]:
            print(f"  {ch['name']:<8} {v['market']:<4} 완제품 {v['finalYoy']}% / "
                  f"체인 {v['totalYoy']}% / 현지화(3M) {v['locQ3Prev']}→{v['locQ3']} "
                  f"[{v['verdict']['label']}]")
            for fi in v["finals"]:
                if fi["verdict"]["code"] in ("localizing", "mixed"):
                    print(f"       └ {fi['label']} {fi['yoy']}% → [{fi['verdict']['label']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
