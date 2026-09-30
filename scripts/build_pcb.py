#!/usr/bin/env python3
"""PCB·기판 레이어 → site/data/pcb.json  (API 호출 없음)

iM증권 「PCB Tracker」(2026-09-28) 한국 축의 원출처 재현 + 그 리포트가 해외
데이터로 우회하는 부분을 한국 데이터로 어디까지 볼 수 있는지의 경계 표시.

내는 것

  0. 코드 검증   probe 코드의 응답 품명을 expect 문자열과 대조한다. 통과한
                 코드만 집계에 들어간다. 실패는 표로 남겨 근거를 보여준다.
  1. P/Q 3분해   수출액 / 중량 / ASP. **이 레이어의 중심**이다. 한국 HS 8534 에
                 층수 구분이 없어 고다층 전환은 ASP 로만 보인다.
  2. 원가 대조   CCL·동박 수입 단가. ASP 상승이 믹스인지 구리값인지 가른다.
  3. 축별 가속   완제품 / 소재 / 장비의 YoY·가속·턴어라운드 + 선후행 시차.
  4. 국가·확산도 대미/대중/대만향 P/Q 와 상위국 집중도.
  5. 지역        안산·수원·달성·구미·세종·청주 (금액만 — 시군구 중량은 비공개).
  6. 해외 대조축 아직 연결 전. 무엇을 기다리는지 화면에 적는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import breadth as BR
from kortrade import flash as F
from kortrade import intl as IN
from kortrade import pcb as P
from kortrade import pq as PQ
from kortrade import watchlist as W
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))
M = 1_000_000.0

# 지역 축에서 이보다 작은 시군구는 싣지 않는다. 자기신고 항목이라 소액 구간은
# 대부분 본사 주소 한 건이 만든 잡음이다.
MIN_PLACE_USD = 2_000_000.0


def _m(v):
    return None if v is None else round(v / M, 1)


def _r(v, n=2):
    return None if v is None else round(v, n)


# ══════════════════════════════════════════════════════════════════════════
# 0. 코드 검증 — probe 코드가 실제로 그 물건인지
#
# 이 프로젝트는 "미검증 코드로 수집하면 조용히 엉뚱한 품목이 쌓인다"는 사고를
# 실제로 겪었다(사용자 제시 20개 중 12개 오류). 그렇다고 전부 draft 로 묶으면
# 화면이 영원히 비는데, 그건 원칙이 아니라 부작용이다.
#
# probe 는 그 사이를 **기계 대조**로 메운다. 관세청 응답의 hs_name 에 expect
# 문자열이 있어야 집계에 들어간다. 사람의 기억이 아니라 응답이 판정한다.
# ══════════════════════════════════════════════════════════════════════════

def verify_codes(df, cfg: P.PcbConfig, cur) -> tuple[list[dict], set[str]]:
    rows, ok = [], set()
    names = {}
    if not df.empty and "hs_name" in df.columns:
        for code, nm in df.groupby("hs_code")["hs_name"].first().items():
            names[str(code)] = None if nm is None else str(nm)
    for c in cfg.all():
        sub = df[df["hs_code"] == c.code]
        usd = float(sub[sub["period"].isin(cur)]["eu"].sum()) if not sub.empty else 0.0
        imp = float(sub[sub["period"].isin(cur)]["iu"].sum()) if not sub.empty else 0.0
        nm = names.get(c.code)
        hit = P.verify_name(c.expect, nm)
        seen = not sub.empty
        counted = bool(c.countable and seen and hit)
        if counted:
            ok.add(c.code)
        rows.append({
            "code": c.code, "label": c.label, "group": c.group, "status": c.status,
            "purity": c.purity, "expect": c.expect, "name": nm, "seen": seen,
            "hit": hit, "counted": counted, "note": c.note,
            "usd": _m(usd), "impUsd": _m(imp),
            "why": ("집계 포함" if counted else
                    ("draft — 측정만" if c.status == "draft" else
                     ("응답 없음 — 코드가 없거나 수집 전" if not seen else
                      f"품명 대조 실패 — '{c.expect}' 를 찾지 못함"))),
        })
    rows.sort(key=lambda r: (P.GROUPS.index(r["group"]) if r["group"] in P.GROUPS else 9,
                             -(r["usd"] or 0)))
    return rows, ok


# ══════════════════════════════════════════════════════════════════════════
# 5. 지역 — 시군구. **금액만 나온다.**
#
# 2026-09-01 관세청 지침으로 시군구 × HS 2·4·6단위 중량이 비공개가 됐다.
# 그 전에도 시군구 API 응답에는 중량 필드가 없었다. 그래서 지역별 ASP 는
# 원리적으로 만들 수 없다 — 이 사실을 화면에도 적는다.
#
# 지역 귀속은 「제조장소 우편번호」지만 **수출자 자기신고 항목**이라 품질이
# 품목마다 다르다. 화장품에서 실증됐다(화성이 색조 1위, 기초 8위 밖).
# '안 잡힌다'가 '생산이 없다'가 아니다.
# ══════════════════════════════════════════════════════════════════════════

def build_places(store: Store, cfg: P.PcbConfig, months, cur, prev, q3, q3p) -> dict:
    codes = cfg.region_codes
    if not codes:
        return {"ok": False, "note": "region_codes 가 비어 있습니다."}
    rg = store.frame(
        "SELECT period, hs_code, sido_name, sigungu_name, SUM(exp_usd) eu"
        " FROM region_trade WHERE hs_code IN (%s)"
        " GROUP BY period, hs_code, sido_name, sigungu_name" % ",".join("?" * len(codes)),
        codes)
    if rg.empty:
        return {"ok": False,
                "note": "시군구 데이터가 아직 없습니다. "
                        "scripts/run_update.py --extra-regions pcb 를 먼저 실행하세요."}
    rg["eu"] = rg["eu"].astype(float).fillna(0.0)
    rg["place"] = (rg["sido_name"].fillna("") + " " + rg["sigungu_name"].fillna("")).str.strip()

    def win(frame, ps, code=None):
        f = frame[frame["period"].isin(ps)]
        if code:
            f = f[f["hs_code"] == code]
        return float(f["eu"].sum())

    pinned = {p.match: p.why for p in cfg.places}
    board6 = "853400"

    # 고정 표시 + 상위 지역을 합집합으로 낸다. 고정 표시가 0 이면 **0 이라는 사실**이
    # 정보다 — 화장품에서 '세종·음성이 안 잡힌다'를 이 방식으로 확인했다.
    tot = rg[rg["period"].isin(cur)].groupby("place")["eu"].sum().sort_values(ascending=False)
    want = [pl for pl in tot.index if tot[pl] >= MIN_PLACE_USD][:12]
    for key in pinned:
        for pl in rg["place"].unique():
            if key in pl and pl not in want:
                want.append(pl)

    rows = []
    for pl in want:
        sub = rg[rg["place"] == pl]
        n8, p8 = win(sub, cur), win(sub, prev)
        n3, p3 = win(sub, q3), win(sub, q3p)
        yoy = W.pct(n8, p8, W.MIN_BASE_USD)
        r3 = W.pct(n3, p3, W.MIN_BASE_USD)
        g = sub[sub["hs_code"] == board6].groupby("period")["eu"].sum()
        why = next((w for k, w in pinned.items() if k in pl), "")
        rows.append({
            "place": pl, "pinned": bool(why), "why": why,
            "usd": _m(n8), "yoy": yoy, "recentYoy": r3,
            "accel": None if (yoy is None or r3 is None) else _r(r3 - yoy, 1),
            "board": _m(win(sub, cur, board6)),
            "verdict": P.verdict(yoy, r3),
            "m": [_r(float(g.get(p, 0.0)) / M, 1) for p in months],
        })
    rows.sort(key=lambda r: (not r["pinned"], -(r["usd"] or 0)))
    covered = sum(r["usd"] or 0 for r in rows)
    whole = _m(win(rg, cur)) or 0
    return {
        "ok": True, "rows": rows, "codes": codes,
        "total": whole,
        "covered": round(covered / whole * 100, 1) if whole else None,
        "note": "시군구 축은 **금액만** 나옵니다 — 시군구 API 에는 중량 필드가 없고, "
                "2026-09-01 지침으로 시군구 중량·HSK 10단위가 비공개가 됐습니다. "
                "지역별 ASP 는 원리적으로 만들 수 없습니다.",
        "warn": "지역 귀속은 「제조장소 우편번호」(제조자 사업장) 기준이지만 "
                "**수출자 자기신고 항목**이라 품목·기업마다 품질이 다릅니다. "
                "화장품에서 실측으로 확인됐습니다 — 화성이 색조에서는 1위인데 "
                "기초에서는 8위 밖이었습니다. 안 잡힌다고 생산이 없는 것이 아닙니다.",
    }


# ══════════════════════════════════════════════════════════════════════════
# 6. 해외 대조축 — 일본 METI 品目別 생산
#
# 왜 이게 화면에 있어야 하는가.
#   한국 ASP 가 올랐을 때 그것이 (a)고다층·패키지기판으로의 믹스 전환인지
#   (b)구리 가격·환율인지를 **한국 데이터만으로는 가를 수 없다.** HS 8534 에
#   층수 구분이 없기 때문이다. 일본 METI 는 品目別(片面/両面/多層/ビルドアップ/
#   フレキシブル) 생산액을 주므로 고부가 비중의 방향을 따로 볼 수 있다.
#
# ★ 두 나라 숫자를 **더하지 않는다.** 엔 vs 달러, 생산 vs 수출, 모집단도 다르다.
#   방향의 일치/불일치만 읽는다. 일치하면 근거가 둘, 엇갈리면 경고다.
# ══════════════════════════════════════════════════════════════════════════

def build_intl(cfg: P.PcbConfig, store: Store | None = None,
               kr_asp_yoy: float | None = None) -> dict:
    declared = [{"key": i.key, "name": i.name, "who": i.who, "needKey": i.need_key,
                 "free": i.free, "have": i.have, "signup": i.signup,
                 "lag": i.lag, "why": " ".join((i.why or "").split())}
                for i in cfg.intl]
    why = ("한국 데이터만으로는 **믹스**(고다층·패키지기판 비중)와 **재고순환**을 "
           "볼 수 없습니다. 한국 HS 8534 에 층수 구분이 없고, 관세 통계에는 "
           "재고 개념 자체가 없기 때문입니다. 일본 METI 가 전자를, 대만 MOEA 가 "
           "후자를 줍니다.")
    off = {"ok": False, "items": declared, "why": why,
           "note": "리포트 표1 의 일본(METI)·대만(MOEA)·북미(GEA) 축은 아직 붙이지 "
                   "않았습니다. 전부 무료 출처이고 조사는 끝났습니다."}
    if store is None:
        return off
    try:
        ic = IN.load()
    except Exception:                                    # noqa: BLE001
        return off

    verify = {}
    vf = ROOT / "data" / "pcb_intl_verify.json"
    if vf.exists():
        try:
            verify = json.loads(vf.read_text(encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            verify = {}
    ev = verify.get("estat") or {}
    # 진단을 화면까지 끌어올린다. 2026-09-30 에 "데이터가 없습니다" 한 줄만 떠서
    # 왜 비었는지(키 문제인지, 표 선택 문제인지)를 레포의 JSON 을 열어야 알았다.
    # 그 한 줄이 화면에 있었으면 한 바퀴를 아꼈다.
    diag = {"note": ev.get("note"), "probes": [
        {"id": d.get("id"), "title": d.get("title"), "year": d.get("year"),
         "rows": d.get("rows"), "score": d.get("score"), "ok": d.get("ok"),
         "reason": d.get("reason"), "nItems": d.get("nItems"),
         "nMeas": d.get("nMeas"), "last": d.get("last"),
         "classes": [f"{c.get('id')}({c.get('n')})"
                     for c in ((d.get("meta") or {}).get("classes") or [])],
         "sample": [nm for c in ((d.get("meta") or {}).get("classes") or [])
                    if c.get("id") != "time" for nm in (c.get("sample") or [])][:8]}
        for d in (verify.get("probes") or [])]}
    tried = verify.get("search", {}).get("tried") or []
    diag["searched"] = len(tried)
    diag["candidates"] = verify.get("search", {}).get("found")
    diag["apiOk"] = bool(tried) and all(t.get("status") == 0 for t in tried)

    ser = store.demand(IN.SOURCE)
    if not ser:
        off["note"] = ("일본 축(METI/e-Stat)에 아직 데이터가 없습니다. "
                       + ("키와 API 호출은 정상입니다 — 통계표 선택 문제입니다. "
                          if diag["apiOk"] else "")
                       + f"원인: {ev.get('note') or 'data/pcb_intl_verify.json 확인'}")
        off["diag"] = diag
        return off

    # 일본 축은 **자기 달력을 쓴다.** 한국 관세청과 공표 시차가 달라서 억지로
    # 맞추면 최신월이 통째로 빈다. 있는 달을 그대로 쓰고 기준월을 따로 적는다.
    months = sorted({p for s in ser.values() for p in s})[-cfg.hist:]
    if len(months) < 13:
        off["note"] = (f"일본 축 월 수가 {len(months)}개뿐이라 전년 동월 비교를 "
                       f"만들 수 없습니다.")
        off["diag"] = diag
        return off
    latest = months[-1]
    cur3 = months[-cfg.recent:]
    prev3 = [F.shift(p, -12) for p in cur3]

    def win(key, ps):
        s_ = ser.get(key) or {}
        vals = [s_.get(p) for p in ps if s_.get(p) is not None]
        return sum(vals) if vals else None

    def yoy(key, a, b):
        n, o = win(key, a), win(key, b)
        return None if (n is None or not o) else round((n / o - 1) * 100, 1)

    # 단위는 **DB 에서** 읽는다. verify 파일에서만 읽으면 그 파일이 없거나 낡은
    # 실행에서 단위가 통째로 빈다 — 값은 있는데 단위만 사라지는 화면이 된다.
    # 百万円↔億円 환산은 하지 않는다. 우리가 환산하면 그 환산이 숫자를 만든다.
    units: dict = {}
    try:
        uf = store.frame(
            "SELECT series, unit FROM demand_series"
            " WHERE source = ? AND unit IS NOT NULL AND unit <> ''"
            " GROUP BY series", (IN.SOURCE,))
        if not uf.empty:
            units = {str(r.series): str(r.unit) for r in uf.itertuples()}
    except Exception:                                    # noqa: BLE001
        units = {}

    def unit_of(key):
        return units.get(key) or (ev.get("units") or {}).get(key, "")

    amt_tot = {p: 0.0 for p in months}
    for i in ic.items:
        k = IN.series_key(i.key, "amt")
        if i.key == "pcb_total":
            continue                    # 계는 합계에 넣지 않는다 — 이중계상
        for p in months:
            v = (ser.get(k) or {}).get(p)
            if v:
                amt_tot[p] += v

    def hv_share(p):
        tot = amt_tot.get(p) or 0.0
        if tot <= 0:
            return None
        hv = sum(((ser.get(IN.series_key(k, "amt")) or {}).get(p) or 0.0)
                 for k in P.HIGH_VALUE_JP)
        return round(hv / tot * 100, 1)

    mix_m = [hv_share(p) for p in months]
    def mix_win(ps):
        tot = sum((amt_tot.get(p) or 0.0) for p in ps)
        if tot <= 0:
            return None
        hv = sum(((ser.get(IN.series_key(k, "amt")) or {}).get(p) or 0.0)
                 for k in P.HIGH_VALUE_JP for p in ps)
        return round(hv / tot * 100, 1)
    mix_now, mix_prev = mix_win(cur3), mix_win(prev3)
    mix_chg = (None if (mix_now is None or mix_prev is None)
               else round(mix_now - mix_prev, 1))

    def _stock(item_key):
        ks, ka_ = IN.series_key(item_key, "stock"), IN.series_key(item_key, "amt")
        n, p_ = win(ks, cur3), win(ks, prev3)
        prod = win(ka_, cur3)
        prod_p = win(ka_, prev3)
        # 재고/생산 비율. 절대 재고액보다 **비율의 방향**이 신호다 —
        # 생산이 같이 늘면 재고가 늘어도 정상이다.
        r_now = (None if not prod or n is None else round(n / prod, 2))
        r_prev = (None if not prod_p or p_ is None else round(p_ / prod_p, 2))
        return {"now": _r(n, 1), "unit": unit_of(ks),
                "yoy": yoy(ks, cur3, prev3),
                "ratio": r_now, "ratioPrev": r_prev,
                "ratioChg": (None if (r_now is None or r_prev is None)
                             else round(r_now - r_prev, 2))}

    rows = []
    for i in ic.items:
        ka, kq = IN.series_key(i.key, "amt"), IN.series_key(i.key, "qty")
        sa = ser.get(ka) or {}
        got = bool(sa) or bool(ser.get(kq))
        share_now = share_prev = None
        if i.key != "pcb_total":
            tn = sum((amt_tot.get(p) or 0.0) for p in cur3)
            tp = sum((amt_tot.get(p) or 0.0) for p in prev3)
            if tn > 0:
                share_now = round((win(ka, cur3) or 0.0) / tn * 100, 1)
            if tp > 0:
                share_prev = round((win(ka, prev3) or 0.0) / tp * 100, 1)
        rows.append({
            "key": i.key, "label": i.label, "why": i.why, "seen": got,
            "highValue": i.key in P.HIGH_VALUE_JP,
            "amt": {"now": _r(sa.get(latest), 1), "unit": unit_of(ka),
                    "yoy": yoy(ka, [latest], [F.shift(latest, -12)]),
                    "recentYoy": yoy(ka, cur3, prev3),
                    "m": [_r(sa.get(p), 1) for p in months]},
            "qty": {"yoy": yoy(kq, [latest], [F.shift(latest, -12)]),
                    "recentYoy": yoy(kq, cur3, prev3)},
            # 재고순환 — 대만 MOEA 를 붙이기 전까지 **재고를 볼 수 있는 유일한 축**이다.
            # 재고가 생산보다 먼저 튀면 다음 분기 수출이 꺾인다.
            "stock": _stock(i.key),
            "share": share_now, "sharePrev": share_prev,
            "shareChg": (None if (share_now is None or share_prev is None)
                         else round(share_now - share_prev, 1)),
        })

    return {
        "ok": True,
        "asOf": latest, "months": months,
        "source": {"who": "経済産業省生産動態統計調査 (e-Stat API)",
                   "statsDataId": ev.get("statsDataId"), "title": ev.get("title"),
                   "cycle": ev.get("cycle"), "from": ev.get("from"), "to": ev.get("to"),
                   "rows": ev.get("rows"), "missing": ev.get("missing") or []},
        "credit": ic.credit,
        "rows": rows,
        "mix": {"label": "고부가 비중 (10층 이상 + 빌드업 다층)",
                "now": mix_now, "prev": mix_prev, "chg": mix_chg, "m": mix_m,
                "note": "일본 생산금액에서 **10층 이상 다층 + 빌드업 다층**이 "
                        "차지하는 비중입니다. 4층·6~8층은 범용에 가까워 뺐습니다. "
                        "한국에는 이 구분이 아예 없습니다 — 그래서 이 축을 둡니다."},
        "cross": {"krAspYoy": kr_asp_yoy, "jpMixChg": mix_chg,
                  "verdict": P.mix_cross(kr_asp_yoy, mix_chg)},
        "why": why,
        "note": "일본은 **생산**, 한국은 **수출**입니다. 단위도 모집단도 달라 "
                "두 숫자를 더하지 않습니다 — 방향만 나란히 놓고 봅니다.",
        "items": declared,
        "diag": ev,
    }


# ══════════════════════════════════════════════════════════════════════════

def build(store: Store, cfg: P.PcbConfig) -> dict | None:
    codes = cfg.all_codes()
    df = store.frame(
        "SELECT period, hs_code, hs_name, country_code,"
        " SUM(exp_usd) eu, SUM(exp_wgt) ew, SUM(imp_usd) iu, SUM(imp_wgt) iw"
        " FROM sector_trade WHERE hs_code IN (%s)"
        " GROUP BY period, hs_code, country_code" % ",".join("?" * len(codes)), codes)
    if df.empty:
        return None
    for c in ("eu", "ew", "iu", "iw"):
        df[c] = df[c].astype(float).fillna(0.0)
    df["hs_code"] = df["hs_code"].astype(str)

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
    # 전월/전년동월 — 리포트가 인용하는 '8월 +15%' 는 단월 YoY 다. 창 평균과
    # 단월은 다른 숫자이므로 둘 다 낸다.
    m1, m1p = [latest], [F.shift(latest, -12)]

    vrows, ok = verify_codes(df, cfg, cur)

    def agg(frame, cs, ps, col):
        if not cs:
            return 0.0
        return float(frame[frame["hs_code"].isin(cs) & frame["period"].isin(ps)][col].sum())

    def ser(frame, cs, col) -> dict[str, float]:
        if not cs:
            return {}
        g = frame[frame["hs_code"].isin(cs)].groupby("period")[col].sum()
        return {p: float(v) for p, v in g.items() if v}

    # 집계에 들어가는 코드 = countable + 응답 있음 + 품명 대조 통과
    def gc(group):
        return [c.code for c in cfg.groups.get(group, []) if c.code in ok]

    board, material, equip = gc("board"), gc("material"), gc("equipment")

    # ── 1. 헤드라인 P/Q 3분해 ────────────────────────────────────────────
    #   리포트 한국 축("8월 +15% / 중량 −3% / ASP +18%")이 그대로 재현되는 자리.
    def pq_of(cs, ps_now, ps_prev, frame=None):
        f = nat if frame is None else frame
        return PQ.decompose(agg(f, cs, ps_now, "eu"), agg(f, cs, ps_prev, "eu"),
                            agg(f, cs, ps_now, "ew"), agg(f, cs, ps_prev, "ew"))

    b_usd, b_wgt = ser(nat, board, "eu"), ser(nat, board, "ew")
    asp_m = {p: PQ.asp(b_usd.get(p), b_wgt.get(p)) for p in months}

    head = {
        "window": pq_of(board, cur, prev),
        "recent": pq_of(board, q3, q3p),
        "month": pq_of(board, m1, m1p),
        "asOfMonth": latest,
        "codes": board,
        "m": {
            "usd": [_r(b_usd.get(p, 0.0) / M, 1) for p in months],
            "wgt": [_r((b_wgt.get(p, 0.0)) / 1000.0, 1) for p in months],   # 톤
            "asp": [_r(asp_m.get(p)) for p in months],
        },
        "aspMax": None, "aspMaxAt": None,
        "caveat": P.asp_caveat(),
    }
    live_asp = [(p, v) for p, v in asp_m.items() if v]
    if live_asp:
        top = max(live_asp, key=lambda kv: kv[1])
        head["aspMax"] = _r(top[1])
        head["aspMaxAt"] = top[0]
        head["aspNowIsMax"] = bool(asp_m.get(latest) and asp_m[latest] >= top[1] - 1e-9)

    # ── 1b. 코드별 P/Q + 합산 가능 여부 ──────────────────────────────────
    #   8534.00.2000(테이프·리드프레임)과 .9000(기타)은 사이클이 다를 수 있다.
    #   합치기 전에 월별 증감률 상관을 재는 것이 이 프로젝트의 원칙이다.
    items = []
    for c in cfg.all():
        cs = [c.code]
        u, w = ser(nat, cs, "eu"), ser(nat, cs, "ew")
        d = pq_of(cs, cur, prev)
        d3 = pq_of(cs, q3, q3p)
        a = {p: PQ.asp(u.get(p), w.get(p)) for p in months}
        imp_u, imp_w = ser(nat, cs, "iu"), ser(nat, cs, "iw")
        items.append({
            "code": c.code, "label": c.label, "group": c.group, "status": c.status,
            "purity": c.purity, "counted": c.code in ok, "note": c.note,
            "usd": _m(d["usd"]), "valueYoy": d["valueYoy"], "qtyYoy": d["qtyYoy"],
            "priceYoy": d["priceYoy"], "asp": d["asp"], "aspPrev": d["aspPrev"],
            "priceShare": d["priceShare"], "qtyShare": d["qtyShare"],
            "verdict": d["verdict"],
            "recentValueYoy": d3["valueYoy"], "recentPriceYoy": d3["priceYoy"],
            "impUsd": _m(agg(nat, cs, cur, "iu")),
            "impYoy": W.pct(agg(nat, cs, cur, "iu"), agg(nat, cs, prev, "iu"), 0),
            "impAsp": _r(PQ.asp(agg(nat, cs, q3, "iu"), agg(nat, cs, q3, "iw"))),
            "m": {"usd": [_r(u.get(p, 0.0) / M, 1) for p in months],
                  "asp": [_r(a.get(p)) for p in months],
                  "impAsp": [_r(PQ.asp(imp_u.get(p), imp_w.get(p))) for p in months]},
        })
    items.sort(key=lambda r: (P.GROUPS.index(r["group"]) if r["group"] in P.GROUPS else 9,
                              -(r["usd"] or 0)))

    merge = []
    bd = [c for c in cfg.groups.get("board", []) if c.code in ok]
    for i in range(len(bd)):
        for j in range(i + 1, len(bd)):
            a = P.growth([float(ser(nat, [bd[i].code], "eu").get(p, 0.0)) for p in months])
            b = P.growth([float(ser(nat, [bd[j].code], "eu").get(p, 0.0)) for p in months])
            xs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
            r = P.corr([p[0] for p in xs], [p[1] for p in xs]) if len(xs) >= 8 else None
            merge.append({"a": bd[i].code, "aLabel": bd[i].label,
                          "b": bd[j].code, "bLabel": bd[j].label,
                          "r": _r(r, 3), "n": len(xs), "verdict": P.merge_verdict(r)})

    # ── 2. 축별 가속 ─────────────────────────────────────────────────────
    groups, tot_n, tot_p, tot_3, tot_3p = [], 0.0, 0.0, 0.0, 0.0
    for g in P.GROUPS:
        cs = gc(g)
        if not cs:
            continue
        n8, p8 = agg(nat, cs, cur, "eu"), agg(nat, cs, prev, "eu")
        n3, p3 = agg(nat, cs, q3, "eu"), agg(nat, cs, q3p, "eu")
        yoy, r3 = W.pct(n8, p8, W.MIN_BASE_USD), W.pct(n3, p3, W.MIN_BASE_USD)
        if g in P.DEMAND_GROUPS:
            tot_n += n8; tot_p += p8; tot_3 += n3; tot_3p += p3
        gs = ser(nat, cs, "eu")
        groups.append({
            "group": g, "label": P.GROUP_LABEL[g], "codes": cs,
            "usd": _m(n8), "yoy": yoy, "recentYoy": r3,
            "accel": None if (yoy is None or r3 is None) else _r(r3 - yoy, 1),
            "verdict": P.verdict(yoy, r3),
            "pq": pq_of(cs, cur, prev),
            "m": [_r(gs.get(p, 0.0) / M, 1) for p in months],
        })
    t_yoy = W.pct(tot_n, tot_p, W.MIN_BASE_USD)
    t_r3 = W.pct(tot_3, tot_3p, W.MIN_BASE_USD)
    total = {"usd": _m(tot_n), "yoy": t_yoy, "recentYoy": t_r3,
             "accel": None if (t_yoy is None or t_r3 is None) else _r(t_r3 - t_yoy, 1),
             "verdict": P.verdict(t_yoy, t_r3),
             "note": "완제품 + 소재. **장비는 뺐습니다** — CAPEX 는 수요의 흐름이 "
                     "아니라 증설의 흐름이라 섞으면 둘 다 흐려집니다."}

    # ── 2b. 원가 대조 — ASP 상승이 믹스인가 구리값인가 ────────────────────
    #   구리가 오르면 믹스가 그대로여도 ASP 가 오른다. CCL 수입 단가를 설명변수로
    #   넣고, 원가로 설명되지 않는 잔차만 믹스·마진으로 읽는다.
    P_ser = {p: v for p, v in asp_m.items() if v}
    costs, best = [], None
    for c in cfg.cost_codes():
        iu, iw = ser(nat, [c.code], "iu"), ser(nat, [c.code], "iw")
        C = {p: v for p, v in
             ((p, PQ.asp(iu.get(p), iw.get(p))) for p in months) if v}
        c_now = C.get(months[-1])
        c_3m = C.get(months[-4]) if len(months) >= 4 else None
        fit = (P.best_lag(P_ser, C, F.shift, cfg.max_lag_months, cfg.min_months)
               if P_ser and C else None)
        costs.append({
            "code": c.code, "label": c.label,
            "impUsd": _m(agg(nat, [c.code], cur, "iu")),
            "impYoy": W.pct(agg(nat, [c.code], cur, "iu"),
                            agg(nat, [c.code], prev, "iu"), 0),
            "price": _r(c_now), "priceMom3": W.pct(c_now, c_3m, 0),
            "priceSeries": [_r(C.get(p)) for p in months],
            "lag": None if not fit else fit["lag"],
            "beta": None if not fit else _r(fit["fit"]["beta"], 3),
            "r2": None if not fit else _r(fit["fit"]["r2"], 3),
            "n": None if not fit else fit["fit"]["n"],
            "lagIdentified": None if not fit else fit["identified"],
            "lagCurve": None if not fit else fit["curve"],
        })
        if fit and fit["fit"]["r2"] >= cfg.min_r2 and (
                best is None or fit["fit"]["r2"] > best["fit"]["fit"]["r2"]):
            best = {"cost": c, "fit": fit, "C": C}

    spread = {"linked": False,
              "note": "CCL 수입 단가와 PCB 수출 ASP 의 연동이 확인되지 않아 "
                      "원가 보정 없이 ASP 를 그대로 읽습니다. "
                      "ASP 상승분 중 얼마가 구리값인지는 아직 가르지 못합니다."}
    if best:
        L, fit = best["fit"]["lag"], best["fit"]["fit"]
        e = {}
        for p in months:
            cp = F.shift(p, -L)
            if p in P_ser and cp in best["C"]:
                e[p] = P_ser[p] - (fit["alpha"] + fit["beta"] * best["C"][cp])
        ks = sorted(e)
        e_now = e.get(ks[-1]) if ks else None
        e_3m = e.get(ks[-4]) if len(ks) >= 4 else None
        d = None if (e_now is None or e_3m is None) else e_now - e_3m
        spread = {
            "linked": True, "costCode": best["cost"].code,
            "costLabel": best["cost"].label, "lag": L,
            "beta": _r(fit["beta"], 3), "r2": _r(fit["r2"], 3), "n": fit["n"],
            "diffR2": best["fit"]["diffR2"], "lagIdentified": best["fit"]["identified"],
            "lagCurve": best["fit"]["curve"],
            "pNow": _r(P_ser.get(ks[-1]) if ks else None),
            "cNow": _r(best["C"].get(months[-1])),
            "eNow": _r(e_now), "e3m": _r(e_3m),
            "series": [_r(e.get(p)) for p in months],
            "verdict": ({"code": "unknown", "label": "판정 불가",
                         "note": "잔차 시계열이 짧습니다."} if d is None else
                        {"code": "mix_up", "label": "믹스·마진 개선",
                         "note": f"원가로 설명되지 않는 단가가 {cfg.recent}개월간 "
                                 f"{d:+.2f}$/kg 벌어졌습니다. ASP 상승이 구리값이 "
                                 f"아니라 믹스·판가 쪽이라는 뜻입니다."} if d > 0 else
                        {"code": "cost_push", "label": "원가 전가",
                         "note": f"잔차가 {d:+.2f}$/kg 좁혀졌습니다. ASP 가 올랐다면 "
                                 f"상당 부분이 원가(구리) 전가입니다."}),
            "note": f"PCB 수출 ASP 를 {best['cost'].label} 수입 단가에 회귀했습니다. "
                    f"시차 {L}개월 · 전가율 β={_r(fit['beta'], 3)} · 수준 R²="
                    f"{_r(fit['r2'], 3)} (표본 {fit['n']}개월). β 는 가정이 아니라 "
                    f"실측값입니다. "
                    + ("시차는 차분 회귀로 식별했습니다."
                       if best["fit"]["identified"] else
                       "다만 시차별 차분 R² 곡선이 평탄해 **시차 자체는 식별되지 "
                       "않았습니다** — 참고로만 보십시오."),
        }

    # ── 3. 선후행 — 소재·장비가 완제품에 앞서는가 ─────────────────────────
    def gser(g):
        cs = gc(g)
        s = ser(nat, cs, "eu")
        return [float(s.get(p, 0.0)) for p in months]

    leadlag = []
    for lead, lag_g in (("equipment", "board"), ("material", "board"),
                        ("equipment", "material")):
        if not gc(lead) or not gc(lag_g):
            continue
        a, b = P.growth(gser(lead)), P.growth(gser(lag_g))
        rows = []
        for k in range(cfg.leadlag_max_months + 1):
            xs = [(a[i], b[i + k]) for i in range(len(a) - k)
                  if a[i] is not None and b[i + k] is not None]
            r = P.corr([p[0] for p in xs], [p[1] for p in xs]) if len(xs) >= 8 else None
            rows.append({"k": k, "r": _r(r, 3), "n": len(xs)})
        live = [x for x in rows if x["r"] is not None]
        top = max(live, key=lambda x: x["r"]) if live else None
        leadlag.append({"lead": lead, "leadLabel": P.GROUP_LABEL[lead],
                        "lag": lag_g, "lagLabel": P.GROUP_LABEL[lag_g], "rows": rows,
                        "bestK": None if not top else top["k"],
                        "bestR": None if not top else top["r"]})

    # ── 4. 국가 축 — P/Q + 확산도 ────────────────────────────────────────
    ctys, cur_map, prev_map = [], {}, {}
    names = {}
    if "country_name" not in df.columns:
        df["country_name"] = None
    for cc, nm in df.groupby("country_code")["country_name"].first().items():
        names[str(cc)] = None if nm is None else str(nm)
    for cc in sorted({str(x) for x in df["country_code"].unique()} - {"ALL"}):
        sub = df[df["country_code"] == cc]
        d = PQ.decompose(agg(sub, board, cur, "eu"), agg(sub, board, prev, "eu"),
                         agg(sub, board, cur, "ew"), agg(sub, board, prev, "ew"))
        if not d["usd"]:
            continue
        cur_map[cc] = d["usd"]
        prev_map[cc] = d["usdPrev"] or 0.0
        ctys.append({
            "cty": cc, "name": names.get(cc) or cc, "hub": cc in cfg.hubs,
            "hubWhy": " ".join((cfg.hubs.get(cc) or "").split()),
            "usd": _m(d["usd"]), "valueYoy": d["valueYoy"], "qtyYoy": d["qtyYoy"],
            "priceYoy": d["priceYoy"], "asp": d["asp"], "aspPrev": d["aspPrev"],
            "priceShare": d["priceShare"], "verdict": d["verdict"],
        })
    ctys.sort(key=lambda r: -(r["usd"] or 0))
    tot_cur = sum(cur_map.values()) or 1.0
    for r in ctys:
        r["share"] = _r((r["usd"] or 0) * M / tot_cur * 100, 1)
    bre = BR.analyze(cur_map, prev_map, hubs=set(cfg.hubs)) if cur_map else None

    # ── 5·6 ──────────────────────────────────────────────────────────────
    places = build_places(store, cfg, months, cur, prev, q3, q3p)
    # 한국 ASP 의 최근창 YoY 를 넘겨 일본 品目別 비중과 교차검증시킨다
    intl = build_intl(cfg, store, head["recent"]["priceYoy"])

    drafts = [r for r in items if r["status"] == "draft"]

    return {
        "version": cfg.version,
        "builtAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "asOf": latest, "months": months,
        "window": cfg.window, "recent": cfg.recent,
        "minWgtKg": PQ.MIN_WGT_KG, "flatPct": PQ.FLAT_PCT,
        "verify": vrows, "headline": head, "items": items, "merge": merge,
        "total": total, "groups": groups, "costs": costs, "spread": spread,
        "leadlag": leadlag, "countries": ctys, "breadth": bre,
        "places": places, "intl": intl, "drafts": drafts,
        "rejected": cfg.rejected,
        "sources": [
            {"name": "한국 수출입", "who": "관세청 품목별 국가별 수출입실적 "
                                        "(공공데이터포털 data.go.kr)",
             "freq": "월 1회 · 매월 15일경, 과거 월 소급 정정", "unit": "USD · kg"},
            {"name": "한국 시군구", "who": "관세청 시군구별 품목별 수출입실적",
             "freq": "월 1회 · **중량 없음**, 2026-09-01 이후 HSK10·중량 비공개",
             "unit": "USD"},
            {"name": "코드·품명", "who": "관세율표(2025-01-01 발효) — OpenAPI 응답 "
                                       "품명으로 매월 재대조",
             "freq": "연 1회 개정", "unit": "—"},
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default=str(SITE / "data"))
    args = ap.parse_args()

    cfg = P.load()
    errs = cfg.validate()
    if errs:
        print("PCB 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        payload = build(store, cfg)
    if not payload:
        print("PCB 데이터가 없습니다. scripts/run_watchlist.py 를 먼저 실행하세요.")
        return 1
    (out / "pcb.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    h = payload["headline"]
    print(f"pcb.json — 기준월 {payload['asOf']}")
    print("  코드 검증:")
    for v in payload["verify"]:
        mark = "✓" if v["counted"] else ("·" if v["status"] == "draft" else "✗")
        print(f"    {mark} {v['code']} {v['label']:<26} {v['why']}"
              + (f"  [{v['name']}]" if v["name"] else ""))
    for key, lab in (("month", f"단월 {payload['asOf']}"),
                     ("recent", f"최근 {payload['recent']}M"),
                     ("window", f"{payload['window']}M")):
        d = h[key]
        print(f"  {lab:<12} 금액 {str(d['valueYoy']):>7}% · 중량 {str(d['qtyYoy']):>7}% "
              f"· ASP {str(d['priceYoy']):>7}% (${d['asp']}/kg) [{d['verdict']['label']}]")
    if h["aspMax"]:
        print(f"  ASP 최고 ${h['aspMax']}/kg @ {h['aspMaxAt']}"
              + ("  ← 현재가 최고치" if h.get("aspNowIsMax") else ""))
    for g in payload["groups"]:
        print(f"  {g['label']:<20} ${str(g['usd']):>8}M  YoY {str(g['yoy']):>7}% → "
              f"{str(g['recentYoy']):>7}%  [{g['verdict']['label']}]")
    sp = payload["spread"]
    print(f"  원가 대조 — {sp['note'][:110]}")
    if sp["linked"]:
        print(f"     잔차 {sp['e3m']} → {sp['eNow']} $/kg  [{sp['verdict']['label']}]")
    for m in payload["merge"]:
        print(f"  합산검사 {m['a']} vs {m['b']}: r={m['r']} [{m['verdict']['label']}]")
    for ll in payload["leadlag"]:
        print(f"  선후행 {ll['lead']}→{ll['lag']}: 최적 시차 {ll['bestK']}개월 r={ll['bestR']}")
    pl = payload["places"]
    if pl.get("ok"):
        for r in pl["rows"][:10]:
            print(f"  지역 {r['place']:<16} ${str(r['usd']):>8}M {str(r['yoy']):>7}% → "
                  f"{str(r['recentYoy']):>7}%{'  (고정)' if r['pinned'] else ''}")
    else:
        print(f"  지역 — {pl['note']}")
    it = payload["intl"]
    if it.get("ok"):
        print(f"  해외 대조축 — 일본 METI {it['asOf']} (표 {it['source']['statsDataId']})")
        for r in it["rows"]:
            print(f"    {r['label']:<24} 금액YoY {str(r['amt']['yoy']):>7}% "
                  f"최근 {str(r['amt']['recentYoy']):>7}% · 비중 "
                  f"{str(r['share']):>5}% ({str(r['shareChg'])}%p)"
                  + ("" if r["seen"] else "  ← 데이터 없음"))
        m = it["mix"]
        print(f"    고부가 비중 {m['prev']}% → {m['now']}% ({m['chg']}%p)  "
              f"[{it['cross']['verdict']['label']}]")
        if it["source"]["missing"]:
            print(f"    ※ 못 받은 계열: {it['source']['missing']}")
    else:
        print(f"  해외 대조축 — 미연결: {it['note'][:80]}")
        for i in it["items"]:
            need = i["needKey"] or "키 불필요"
            print(f"    {i['name']:<28} {i['who'][:34]:<34} {need}"
                  + ("  (보유)" if i["have"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
