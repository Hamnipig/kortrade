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

from kortrade import breadth as BR
from kortrade import pq as PQ
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


# ══════════════════════════════════════════════════════════════════════════════
# 국가 축 — 단가(P/Q)와 확산도
#
# 시군구 축(region_trade)에는 **중량 필드가 없다**. 그래서 단가는 국가 축에서만
# 나온다. 다행히 국가별 API(nitemtrade)는 expWgt 를 주고 우리는 이미 수집해 두었다
# — 추가 API 호출 0건이다.
#
# 또 하나: sector_trade 의 hs_code 는 **응답 원본(10단위)** 이고 hs6 는 롤업 키다.
# 즉 330499 를 6단위로 요청해도 3304991000(기초) / 3304992000(메이크업) 이
# 따로 들어와 있다. 이 둘은 단가가 33.8 vs 53.2 $/kg 로 1.6배 다르고 방향도
# 다른데, 지금까지 6단위 하나로 합쳐 평균을 내고 있었다. 쪼갠다.
# ══════════════════════════════════════════════════════════════════════════════

def load_nation(store: Store, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    df = store.frame(
        "SELECT period, hs_code, hs6, MAX(hs_name) hs_name, country_code,"
        " MAX(country_name) country_name, SUM(exp_usd) exp_usd, SUM(exp_wgt) exp_wgt"
        " FROM sector_trade WHERE hs6 IN (%s)"
        " GROUP BY period, hs_code, hs6, country_code" % ",".join("?" * len(codes)),
        codes,
    )
    if df.empty:
        return df
    for c in ("exp_usd", "exp_wgt"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def _pqrow(sub: pd.DataFrame, cur: list[str], prev: list[str]) -> dict:
    def s(ps, col):
        return float(sub[sub["period"].isin(ps)][col].sum())
    return PQ.decompose(s(cur, "exp_usd"), s(prev, "exp_usd"),
                        s(cur, "exp_wgt"), s(prev, "exp_wgt"))


def _usd(d: dict) -> dict:
    """페이로드 크기를 줄이려고 달러를 백만 달러로 접는다."""
    out = dict(d)
    for k in ("usd", "usdPrev"):
        if out.get(k) is not None:
            out[k] = round(out[k] / 1e6, 1)
    for k in ("wgt", "wgtPrev"):
        if out.get(k) is not None:
            out[k] = round(out[k] / 1000.0, 1)      # 톤
    return out


def build_nation(store: Store, sec: Sector) -> dict | None:
    df = load_nation(store, sec.codes)
    if df.empty:
        return None

    months = sorted(df["period"].unique())[-HIST_MONTHS:]
    df = df[df["period"].isin(months)]
    cur = months[-WINDOW:]
    prev = [_shift(p, -12) for p in cur]
    if not set(prev) & set(months):
        return None                                  # 전년 동기가 없으면 비교 불가

    world = df[df["country_code"] == "ALL"]          # 국가 구분 없는 전국 합계
    nat = df[df["country_code"] != "ALL"]
    base = world if not world.empty else nat         # 합계가 없으면 수집국 합으로 대체

    # ---------- 카테고리 (HS6) + 그 아래 HSK 10단위 ----------
    cats = []
    for hs in sec.codes:
        sub = base[base["hs6"] == hs]
        if sub.empty:
            continue
        row = _usd(_pqrow(sub, cur, prev))
        # 월별 단가 계열 — 추이를 눈으로 보기 위한 것. 중량이 작은 달은 비운다.
        g = sub.groupby("period")[["exp_usd", "exp_wgt"]].sum().reindex(months, fill_value=0.0)
        row["aspM"] = [None if (a := PQ.asp(u, w)) is None else round(a, 1)
                       for u, w in zip(g["exp_usd"], g["exp_wgt"])]

        kids = []
        for code, ksub in sub.groupby("hs_code"):
            if str(code) == hs:                      # 6단위로만 돌아온 행은 분해할 게 없다
                continue
            k = _usd(_pqrow(ksub, cur, prev))
            if not k["usd"]:
                continue
            k["code"] = str(code)
            k["name"] = str(ksub["hs_name"].dropna().iloc[0]) if ksub["hs_name"].notna().any() else str(code)
            k["share"] = round(k["usd"] / row["usd"] * 100, 1) if row["usd"] else None
            kids.append(k)
        kids.sort(key=lambda r: -(r["usd"] or 0))
        row.update({"hs": hs, "name": sec.label(hs), "group": sec.group(hs),
                    "hs10": kids})
        cats.append(row)
    cats.sort(key=lambda r: -(r["usd"] or 0))
    if not cats:
        return None

    # ---------- 국가별 ----------
    hubs = set(sec.hub_countries)
    countries, cur_usd, prev_usd = [], {}, {}
    for cc, csub in nat.groupby("country_code"):
        r = _usd(_pqrow(csub, cur, prev))
        if not r["usd"]:
            continue
        cc = str(cc)
        r["cc"] = cc
        r["name"] = str(csub["country_name"].dropna().iloc[0]) if csub["country_name"].notna().any() else cc
        r["hub"] = cc in hubs
        r["why"] = " ".join(str((sec.hub_countries.get(cc) or {}).get("why", "")).split())
        countries.append(r)
        cur_usd[cc] = r["usd"] * 1e6
        prev_usd[cc] = (r["usdPrev"] or 0.0) * 1e6
    countries.sort(key=lambda r: -(r["usd"] or 0))

    world_total = float(world[world["period"].isin(cur)]["exp_usd"].sum()) or None
    spread = BR.analyze(cur_usd, prev_usd, hubs=hubs, world_total=world_total)
    # 백만 달러로 접는다 (화면 단위 통일)
    for k in ("total",):
        for blk in (spread["all"]["now"], spread["all"]["prev"],
                    spread["exHub"]["now"], spread["exHub"]["prev"]):
            blk[k] = round(blk[k] / 1e6, 1)
    for k in ("totalDelta", "outsideDelta"):
        spread["contribution"][k] = round(spread["contribution"][k] / 1e6, 1)
    for side in ("gainers", "losers"):
        for r in spread["contribution"][side]:
            r["delta"] = round(r["delta"] / 1e6, 1)
    spread["hub"]["usd"] = round(spread["hub"]["usd"] / 1e6, 1)

    # 신규 시장 진입 — 월 $2M 을 연속 2개월 넘긴 시점
    ENTRY = 2_000_000
    entries = []
    for cc, csub in nat.groupby("country_code"):
        ser = csub.groupby("period")["exp_usd"].sum().to_dict()
        when = BR.first_cross(ser, ENTRY)
        if when and when >= months[-24]:             # 최근 2년 내 진입만
            entries.append({"cc": str(cc), "since": when,
                            "usd": round(float(csub[csub["period"].isin(cur)]["exp_usd"].sum()) / 1e6, 1)})
    entries.sort(key=lambda r: r["since"], reverse=True)

    return {
        "asOf": months[-1], "months": months, "window": WINDOW,
        "total": _usd(_pqrow(base, cur, prev)),
        "cats": cats,
        "countries": countries,
        "breadth": spread,
        "entries": entries[:8],
        "entryUsd": ENTRY / 1e6,
        "hubNote": {cc: " ".join(str((v or {}).get("why", "")).split())
                    for cc, v in sec.hub_countries.items()},
        "minWgtKg": PQ.MIN_WGT_KG, "flatPct": PQ.FLAT_PCT,
    }


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

        places, all_places = [], {}
        for place, g in sub.groupby("place"):
            pc, pp_ = win(g, cur), win(g, prev)
            # ★ 고정 표시(pinned) 지역은 $1M 미만이어도 값을 남긴다.
            #   "얼마나 작은가"가 곧 답이기 때문이다. 순위 표에서만 걸러낸다.
            all_places[place] = (pc, pp_)
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
        rank_of = {r["place"]: i + 1 for i, r in enumerate(places)}

        # ── 고정 표시 지역 ────────────────────────────────────────────────
        # 순위 컷오프(상위 8) 때문에 안 보이는 것인지, 애초에 그 지역으로 안
        # 잡히는 것인지 **구분이 안 되던** 문제를 푼다. 값이 0이면 0이라고 찍는다.
        pinned = []
        for pin in sec.pinned_places:
            key = str(pin.get("match", "")).strip()
            if not key:
                continue
            hits = [pl for pl in all_places if key in pl]
            pc = sum(all_places[h][0] for h in hits)
            pp_ = sum(all_places[h][1] for h in hits)
            pinned.append({
                "match": key, "why": " ".join(str(pin.get("why", "")).split()),
                "places": sorted(hits),
                "ytd": round(pc / 1e6, 1) if hits else None,
                "yoy": _pct(pc, pp_) if pp_ >= MIN_BASE_USD else None,
                "rank": next((rank_of[h] for h in hits if h in rank_of), None),
                "shownInTop": any(h in rank_of for h in hits),
                # 카테고리 대비 비중 — '작아서 안 보인다'를 숫자로 말한다
                "share": round(pc / c * 100, 2) if c else None,
            })

        # 반올림된 ytd 를 더하면 100.2% 같은 값이 나온다. 원값으로 계산한다.
        shown = sum(all_places[r["place"]][0] for r in places[:TOP_PLACES])
        cats.append({
            "pinned": pinned,
            # 상위 N 이 카테고리의 몇 %를 설명하는가. 낮으면 '상위권 = 전부'가 아니다.
            "placesShownUsd": round(shown / 1e6, 1),
            "placesCoverage": round(min(shown / c, 1.0) * 100, 1) if c else None,
            "placesTotal": len(places),
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
        # 국가 축(단가·확산도). 국가별 수집이 안 된 섹터는 None 이고 화면이 숨긴다.
        "nation": build_nation(store, sec),
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
