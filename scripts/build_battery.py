#!/usr/bin/env python3
"""2차전지·ESS 심화 → site/data/battery.json  (API 호출 없음)

세 가지를 낸다.

  1. 마진 프록시 — 양극재 판가(P)를 리튬 수입단가(C)에 회귀하고 **잔차**를 본다.
     시차 L 은 가정하지 않고 0~4개월을 훑어 R² 로 고른다. 잔차가 벌어지면
     원가로 설명되지 않는 판가, 즉 마진이 확대되는 중이다.
  2. 단계별 가속·턴어라운드 — 완제품/부품/소재/장비 각각의 8개월 YoY 와
     최근 3개월 YoY 를 비교한다. '가속'과 '턴어라운드'를 따로 말한다.
  3. 선후행 구조 — 장비 → 소재 → 완제품의 시차 상관을 재서 지금 무엇이
     끌고 있는지 보여준다.

draft 코드(사용자 제시 미검증분)는 **금액과 단가를 측정해서 따로** 보여준다.
검증 전에는 합계에 넣지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import battery as B
from kortrade import flash as F
from kortrade import watchlist as W
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))
M = 1_000_000.0


def _m(v):
    return None if v is None else round(v / M, 1)


def _r(v, n=2):
    return None if v is None else round(v, n)


def build(store: Store, cfg: B.BatteryConfig) -> dict | None:
    codes = cfg.all_codes()
    df = store.frame(
        "SELECT period, hs_code, country_code,"
        " SUM(exp_usd) eu, SUM(exp_wgt) ew, SUM(imp_usd) iu, SUM(imp_wgt) iw"
        " FROM sector_trade WHERE hs_code IN (%s)"
        " GROUP BY period, hs_code, country_code" % ",".join("?" * len(codes)), codes)
    if df.empty:
        return None
    for c in ("eu", "ew", "iu", "iw"):
        df[c] = df[c].astype(float).fillna(0.0)

    months = sorted(p for p in df["period"].unique() if F.split_period(p))[-cfg.hist:]
    if len(months) < 6:
        return None
    df = df[df["period"].isin(months)]
    nat = df[df["country_code"] == "ALL"]
    latest = months[-1]
    cur = months[-cfg.window:]
    prev = [F.shift(p, -12) for p in cur]
    q3 = months[-cfg.recent:]
    q3p = [F.shift(p, -12) for p in q3]

    def agg(frame, cs, ps, col):
        return float(frame[frame["hs_code"].isin(cs) & frame["period"].isin(ps)][col].sum())

    def series(frame, cs, col) -> dict[str, float]:
        g = frame[frame["hs_code"].isin(cs)].groupby("period")[col].sum()
        return {p: float(v) for p, v in g.items() if v}

    # ── 1. 원가(C) · 판가(P) · 마진 프록시 ────────────────────────────────
    price_codes = [c.code for c in cfg.price]
    p_usd, p_wgt = series(nat, price_codes, "eu"), series(nat, price_codes, "ew")
    P = {p: B.unit_price(p_usd.get(p), p_wgt.get(p)) for p in months}
    P = {p: v for p, v in P.items() if v}

    costs, best = [], None
    for c in cfg.cost:
        iu, iw = series(nat, [c.code], "iu"), series(nat, [c.code], "iw")
        C = {p: B.unit_price(iu.get(p), iw.get(p)) for p in months}
        C = {p: v for p, v in C.items() if v}
        cser = [_r(C.get(p), 2) for p in months]
        # 최근 3개월 리튬 단가 모멘텀 — 재고평가 효과의 선행 신호
        c_now, c_3m = C.get(months[-1]), C.get(months[-4]) if len(months) >= 4 else None
        fit = B.best_lag(P, C, F.shift, cfg.max_lag_months, cfg.min_months) if P and C else None
        rec = {
            "code": c.code, "label": c.label, "for": c.for_, "status": c.status,
            "note": c.note,
            "impUsd": _m(agg(nat, [c.code], cur, "iu")),
            "impYoy": W.pct(agg(nat, [c.code], cur, "iu"), agg(nat, [c.code], prev, "iu"), 0),
            "price": _r(c_now), "priceSeries": cser,
            "priceMom3": W.pct(c_now, c_3m, 0),
            "lag": None if not fit else fit["lag"],
            "beta": None if not fit else _r(fit["fit"]["beta"], 3),
            "r2": None if not fit else _r(fit["fit"]["r2"], 3),
            "n": None if not fit else fit["fit"]["n"],
            "lagCurve": None if not fit else fit["curve"],
            "diffR2": None if not fit else fit["diffR2"],
            "lagIdentified": None if not fit else fit["identified"],
        }
        costs.append(rec)
        if fit and fit["fit"]["r2"] >= cfg.min_r2 and (
                best is None or fit["fit"]["r2"] > best["fit"]["fit"]["r2"]):
            best = {"cost": c, "fit": fit, "C": C}

    # 잔차 e(t) = P(t) − (α + β·C(t−L))  ← 원가로 설명되지 않는 판가 = 마진 프록시
    spread = {"linked": False, "note": "리튬 수입단가와 양극재 판가의 연동이 "
                                       "확인되지 않아 마진 방향을 내지 않습니다."}
    if best:
        L, fit = best["fit"]["lag"], best["fit"]["fit"]
        e = {}
        for p in months:
            cp = F.shift(p, -L)
            if p in P and cp in best["C"]:
                e[p] = P[p] - (fit["alpha"] + fit["beta"] * best["C"][cp])
        ks = sorted(e)
        e_now = e.get(ks[-1]) if ks else None
        e_3m = e.get(ks[-4]) if len(ks) >= 4 else None
        cm = next(c for c in costs if c["code"] == best["cost"].code)
        spread = {
            "linked": True,
            "costCode": best["cost"].code, "costLabel": best["cost"].label,
            "lag": L, "beta": _r(fit["beta"], 3), "r2": _r(fit["r2"], 3), "n": fit["n"],
            "diffR2": best["fit"]["diffR2"], "lagIdentified": best["fit"]["identified"],
            "lagCurve": best["fit"]["curve"],
            "pNow": _r(P.get(ks[-1]) if ks else None), "cNow": cm["price"],
            "eNow": _r(e_now), "e3m": _r(e_3m),
            "series": [_r(e.get(p)) for p in months],
            "pSeries": [_r(P.get(p)) for p in months],
            "verdict": B.margin_verdict(e_now, e_3m, cm["priceMom3"]),
            "note": f"양극재 판가(P)를 {best['cost'].label} 수입단가(C)에 회귀했습니다. "
                    f"시차 {L}개월 · 전가율 β={_r(fit['beta'], 3)} · 수준 R²={_r(fit['r2'], 3)} "
                    f"(표본 {fit['n']}개월). β 는 가정이 아니라 실측값입니다. "
                    + ("시차는 차분 회귀로 식별했습니다."
                       if best["fit"]["identified"] else
                       "다만 시차별 차분 R² 곡선이 평탄해 **시차 자체는 식별되지 않았습니다** — "
                       "원가 시계열이 매끄러우면 어느 시차나 비슷하게 맞습니다. "
                       "시차 값은 참고로만 보세요."),
        }

    # ── 2. 단계별 가속·턴어라운드 (active 코드만) ────────────────────────
    stages, tot_now, tot_prev, tot_q3, tot_q3p = [], 0.0, 0.0, 0.0, 0.0
    for st in B.STAGES:
        cs = cfg.stage_codes(st, active_only=True)
        if not cs:
            continue
        n8, p8 = agg(nat, cs, cur, "eu"), agg(nat, cs, prev, "eu")
        n3, p3 = agg(nat, cs, q3, "eu"), agg(nat, cs, q3p, "eu")
        yoy = W.pct(n8, p8, W.MIN_BASE_USD)
        r3 = W.pct(n3, p3, W.MIN_BASE_USD)
        if st in B.DEMAND_STAGES:
            tot_now += n8; tot_prev += p8; tot_q3 += n3; tot_q3p += p3
        items = []
        for c in cfg.stages.get(st, []):
            if not c.active:
                continue
            iu, ip = agg(nat, [c.code], cur, "eu"), agg(nat, [c.code], prev, "eu")
            i3, i3p = agg(nat, [c.code], q3, "eu"), agg(nat, [c.code], q3p, "eu")
            iy, ir = W.pct(iu, ip, W.MIN_BASE_USD), W.pct(i3, i3p, W.MIN_BASE_USD)
            g = nat[nat["hs_code"] == c.code].groupby("period")["eu"].sum()
            items.append({
                "code": c.code, "label": c.label, "purity": c.purity,
                "evidence": c.evidence,
                "usd": _m(iu), "yoy": iy, "recentYoy": ir,
                "accel": None if (iy is None or ir is None) else _r(ir - iy, 1),
                "verdict": B.verdict(iy, ir),
                "m": [_r(float(g.get(p, 0.0)) / M, 1) for p in months],
            })
        stages.append({
            "stage": st, "usd": _m(n8), "yoy": yoy, "recentYoy": r3,
            "accel": None if (yoy is None or r3 is None) else _r(r3 - yoy, 1),
            "verdict": B.verdict(yoy, r3), "items": items,
            "m": [_r(float(nat[nat["hs_code"].isin(cs) & (nat["period"] == p)]["eu"].sum()) / M, 1)
                  for p in months],
        })

    t_yoy = W.pct(tot_now, tot_prev, W.MIN_BASE_USD)
    t_r3 = W.pct(tot_q3, tot_q3p, W.MIN_BASE_USD)
    total = {"usd": _m(tot_now), "yoy": t_yoy, "recentYoy": t_r3,
             "accel": None if (t_yoy is None or t_r3 is None) else _r(t_r3 - t_yoy, 1),
             "verdict": B.verdict(t_yoy, t_r3)}

    # ── 3. 선후행 — 지금 사이클을 끌고 있는 단계는 어디인가 ───────────────
    def stage_series(st):
        cs = cfg.stage_codes(st, active_only=True)
        g = nat[nat["hs_code"].isin(cs)].groupby("period")["eu"].sum()
        return [float(g.get(p, 0.0)) for p in months]

    leadlag = []
    for lead, lag_st in (("equipment", "material"), ("material", "final"),
                         ("equipment", "final"), ("component", "final")):
        a, b = B.growth(stage_series(lead)), B.growth(stage_series(lag_st))
        rows = []
        for k in range(cfg.leadlag_max_months + 1):
            xs = [(a[i], b[i + k]) for i in range(len(a) - k)
                  if a[i] is not None and b[i + k] is not None]
            r = B.corr([p[0] for p in xs], [p[1] for p in xs]) if len(xs) >= 8 else None
            rows.append({"k": k, "r": _r(r, 3), "n": len(xs)})
        ok = [x for x in rows if x["r"] is not None]
        top = max(ok, key=lambda x: x["r"]) if ok else None
        leadlag.append({"lead": lead, "lag": lag_st, "rows": rows,
                        "bestK": None if not top else top["k"],
                        "bestR": None if not top else top["r"]})

    # ── 4. 시장별 (현지화는 국가 단위로 일어난다) ────────────────────────
    mkts = []
    for mk in cfg.markets:
        sub = df[df["country_code"] == mk]
        if sub.empty:
            continue
        fin = cfg.stage_codes("final", True)
        up = cfg.stage_codes("component", True) + cfg.stage_codes("material", True)
        eq = cfg.stage_codes("equipment", True)
        f_n, f_p = agg(sub, fin, cur, "eu"), agg(sub, fin, prev, "eu")
        u_n, u_p = agg(sub, up, cur, "eu"), agg(sub, up, prev, "eu")
        e_n, e_p = agg(sub, eq, cur, "eu"), agg(sub, eq, prev, "eu")
        if f_n + u_n < 5_000_000:
            continue
        ess = agg(sub, ["8507603000"], cur, "eu")
        ess_p = agg(sub, ["8507603000"], prev, "eu")
        mkts.append({
            "market": mk,
            "final": _m(f_n), "finalYoy": W.pct(f_n, f_p, W.MIN_BASE_USD),
            "upstream": _m(u_n), "upstreamYoy": W.pct(u_n, u_p, W.MIN_BASE_USD),
            "equip": _m(e_n), "equipYoy": W.pct(e_n, e_p, W.MIN_BASE_USD),
            "loc": _r(u_n / f_n, 2) if f_n > 0 else None,
            "locPrev": _r(u_p / f_p, 2) if f_p > 0 else None,
            "ess": _m(ess), "essYoy": W.pct(ess, ess_p, W.MIN_BASE_USD),
        })
    mkts.sort(key=lambda r: -(r["final"] or 0))

    # ── 5. draft 코드 측정 — 채택할지 말지의 근거를 숫자로 ────────────────
    anchor = nat[nat["hs_code"] == "8479899050"].groupby("period")["eu"].sum()
    anchor_g = B.growth([float(anchor.get(p, 0.0)) for p in months])
    draft = []
    for c in cfg.drafts():
        is_cost = any(x.code == c.code for x in cfg.cost)
        col_u, col_w = ("iu", "iw") if is_cost else ("eu", "ew")
        u, w = agg(nat, [c.code], cur, col_u), agg(nat, [c.code], cur, col_w)
        up = agg(nat, [c.code], prev, col_u)
        us = agg(df[df["country_code"] == "US"], [c.code], cur, col_u)
        g = nat[nat["hs_code"] == c.code].groupby("period")[col_u].sum()
        gs = B.growth([float(g.get(p, 0.0)) for p in months])
        xs = [(a, b) for a, b in zip(anchor_g, gs) if a is not None and b is not None]
        r_anchor = B.corr([p[0] for p in xs], [p[1] for p in xs]) if len(xs) >= 8 else None
        draft.append({
            "code": c.code, "label": c.label, "side": "수입" if is_cost else "수출",
            "purity": c.purity, "note": c.note or c.evidence,
            "usd": _m(u), "yoy": W.pct(u, up, 0),
            "unitPrice": _r(B.unit_price(u, w)),
            "usShare": None if u <= 0 else _r(us / u * 100, 1),
            "rWithCoater": _r(r_anchor, 3),
            "seen": u > 0,
        })
    draft.sort(key=lambda r: -(r["usd"] or 0))

    return {
        "version": cfg.version,
        "builtAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "asOf": latest, "months": months,
        "window": cfg.window, "recent": cfg.recent,
        "total": total, "stages": stages, "spread": spread, "costs": costs,
        "leadlag": leadlag, "markets": mkts, "drafts": draft,
        "rejected": cfg.rejected,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default=str(SITE / "data"))
    args = ap.parse_args()

    cfg = B.load()
    errs = cfg.validate()
    if errs:
        print("배터리 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        payload = build(store, cfg)
    if not payload:
        print("배터리 데이터가 없습니다. scripts/run_watchlist.py 를 먼저 실행하세요.")
        return 1
    (out / "battery.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    t = payload["total"]
    print(f"battery.json — 기준월 {payload['asOf']}")
    print(f"  체인 전체 ${t['usd']}M · {payload['window']}M YoY {t['yoy']}% · "
          f"최근 {payload['recent']}M {t['recentYoy']}% · 가속 {t['accel']}%p "
          f"[{t['verdict']['label']}]")
    for s in payload["stages"]:
        print(f"  {s['stage']:<10} ${str(s['usd']):>8}M  YoY {str(s['yoy']):>7}% → "
              f"{str(s['recentYoy']):>7}%  [{s['verdict']['label']}]")
        for i in s["items"]:
            print(f"     {i['label']:<18} ${str(i['usd']):>8}M {str(i['yoy']):>7}% → "
                  f"{str(i['recentYoy']):>7}%  [{i['verdict']['label']}]")
    sp = payload["spread"]
    if sp["linked"]:
        print(f"  마진 프록시 — P ${sp['pNow']}/kg vs {sp['costLabel']} ${sp['cNow']}/kg, "
              f"시차 {sp['lag']}M{'(식별됨)' if sp['lagIdentified'] else '(미식별)'} · "
              f"β={sp['beta']} · 수준R²={sp['r2']} / 차분R²={sp['diffR2']} (n={sp['n']})")
        print(f"     잔차 {sp['e3m']} → {sp['eNow']} $/kg  [{sp['verdict']['label']}]")
    else:
        print(f"  마진 프록시 — {sp['note']}")
    for ll in payload["leadlag"]:
        print(f"  선후행 {ll['lead']}→{ll['lag']}: 최적 시차 {ll['bestK']}개월 r={ll['bestR']}")
    print("  draft 코드 측정:")
    for d in payload["drafts"]:
        print(f"     {d['label']:<26} {d['side']} ${str(d['usd']):>8}M "
              f"단가 ${str(d['unitPrice']):>8}/kg 對미 {d['usShare']}% "
              f"코터상관 {d['rWithCoater']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
