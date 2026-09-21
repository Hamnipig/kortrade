#!/usr/bin/env python3
"""속보 → site/data/flash.json  (API 호출 없음)

이 레이어가 내놓는 숫자는 네 종류이고, **신뢰 순서가 정해져 있다.**

  1. 점유율 변화(%p)   조업일수에 완전히 중립. 산업 간 상대 강도는 이걸로 본다.
  2. 월 착지 추정      작년 같은 달의 월중 분포를 가정해 조업일수를 상쇄한다.
  3. 평일 기준 일평균  공휴일을 반영하지 못한다(명절이 낀 순에서는 틀린다).
  4. 누계 YoY(원본)    조업일수 차이가 그대로 섞여 있다. 헤드라인일 뿐이다.

화면에서도 이 순서로 배치한다. 4번을 크게 띄우고 1번을 각주로 내리면
이 레이어는 계절성 잡음 생성기가 된다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import flash as F
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))

HIST = 24          # 월전체(잠정) 시계열 길이
M = 1_000_000.0


def _m(v):
    return None if v is None else round(v / M, 1)


def build(store: Store, cfg: F.FlashConfig) -> dict | None:
    rows = store.conn.execute(
        "SELECT period, seq, kind, slot, dt, day_to, exp_usd FROM flash_trade"
    ).fetchall()
    if not rows:
        return None

    val: dict[tuple, float] = {}
    dayto: dict[tuple, int] = {}
    dtstr: dict[tuple, str] = {}
    for r in rows:
        k = (r["kind"], r["slot"], r["period"], r["seq"])
        val[k] = float(r["exp_usd"] or 0)
        dayto[k] = int(r["day_to"] or 0)
        dtstr[k] = r["dt"] or ""

    kinds = [k for k in ("item", "country") if any(r["kind"] == k for r in rows)]
    if "item" not in kinds:
        return None

    months = sorted({r["period"] for r in rows if r["kind"] == "item"})
    latest = months[-1]
    # 최신월에서 실제로 들어온 가장 늦은 순. 월초에는 1, 월말 직후에는 3이 된다.
    seq = max(r["seq"] for r in rows if r["kind"] == "item" and r["period"] == latest)
    py = F.shift(latest, -12)

    def get(kind, slot, period, s):
        return val.get((kind, slot, period, s))

    def wd(period, kind, slot, s):
        """해당 (월, 순)의 평일수. day_to 는 응답 원문에서 가져온다 — 2월은 28/29다."""
        dd = dayto.get((kind, slot, period, s)) or dayto.get((kind, "00", period, s)) or 0
        if not dd:
            return None
        return F.weekdays(int(period[:4]), int(period[5:7]), dd) or None

    def block(kind: str, slot: str) -> dict:
        now = get(kind, slot, latest, seq)
        prev = get(kind, slot, py, seq)
        tot_now = get(kind, "00", latest, seq)
        tot_prev = get(kind, "00", py, seq)
        full_prev = get(kind, slot, py, 3)          # 작년 그 달 월전체(잠정)

        w_now, w_prev = wd(latest, kind, slot, seq), wd(py, kind, slot, seq)
        avg_now = (now / w_now) if (now and w_now) else None
        avg_prev = (prev / w_prev) if (prev and w_prev) else None

        sh_now, sh_prev = F.share(now, tot_now), F.share(prev, tot_prev)
        # 월 전체 평일수 — 잔여 기간을 채울 때 쓴다
        wf_now = F.weekdays(int(latest[:4]), int(latest[5:7]), 31)
        wf_prev = F.weekdays(int(py[:4]), int(py[5:7]), 31)
        est = F.landing_wd(now, prev, full_prev, w_now, w_prev, wf_now, wf_prev,
                           cfg.min_base_usd)

        # 월전체(잠정) 시계열과 그 전년비
        ser, ser_yoy = [], []
        for p in months[-HIST:]:
            v = get(kind, slot, p, 3)
            ser.append(_m(v))
            ser_yoy.append(F.pct(v, get(kind, slot, F.shift(p, -12), 3), cfg.min_base_usd))

        # 순(旬) 구간값 — 누계를 빼서 만든다. 중순/하순에 가속이 붙었는지 본다.
        c1, c2, c3 = (get(kind, slot, latest, s) for s in (1, 2, 3))
        segs = {"상순": _m(c1),
                "중순": _m(c2 - c1) if (c1 and c2) else None,
                "하순": _m(c3 - c2) if (c2 and c3) else None}

        return {
            "usd": _m(now), "prevUsd": _m(prev),
            "yoy": F.pct(now, prev, cfg.min_base_usd),
            "share": sh_now, "sharePrev": sh_prev,
            "shareChgPp": (None if (sh_now is None or sh_prev is None)
                           else round(sh_now - sh_prev, 2)),
            "wdAvg": _m(avg_now), "wdYoy": F.pct(avg_now, avg_prev),
            "wd": w_now, "wdPrev": w_prev,
            "wdFull": wf_now, "wdFullPrev": wf_prev,
            "est": _m(est), "estPrevFull": _m(full_prev),
            "estYoy": F.pct(est, full_prev, cfg.min_base_usd),
            "shapeRatio": F.shape_ratio(prev, full_prev),
            "series": ser, "seriesYoy": ser_yoy, "segs": segs,
        }

    def pack(ss: F.SlotSet) -> list[dict]:
        out = []
        for s in ss.body():
            b = block(ss.kind, s.slot)
            if b["usd"] is None:
                continue
            out.append({"slot": s.slot, "label": s.label, "hs": s.hs,
                        "tickers": s.tickers, "note": s.note, "lumpy": s.lumpy,
                        "watch": s.watch, "chain": s.chain, "stage": s.stage, **b})
        # 점유율 변화 내림차순 — 이 레이어에서 가장 믿을 수 있는 축을 기본 정렬로 둔다
        out.sort(key=lambda r: (r["shareChgPp"] is None, -(r["shareChgPp"] or 0)))
        return out

    items = pack(cfg.item)
    countries = pack(cfg.country) if (cfg.country.verified and "country" in kinds) else []

    # 파생 비율 (자동차 현지화, 메모리 대비 SSD)
    ratios = []
    for r in cfg.ratios:
        n_now, d_now = get("item", r.num, latest, seq), get("item", r.den, latest, seq)
        n_pre, d_pre = get("item", r.num, py, seq), get("item", r.den, py, seq)
        cur = round(n_now / d_now, 3) if (n_now and d_now) else None
        pre = round(n_pre / d_pre, 3) if (n_pre and d_pre) else None
        ratios.append({
            "key": r.key, "name": r.name,
            "num": cfg.item.label(r.num), "den": cfg.item.label(r.den),
            "now": cur, "prev": pre,
            "chgPct": F.pct(cur, pre), "risingMeans": r.rising_means,
            "note": " ".join(r.note.split()),
        })

    # ── 속보(잠정) vs 확정 정합성 ────────────────────────────────────────
    # 같은 달을 두 경로로 받은 값이 크게 어긋나면 매핑이나 단위를 의심해야 한다.
    # universe_trade 는 97개 장 전수라 합계가 곧 전국 수출 총액이다.
    rec = None
    conf = store.conn.execute(
        "SELECT period, SUM(exp_usd) t FROM universe_trade GROUP BY period"
    ).fetchall()
    conf_map = {r["period"]: float(r["t"] or 0) for r in conf}
    for p in reversed(months):
        f3, c = get("item", "00", p, 3), conf_map.get(p)
        if f3 and c:
            gap = (f3 / c - 1) * 100
            rec = {"period": p, "flash": _m(f3), "confirmed": _m(c),
                   "gapPct": round(gap, 2),
                   "ok": abs(gap) <= cfg.reconcile_tolerance_pct,
                   "tol": cfg.reconcile_tolerance_pct}
            break

    # 다음 발표 예정 — 관세청은 11일경/21일경/익월 1일경에 순차 공표한다
    nxt = {1: ("01~20일 누계", f"{latest[5:7].lstrip('0')}월 21일경"),
           2: ("월전체 잠정", f"{F.shift(latest, 1)[5:7].lstrip('0')}월 1일경"),
           3: ("다음 달 01~10일 누계", f"{F.shift(latest, 1)[5:7].lstrip('0')}월 11일경")}[seq]

    return {
        "version": cfg.version,
        "builtAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "asOf": {"period": latest, "seq": seq, "label": F.SEQ_LABEL[seq],
                 "span": F.SEQ_SPAN[seq], "dt": dtstr.get(("item", "00", latest, seq), ""),
                 "prevPeriod": py},
        "next": {"what": nxt[0], "when": nxt[1]},
        # 확정 월별 통계 대비 선행 일수. 월전체 잠정은 15일, 상순은 35일 앞선다.
        "leadDays": {1: 35, 2: 25, 3: 15}[seq],
        "total": block("item", "00"),
        "items": items,
        "itemVerified": cfg.item.verified,
        "itemNote": cfg.item.verified_note,
        "countries": countries,
        "countryVerified": cfg.country.verified,
        "countryNote": cfg.country.verified_note,
        "countryCollected": "country" in kinds,
        "ratios": ratios,
        "months": months[-HIST:],
        "reconcile": rec,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default=str(SITE / "data"))
    ap.add_argument("--verify", default="data/flash_verify.json",
                    help="verify_flash.py 가 남긴 검증 결과. 통과했으면 국가 슬롯을 개방한다.")
    args = ap.parse_args()

    cfg = F.load()

    # 설정 파일은 '사람이 쓴 기본값', 검증 파일은 '기계가 확인한 사실'. 후자만 개방권을 갖는다.
    vp = Path(args.verify)
    if vp.exists():
        try:
            v = json.loads(vp.read_text(encoding="utf-8"))
            if (v.get("country") or {}).get("ok"):
                cfg.country.verified = True
                cfg.country.verified_note = (
                    f"{v.get('checkedAt', '')} 검증 통과 — 품목별국가별 API 로 받은 "
                    f"국가별 월계와 슬롯 시계열이 일치했습니다.")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"검증 파일을 읽지 못했습니다({exc}). 국가 슬롯은 닫아 둡니다.")

    errs = cfg.validate()
    if errs:
        print("속보 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        payload = build(store, cfg)
    if not payload:
        print("속보 데이터가 없습니다. scripts/run_flash.py 를 먼저 실행하세요.")
        return 1

    (out / "flash.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    a = payload["asOf"]
    t = payload["total"]
    print(f"flash.json — {a['period']} {a['label']} "
          f"(확정치 대비 {payload['leadDays']}일 선행)")
    print(f"  전체 ${t['usd']}M · 누계YoY {t['yoy']}% · 착지추정 ${t['est']}M "
          f"(YoY {t['estYoy']}%) · 평일 {t['wd']}일(전년 {t['wdPrev']}일)")
    print("  품목 (점유율 변화 순):")
    for r in payload["items"]:
        print(f"    {r['label']:<12} ${str(r['usd']):>9}M  누계YoY {str(r['yoy']):>7}%"
              f"  점유율 {str(r['share']):>5}% ({r['shareChgPp']:+.2f}%p)"
              f"  착지 ${str(r['est']):>9}M ({r['estYoy']}%)")
    for r in payload["ratios"]:
        print(f"  {r['name']}: {r['prev']} → {r['now']} ({r['chgPct']}%)")
    rec = payload["reconcile"]
    if rec:
        mark = "OK" if rec["ok"] else "★괴리"
        print(f"  정합성 {rec['period']}: 잠정 ${rec['flash']}M vs 확정 ${rec['confirmed']}M "
              f"= {rec['gapPct']:+.2f}% [{mark}]")
    if not payload["countryVerified"]:
        print("  국가 슬롯: 미검증 — 화면에 내보내지 않습니다 "
              "(scripts/verify_flash.py --countries 로 검증)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
