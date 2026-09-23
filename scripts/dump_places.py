#!/usr/bin/env python3
"""시군구 전체 덤프 — 진단용. **사이트 파일을 만들지 않는다.** (API 호출 없음)

왜 만들었나 (2026-09-23)
  화장품 대시보드는 카테고리당 **상위 8개 시군구**만 싣는다. 그래서
  "화성·음성·세종이 안 보인다"가

      (a) 순위 컷오프 밖이라 안 보이는 것인지
      (b) 애초에 그 지역으로 안 잡히는 것인지

  구분이 안 됐다. 둘은 완전히 다른 결론으로 이어진다 —
  (a)면 ODM 생산지가 작게라도 잡히므로 따로 추적할 값이 있고,
  (b)면 그 축은 버리고 다른 방법을 찾아야 한다.

  상위 8개 합이 기초(330499)의 **58%** 밖에 안 된다는 사실이 이 질문을 급하게 만들었다.
  나머지 42%가 어디에 있는지 모르는 채로 순위표를 읽고 있었던 것이다.

배경 — 지역 귀속 기준 (1차 출처 확인, 2026-09-23)
  공공데이터포털 「관세청_시군구별 품목별 수출입실적」 통계제공 기준:
    "수입은 「납세의무자 주소지 우편번호」, 수출은 「제조장소 우편번호」 기준으로 집계"
  한국무역협회 K-stat 통계가이드:
    수출은 "수출신고서 상 제조자의 사업장 소재지의 우편번호를 기준"

  → **제도상 기준은 공장 소재지가 맞다.** 그런데 실측은 강남구가 기초 1위($1,443M)다.
    강남구에 화장품 공장은 없다. 즉 기준의 문제가 아니라 **필드 품질**의 문제다 —
    제조자는 수출자가 신고서에 직접 적는 항목이고, 완제품을 매입해 수출하는
    브랜드사는 자기 주소를 넣는 쪽이 편하다. 그래서 규범은 공장, 실현값은 수출자다.

    이 차이는 실무적으로 중요하다. 제도가 막는 거라면 영구 불가지만,
    신고 품질 문제라면 **품목·기업마다 되는 데가 있고 안 되는 데가 있다.**
    화성이 색조에서만 1위로 뜨는 것이 그 증거다(색조 ODM 은 직수출 비중이 높다).
    이 스크립트는 그 '되는 데'를 찾기 위한 것이다.

사용법:
    python scripts/dump_places.py                        # 화장품 전 카테고리
    python scripts/dump_places.py --hs 330499 --top 40
    python scripts/dump_places.py --grep 화성,음성,세종,평택
    python scripts/dump_places.py --csv logs/places.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import regions
from kortrade.codes import canon_sido
from kortrade.sectors import get_sector
from kortrade.store import Store

WINDOW = 8


def _shift(p: str, k: int) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--sector", default="cosmetics")
    ap.add_argument("--hs", default=None, help="특정 HS6만 (기본: 섹터 전체)")
    ap.add_argument("--top", type=int, default=30, help="카테고리별 출력 행 수 (0=전체)")
    ap.add_argument("--grep", default="화성,음성,세종,평택,청주,오산",
                    help="쉼표구분. 순위와 무관하게 따로 찾아 보여줄 지역")
    ap.add_argument("--csv", default=None, help="전체를 CSV 로도 저장")
    args = ap.parse_args()

    sec = get_sector(args.sector)
    codes = [args.hs] if args.hs else sec.codes

    with Store(args.db) as store:
        df = store.frame(
            "SELECT period, hs_code, sido_name, sigungu_name, SUM(exp_usd) exp_usd,"
            " SUM(exp_cnt) exp_cnt FROM region_trade WHERE hs_code IN (%s)"
            " GROUP BY period, hs_code, sido_name, sigungu_name" % ",".join("?" * len(codes)),
            codes,
        )
    if df.empty:
        print("데이터가 없습니다. scripts/run_update.py --sectors 를 먼저 실행하세요.")
        return 1

    df["exp_usd"] = pd.to_numeric(df["exp_usd"], errors="coerce").fillna(0.0)
    df["exp_cnt"] = pd.to_numeric(df["exp_cnt"], errors="coerce").fillna(0.0)
    df["sigungu_name"] = [regions.canonical_sigungu(s or "", g)
                          for s, g in zip(df["sido_name"].fillna(""), df["sigungu_name"])]
    df["place"] = pd.Series([canon_sido(s) for s in df["sido_name"].fillna("")],
                            index=df.index) + " " + df["sigungu_name"]

    months = sorted(df["period"].unique())
    cur = months[-WINDOW:]
    prev = [_shift(p, -12) for p in cur]
    print(f"DB 월 범위 {months[0]} ~ {months[-1]} · 비교창 {cur[0]}~{cur[-1]} vs 전년 동기\n")

    keys = [k.strip() for k in args.grep.split(",") if k.strip()]
    rows_all = []

    for hs in codes:
        sub = df[df["hs_code"] == hs]
        if sub.empty:
            print(f"── {hs} {sec.label(hs)}: 데이터 없음\n")
            continue
        g = sub[sub["period"].isin(cur)].groupby("place").agg(
            usd=("exp_usd", "sum"), cnt=("exp_cnt", "sum"))
        p = sub[sub["period"].isin(prev)].groupby("place")["exp_usd"].sum()
        g["prev"] = p.reindex(g.index).fillna(0.0)
        g = g.sort_values("usd", ascending=False)
        tot = float(g["usd"].sum())

        print(f"── {hs} {sec.label(hs)} — 시군구 {len(g)}곳 · 합계 ${tot/1e6:,.1f}M")
        head = g if args.top == 0 else g.head(args.top)
        cum = 0.0
        for i, (place, r) in enumerate(head.iterrows(), 1):
            cum += r["usd"]
            yoy = f"{(r['usd']/r['prev']-1)*100:+7.1f}%" if r["prev"] > 0 else "      –"
            # 건당 금액 — 신고 주체의 성격을 가른다. 브랜드사 대량 선적과
            # 소량 다건(벤더·역직구)은 건당 금액이 자릿수로 다르다.
            per = r["usd"] / r["cnt"] if r["cnt"] else 0.0
            print(f"   {i:3d}. {place:<18} ${r['usd']/1e6:9,.1f}M {yoy}"
                  f"  누적 {cum/tot*100:5.1f}%  건수 {int(r['cnt']):>7,}"
                  f"  건당 ${per/1e3:,.1f}K")
        if args.top and len(g) > args.top:
            rest = float(g["usd"].iloc[args.top:].sum())
            print(f"   ... 나머지 {len(g)-args.top}곳 ${rest/1e6:,.1f}M ({rest/tot*100:.1f}%)")

        # ── 순위와 무관하게 찾는 지역 ─────────────────────────────────
        print(f"   [지정 조회] {', '.join(keys)}")
        for k in keys:
            hits = [pl for pl in g.index if k in pl]
            if not hits:
                print(f"     · {k:<6} 해당 시군구 없음 — 이 카테고리에서 전혀 잡히지 않음")
                continue
            u = float(g.loc[hits, "usd"].sum()); pv = float(g.loc[hits, "prev"].sum())
            rank = min(list(g.index).index(h) + 1 for h in hits)
            yoy = f"{(u/pv-1)*100:+.1f}%" if pv > 0 else "–"
            print(f"     · {k:<6} ${u/1e6:,.1f}M  {rank}위/{len(g)}  "
                  f"비중 {u/tot*100:.2f}%  YoY {yoy}   [{' · '.join(hits)}]")
        print()

        if args.csv:
            t = g.reset_index(); t.insert(0, "hs", hs); t.insert(1, "name", sec.label(hs))
            rows_all.append(t)

    if args.csv and rows_all:
        out = Path(args.csv); out.parent.mkdir(parents=True, exist_ok=True)
        pd.concat(rows_all).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"CSV 저장 → {out}")

    print("읽는 법 — '해당 시군구 없음'이면 그 지역으로 아예 귀속되지 않는다는 뜻이고,")
    print("순위만 밖이면 규모가 작을 뿐 잡히기는 한다는 뜻이다. 둘은 다른 결론으로 간다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
