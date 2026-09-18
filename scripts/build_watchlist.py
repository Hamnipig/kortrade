#!/usr/bin/env python3
"""워치리스트 → site/data/watchlist.json  (API 호출 없음)

품목 하나당 계산하는 것:
  금액  최근 8개월 합계, YoY, 최근 3개월 YoY, 가속(= 3M YoY − 8M YoY)
  P     $/kg 와 그 YoY.  단가가 실적의 P 축이다.
  Q     중량(톤)과 그 YoY. 금액 증가가 단가 때문인지 물량 때문인지 가른다.
  국가  상위 국가별 금액·비중·YoY

**금액만 보면 틀린다.** 양극재처럼 단가가 반토막 나는데 물량이 늘어 금액이 유지되는
품목, 변압기처럼 물량은 제자리인데 단가가 뛰는 품목이 섞여 있다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import watchlist as W
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))

WINDOW = 8          # 비교창(개월)
RECENT = 3          # 최근 구간(개월)
HIST = 32           # 스파크라인에 싣는 월수
TOP_COUNTRIES = 5


def _shift(p: str, k: int) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def load_all(store: Store, codes: list[str]) -> pd.DataFrame:
    """sector_trade 에서 워치리스트가 쓰는 10단위만 꺼낸다."""
    if not codes:
        return pd.DataFrame()
    q = ("SELECT period, hs_code, country_code, SUM(exp_usd) usd, SUM(exp_wgt) kg"
         " FROM sector_trade WHERE hs_code IN (%s)"
         " GROUP BY period, hs_code, country_code" % ",".join("?" * len(codes)))
    df = store.frame(q, codes)
    if df.empty:
        return df
    for c in ("usd", "kg"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def build(store: Store, wl: W.Watchlist) -> dict | None:
    all_codes = sorted({c for i in wl.active() for c in i.hsk})
    df = load_all(store, all_codes)
    if df.empty:
        return None

    months = sorted(df["period"].unique())[-HIST:]
    df = df[df["period"].isin(months)]
    if len(months) < WINDOW + 12:
        # 전년 동기가 없으면 YoY 가 전부 None 이 된다. 경고만 남기고 계속한다.
        print(f"  ⚠ 월 수가 {len(months)}개뿐이라 YoY 가 비어 있을 수 있습니다")

    cur = months[-WINDOW:]
    prev = [_shift(p, -12) for p in cur]
    q3 = months[-RECENT:]
    q3p = [_shift(p, -12) for p in q3]

    def w(sub, ps, col):
        return float(sub[sub["period"].isin(ps)][col].sum())

    rows = []
    for it in wl.items:
        base = {
            "key": it.key, "name": it.name, "group": it.group,
            "hsk": it.hsk, "hs6": it.parents,
            "tickers": it.tickers, "idea": it.idea,
            "purity": it.purity, "status": it.status,
            "evidence": " ".join(it.evidence.split()),
        }
        if not it.active:
            rows.append({**base, "ready": False})
            continue

        sub = df[df["hs_code"].isin(it.hsk)]
        if sub.empty:
            rows.append({**base, "ready": False,
                         "evidence": base["evidence"] + " (수집 결과 없음)"})
            continue

        # 전국 합계는 country_code='ALL' 행. 국가 분해 행과 더하면 이중계상이 된다.
        tot = sub[sub["country_code"] == "ALL"]
        byc = sub[sub["country_code"] != "ALL"]
        if tot.empty:            # 합계를 못 받은 경우에만 국가 합으로 대체
            tot = byc

        sig = W.signals(
            w(tot, cur, "usd"), w(tot, prev, "usd"),
            w(tot, cur, "kg"), w(tot, prev, "kg"),
            w(tot, q3, "usd"), w(tot, q3p, "usd"),
            w(tot, q3, "kg"), w(tot, q3p, "kg"),
        )

        m_usd = tot.groupby("period")["usd"].sum().reindex(months, fill_value=0.0)
        m_kg = tot.groupby("period")["kg"].sum().reindex(months, fill_value=0.0)
        m_p = [(u / k) if k >= W.MIN_BASE_KG else None
               for u, k in zip(m_usd.tolist(), m_kg.tolist())]

        countries = []
        tot_c = w(byc, cur, "usd")
        for cc, g in byc.groupby("country_code"):
            c_usd, p_usd = w(g, cur, "usd"), w(g, prev, "usd")
            if c_usd <= 0:
                continue
            countries.append({
                "cc": cc, "name": wl.countries.get(cc, cc),
                "usd": round(c_usd / 1e6, 1),
                "share": round(c_usd / tot_c * 100, 1) if tot_c else None,
                "yoy": W.pct(c_usd, p_usd, W.MIN_BASE_USD / 3),
            })
        countries.sort(key=lambda r: -r["usd"])

        rows.append({**base, "ready": True, **sig,
                     "mUsd": [round(x / 1e6, 2) for x in m_usd.tolist()],
                     "mKg": [round(x / 1000, 1) for x in m_kg.tolist()],
                     "mP": [round(x, 2) if x else None for x in m_p],
                     "countries": countries[:TOP_COUNTRIES],
                     "covered": round(sum(c["usd"] for c in countries) / (sig["usd"] or 1) * 100)
                     if sig["usd"] else None})

    ready = [r for r in rows if r.get("ready")]
    if not ready:
        return None
    rows.sort(key=lambda r: (-(r.get("usd") or -1)))

    # ---- 상단 아이디어 신호 (사용자가 고른 3종) ----
    def top(pred, keyf, n=4):
        c = [r for r in ready if pred(r)]
        c.sort(key=keyf, reverse=True)
        return [r["key"] for r in c[:n]]

    highlights = {
        "accel": top(lambda r: r.get("accel") is not None, lambda r: r["accel"]),
        "qTurn": top(lambda r: r.get("qTurn"), lambda r: (r.get("qQ3") or 0)),
        "pTurn": top(lambda r: r.get("pTurn"), lambda r: (r.get("pQ3") or 0)),
        "decel": sorted([r["key"] for r in ready if (r.get("accel") or 0) < 0],
                        key=lambda k: next(r["accel"] for r in ready if r["key"] == k))[:4],
    }

    return {
        "asOf": months[-1], "months": months,
        "window": WINDOW, "recent": RECENT,
        "version": wl.version,
        "groups": [{"key": k, **v} for k, v in
                   sorted(wl.groups.items(), key=lambda kv: kv[1].get("order", 99))],
        "items": rows,
        "highlights": highlights,
        "builtAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default=str(SITE / "data"))
    args = ap.parse_args()

    wl = W.load()
    errs = wl.validate()
    if errs:
        print("워치리스트 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        payload = build(store, wl)

    if not payload:
        print("워치리스트 데이터가 없습니다. scripts/run_watchlist.py 를 먼저 실행하세요.")
        return 1

    (out / "watchlist.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n_ok = sum(1 for r in payload["items"] if r.get("ready"))
    print(f"watchlist.json — 품목 {n_ok}/{len(payload['items'])} · 기준월 {payload['asOf']}")
    for k, v in payload["highlights"].items():
        print(f"  {k:<6} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
