#!/usr/bin/env python3
"""유니버스 → site/data/universe.json  (API 호출 없음)

목적은 "23개 워치리스트 밖에서 뭐가 새로 뜨는가"다. 1,200개 항을 다 보여주는 건
불가능하니, 가속·물량·단가로 정렬해 상·하위만 띄우고 나머지는 검색으로 찾게 한다.

각 항(HS4)에 대해:
  금액  최근 8개월, YoY, 최근 3개월 YoY, 가속(3M YoY − 8M YoY)
  Q     중량 YoY   (실물 물량)
  P     $/kg 와 YoY (단가)
워치리스트와 같은 정의를 쓰므로 두 화면의 숫자가 서로 어긋나지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import watchlist as W
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))

WINDOW = 8
RECENT = 3
HIST = 26
TOP_N = 25                    # 랭킹 표에 띄울 상·하위 개수
MIN_USD = 20_000_000          # 비교창 $20M 미만은 랭킹에서 제외 (잡음)


def _shift(p: str, k: int) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def build(store: Store) -> dict | None:
    df = store.frame("SELECT period, hs4, hs2, top_name, exp_usd usd, exp_wgt kg"
                     " FROM universe_trade")
    if df.empty:
        return None
    for c in ("usd", "kg"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    chapters = yaml.safe_load(
        (ROOT / "config" / "hs_chapters.yaml").read_text(encoding="utf-8"))["chapters"]
    chapters = {str(k).zfill(2): v for k, v in chapters.items()}

    months = sorted(df["period"].unique())[-HIST:]
    cur = months[-WINDOW:]
    prev = [_shift(p, -12) for p in cur]
    q3 = months[-RECENT:]
    q3p = [_shift(p, -12) for p in q3]
    need = set(cur + prev + q3 + q3p)
    d = df[df["period"].isin(need | set(months))]

    # 워치리스트가 이미 10단위로 보고 있는 항은 표시만 해 둔다(중복 추적 방지)
    wl = W.load()
    watched = {c[:4] for i in wl.active() for c in i.hsk}

    def w(sub, ps, col):
        return float(sub[sub["period"].isin(ps)][col].sum())

    rows = []
    for hs4, g in d.groupby("hs4"):
        c_usd, p_usd = w(g, cur, "usd"), w(g, prev, "usd")
        if c_usd < MIN_USD and p_usd < MIN_USD:
            continue
        c_kg, p_kg = w(g, cur, "kg"), w(g, prev, "kg")
        q_usd, qp_usd = w(g, q3, "usd"), w(g, q3p, "usd")
        q_kg, qp_kg = w(g, q3, "kg"), w(g, q3p, "kg")
        sig = W.signals(c_usd, p_usd, c_kg, p_kg, q_usd, qp_usd, q_kg, qp_kg)
        m = g.groupby("period")["usd"].sum().reindex(months, fill_value=0.0)
        hs2 = hs4[:2]
        name = (g.sort_values("usd", ascending=False)["top_name"].dropna().head(1).tolist()
                or [""])[0]
        rows.append({
            "hs4": hs4, "hs2": hs2, "chapter": chapters.get(hs2, hs2),
            "label": name, "watched": hs4 in watched,
            **sig,
            "m": [round(x / 1e6, 1) for x in m.tolist()],
        })

    if not rows:
        return None
    rows.sort(key=lambda r: -(r["usd"] or 0))

    have = [r for r in rows if r.get("accel") is not None]
    up = sorted(have, key=lambda r: -r["accel"])[:TOP_N]
    down = sorted(have, key=lambda r: r["accel"])[:TOP_N]
    qturn = sorted([r for r in rows if r.get("qTurn")],
                   key=lambda r: -(r.get("qQ3") or 0))[:TOP_N]
    pturn = sorted([r for r in rows if r.get("pTurn")],
                   key=lambda r: -(r.get("pQ3") or 0))[:TOP_N]

    # 장 단위 롤업 — 대분류에서 어디가 움직이는지
    ch_rows = []
    for hs2, g in d.groupby("hs2"):
        c_usd, p_usd = w(g, cur, "usd"), w(g, prev, "usd")
        if c_usd < MIN_USD:
            continue
        sig = W.signals(c_usd, p_usd, w(g, cur, "kg"), w(g, prev, "kg"),
                        w(g, q3, "usd"), w(g, q3p, "usd"),
                        w(g, q3, "kg"), w(g, q3p, "kg"))
        ch_rows.append({"hs2": hs2, "chapter": chapters.get(hs2, hs2), **sig})
    ch_rows.sort(key=lambda r: -(r["usd"] or 0))

    total_cur = sum(r["usd"] for r in ch_rows)
    return {
        "asOf": months[-1], "months": months,
        "window": WINDOW, "recent": RECENT,
        "minUsd": MIN_USD / 1e6,
        "totals": {"usd": round(total_cur, 1),
                   "nHs4": len(rows), "nChapters": len(ch_rows)},
        "chapters": ch_rows,
        "items": rows,
        "rank": {"accelUp": [r["hs4"] for r in up], "accelDown": [r["hs4"] for r in down],
                 "qTurn": [r["hs4"] for r in qturn], "pTurn": [r["hs4"] for r in pturn]},
        "builtAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default=str(SITE / "data"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        payload = build(store)
    if not payload:
        print("유니버스 데이터가 없습니다. scripts/run_universe.py 를 먼저 실행하세요.")
        return 1
    (out / "universe.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    t = payload["totals"]
    print(f"universe.json — 항 {t['nHs4']}개 / 장 {t['nChapters']}개 · "
          f"기준월 {payload['asOf']} · 최근 {payload['window']}개월 ${t['usd']:,.0f}M")
    for k, v in payload["rank"].items():
        print(f"  {k:<10} {v[:6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
