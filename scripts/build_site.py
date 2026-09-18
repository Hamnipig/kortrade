#!/usr/bin/env python3
"""수집된 DB → 정적 사이트용 JSON 생성. (API 호출 없음)

site/data/manifest.json   섹터 목록 + 갱신 시각 + 커버리지
site/data/<key>.json      섹터별 대시보드 페이로드

site/index.html 은 고정 파일이고, 이 스크립트는 data/*.json 만 갈아끼운다.
따라서 GitHub Actions 는 수집 → 이 스크립트 → 커밋만 하면 Pages 가 알아서 배포한다.

사용법:
    python scripts/build_site.py                 # 전 섹터
    python scripts/build_site.py --sector cosmetics
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import regions
from kortrade.codes import canon_sido
from kortrade.sectors import Sector, load_sectors, validate_all
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))

WINDOW = 8          # YTD 비교창(개월)
RECENT = 3          # 최근 구간(개월)
TOP_PLACES = 8      # 카테고리별 상위 시군구 수
HIST_MONTHS = 32    # 차트에 싣는 월수
# 증가율의 분모(전년 동기) 최소액. 이보다 작으면 % 를 계산하지 않는다.
# 기저가 거의 0인 계열에서 +1267% 같은 값이 나와 로테이션 판단을 오염시킨다.
MIN_BASE_USD = 2_000_000


def _shift(p: str, k: int) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _pct(c, p):
    return round((c / p - 1) * 100, 1) if p and p > 0 else None


def load_region(store: Store, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    df = store.frame(
        "SELECT period, hs_code, sido_name, sigungu_name, SUM(exp_usd) exp_usd,"
        " SUM(exp_cnt) exp_cnt FROM region_trade WHERE hs_code IN (%s)"
        " GROUP BY period, hs_code, sido_name, sigungu_name" % ",".join("?" * len(codes)),
        codes,
    )
    if df.empty:
        return df
    df["exp_usd"] = pd.to_numeric(df["exp_usd"], errors="coerce").fillna(0.0)
    # 행정구역 개편으로 이름이 바뀐 시군구를 한 실체로 접는다 (인천 중구 -> 제물포구 등)
    df["sigungu_name"] = [regions.canonical_sigungu(s or "", g)
                          for s, g in zip(df["sido_name"].fillna(""), df["sigungu_name"])]
    # ★ str[:2] 로 자르면 안 된다. '경상북도'·'경상남도'가 둘 다 '경상'이 되고
    #   '충청남도'·'충청북도'도 둘 다 '충청'이 되어 서로 다른 지역이 한 줄로 합쳐 보인다.
    #   (실측: '경상 성주군'으로 표시됐는데 성주군은 경상북도다.)
    df["place"] = pd.Series([canon_sido(s) for s in df["sido_name"].fillna("")],
                            index=df.index) + " " + df["sigungu_name"]
    return df


def build_sector(store: Store, sec: Sector) -> dict | None:
    df = load_region(store, sec.codes)
    if df.empty:
        return None

    months = sorted(df["period"].unique())[-HIST_MONTHS:]
    df = df[df["period"].isin(months)]
    end = months[-1]
    cur = months[-WINDOW:]
    prev = [_shift(p, -12) for p in cur]
    q_cur = months[-RECENT:]
    q_prev = [_shift(p, -12) for p in q_cur]

    def win(sub, ps):
        return float(sub[sub["period"].isin(ps)]["exp_usd"].sum())

    # ---------- 카테고리 ----------
    tot_c = win(df, cur)
    tot_p = win(df, prev)
    cats = []
    for hs in sec.codes:
        sub = df[df["hs_code"] == hs]
        if sub.empty:
            continue
        m = sub.groupby("period")["exp_usd"].sum().reindex(months, fill_value=0.0)
        c, p = win(sub, cur), win(sub, prev)
        qc, qp = win(sub, q_cur), win(sub, q_prev)
        yoy, q3 = _pct(c, p), _pct(qc, qp)

        places = []
        for place, g in sub.groupby("place"):
            pc, pp_ = win(g, cur), win(g, prev)
            if pc < 1_000_000:          # 비교창 $1M 미만은 노이즈
                continue
            gm = g.groupby("period")["exp_usd"].sum().reindex(months, fill_value=0.0)
            # ★ 분모(전년 동기)가 거의 0이면 증가율은 숫자만 크고 뜻이 없다.
            #   실측: 인천 남동구 향수 YoY +1267%, 가속 -837.4%p — 기저가 $0.6M 이었다.
            #   이런 값은 '–'로 비우고 신규 진입 표시만 남긴다. 억지로 % 를 쓰면
            #   로테이션 판단이 기저효과에 끌려간다.
            py = _pct(pc, pp_) if pp_ >= MIN_BASE_USD else None
            qcv, qpv = win(g, q_cur), win(g, q_prev)
            pq = _pct(qcv, qpv) if qpv >= MIN_BASE_USD else None
            places.append({
                "place": place, "ytd": round(pc / 1e6, 1),
                "yoy": py, "q3": pq,
                "accel": round(pq - py, 1) if (py is not None and pq is not None) else None,
                "new": pp_ < MIN_BASE_USD,      # 전년 기저가 없던 신규 진입
                "v": [round(x / 1e6, 2) for x in gm.tolist()],
            })
        places.sort(key=lambda r: -r["ytd"])

        cats.append({
            "hs": hs, "name": sec.label(hs), "group": sec.group(hs),
            "ytd": round(c / 1e6, 1), "prev": round(p / 1e6, 1),
            "share": round(c / tot_c * 100, 2) if tot_c else None,
            "share_chg": round((c / tot_c - p / tot_p) * 100, 2) if (tot_c and tot_p) else None,
            "yoy": yoy, "q3": q3,
            "accel": round(q3 - yoy, 1) if (yoy is not None and q3 is not None) else None,
            "m": [round(x / 1e6, 2) for x in m.tolist()],
            "places": places[:TOP_PLACES],
        })
    cats.sort(key=lambda r: -r["ytd"])
    if not cats:
        return None

    # ---------- 지배 카테고리 분리 ----------
    dom = next((c for c in cats if c["hs"] == sec.dominant), None)
    rest = [c for c in cats if dom is None or c["hs"] != dom["hs"]]
    rest_m = [round(sum(c["m"][i] for c in rest), 2) for i in range(len(months))]
    split = None
    if dom and rest_m and rest_m[0] > 0 and dom["m"][0] > 0:
        split = {
            "domName": dom["name"], "restName": "그 외",
            "domIdx": [round(x / dom["m"][0] * 100, 1) for x in dom["m"]],
            "restIdx": [round(x / rest_m[0] * 100, 1) for x in rest_m],
        }

    # ---------- 그룹 롤업 ----------
    groups = {}
    for c in cats:
        g = groups.setdefault(c["group"], {"group": c["group"], "ytd": 0.0, "prev": 0.0,
                                           "share_chg": 0.0})
        g["ytd"] += c["ytd"]; g["prev"] += c["prev"]
        g["share_chg"] += c["share_chg"] or 0.0
    for g in groups.values():
        g["ytd"] = round(g["ytd"], 1); g["prev"] = round(g["prev"], 1)
        g["yoy"] = _pct(g["ytd"], g["prev"])
        g["share_chg"] = round(g["share_chg"], 2)

    warn = regions.break_warning(months)
    return {
        "key": sec.key, "name": sec.name, "subtitle": sec.subtitle,
        "asOf": end, "months": months,
        "window": WINDOW, "recent": RECENT,
        "totals": {"ytd": round(tot_c / 1e6, 1), "prev": round(tot_p / 1e6, 1),
                   "yoy": _pct(tot_c, tot_p)},
        "dominant": sec.dominant,
        "cats": cats, "split": split,
        "groups": sorted(groups.values(), key=lambda g: -g["ytd"]),
        "breakWarning": warn,
        "notes": sec.notes.strip(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--sector", default=None, help="특정 섹터만")
    ap.add_argument("--out", default=str(SITE / "data"))
    args = ap.parse_args()

    errs = validate_all()
    if errs:
        print("섹터 설정 오류:"); [print("  -", e) for e in errs]
        return 1

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    sectors = [s for s in load_sectors() if (not args.sector or s.key == args.sector)]

    entries = []
    with Store(args.db) as store:
        cov = store.coverage()
        for sec in sectors:
            payload = build_sector(store, sec) if sec.active else None
            entry = {"key": sec.key, "name": sec.name, "subtitle": sec.subtitle,
                     "status": sec.status, "order": sec.order}
            if payload:
                (out / f"{sec.key}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
                entry.update({"ready": True, "asOf": payload["asOf"],
                              "ytd": payload["totals"]["ytd"], "yoy": payload["totals"]["yoy"],
                              "cats": len(payload["cats"])})
                print(f"  ✓ {sec.key:<14} {payload['asOf']}  ${payload['totals']['ytd']:,.0f}M  "
                      f"카테고리 {len(payload['cats'])}개")
            else:
                entry["ready"] = False
                entry["reason"] = "HS 코드 미검증 (draft)" if not sec.active else "수집 데이터 없음"
                print(f"  · {sec.key:<14} 미수집 — {entry['reason']}")
            entries.append(entry)

    manifest = {
        "generatedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "sectors": entries,
        "coverage": cov,
        "source": "관세청_시군구별 품목별 수출입실적 (공공데이터포털 data.go.kr)",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n생성 완료 → {out}  ({manifest['generatedAt']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
