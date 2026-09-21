#!/usr/bin/env python3
"""속보 슬롯 번호 ↔ 실제 품목/국가 대응 검증.

이 API 의 가장 위험한 점은 응답 필드가 itemUsdAmt00~10 으로 **번호뿐**이라는 것이다.
품목 API 와 국가 API 의 응답 구조가 글자 하나 다르지 않아서, 순서를 한 칸 밀려
읽어도 숫자는 멀쩡해 보인다. 이 프로젝트에서 이미 HSK 코드 12개를 잘못 읽은 적이
있으므로, 선언된 순서를 믿지 않고 **독립적으로 모은 데이터와 맞춰서** 확인한다.

  --items      DB 안의 데이터만 쓴다 (API 호출 0회)
               · slot00(전체)  vs  universe_trade 97개 장 합계
               · slot08        vs  sector_trade 의 SSD(8523511000) 실측
  --countries  국가별 전체 수출 월계를 품목별국가별 API 로 따로 받아 슬롯과 맞춘다
               · 국가 10개 x 창 2개 = 약 20콜

통과하면 data/flash_verify.json 을 남기고, build_flash.py 가 그 파일을 보고
국가 슬롯을 화면에 개방한다. 설정 파일(config/flash.yaml)은 사람이 쓴 기본값
그대로 두고, 기계가 확인한 사실만 별도 파일로 분리한다.

사용법:
    python scripts/verify_flash.py --items
    DATA_GO_KR_SERVICE_KEY='...' python scripts/verify_flash.py --items --countries
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import flash as F
from kortrade.client import CustomsClient, chunk_periods, normalize_period
from kortrade.store import Store

KST = timezone(timedelta(hours=9))

# 통과 기준. 느슨하게 잡으면 검증이 아니라 추인이 된다.
MIN_R = 0.90          # 전월비 증감률 상관
RATIO_LO, RATIO_HI = 0.90, 1.10   # 잠정/확정 수준 비율 (잠정치라 완전 일치는 안 된다)


def corr(a: list[float], b: list[float]) -> float | None:
    if len(a) < 6 or len(a) != len(b):
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    if da == 0 or db == 0:
        return None
    return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db), 4)


def growth(v: list[float]) -> list[float]:
    return [v[i] / v[i - 1] - 1 for i in range(1, len(v)) if v[i - 1] > 0]


def aligned(a: dict[str, float], b: dict[str, float]) -> tuple[list, list, list]:
    ks = sorted(set(a) & set(b))
    return ks, [a[k] for k in ks], [b[k] for k in ks]


def flash_series(store: Store, kind: str, slot: str) -> dict[str, float]:
    """월전체(잠정, seq=3) 시계열."""
    rows = store.conn.execute(
        "SELECT period, exp_usd FROM flash_trade WHERE kind=? AND slot=? AND seq=3",
        (kind, slot)).fetchall()
    return {r["period"]: float(r["exp_usd"] or 0) for r in rows if r["exp_usd"]}


# ------------------------------------------------------------------ 품목 검증

def verify_items(store: Store, cfg: F.FlashConfig) -> dict:
    out = {"checks": [], "ok": True}

    def check(name, fa, fb, expect_ratio, why):
        ks, a, b = aligned(fa, fb)
        r = corr(growth(a), growth(b))
        ratios = [x / y for x, y in zip(a, b) if y > 0]
        med = round(statistics.median(ratios), 3) if ratios else None
        lo, hi = expect_ratio
        ok = (r is not None and r >= MIN_R and med is not None and lo <= med <= hi)
        out["checks"].append({"name": name, "months": len(ks), "r": r,
                              "ratioMedian": med, "expect": [lo, hi],
                              "ok": ok, "why": why})
        out["ok"] = out["ok"] and ok

    # 1) slot00(전체) vs 97개 장 전수 합계. 잠정↔확정이라 ±5% 안이면 정상.
    conf = {r["period"]: float(r["t"] or 0) for r in store.conn.execute(
        "SELECT period, SUM(exp_usd) t FROM universe_trade GROUP BY period")}
    if conf:
        check("slot00 전체 ↔ 유니버스(97개 장) 합계",
              flash_series(store, "item", "00"), conf, (0.95, 1.05),
              "두 경로로 받은 같은 달 전국 수출 총액. 단위(천달러↔달러)가 틀리면 1000배로 튄다.")

    # 2) slot08 vs SSD 실측. SSD 는 컴퓨터주변기기의 부분집합이므로 비율이 1 미만이어야 한다.
    ssd = {r["period"]: float(r["t"] or 0) for r in store.conn.execute(
        "SELECT period, SUM(exp_usd) t FROM sector_trade"
        " WHERE hs_code='8523511000' AND country_code='ALL' GROUP BY period")}
    if ssd:
        check("slot08 컴퓨터주변기기 ⊃ SSD(8523511000)",
              ssd, flash_series(store, "item", "08"), (0.50, 0.99),
              "SSD 가 부분집합이므로 비율은 1 미만이어야 하고, 움직임은 같이 가야 한다.")

    if not out["checks"]:
        out["ok"] = False
        out["note"] = "대조할 데이터가 DB 에 없습니다. run_universe / run_watchlist 를 먼저 돌리세요."
    return out


# ------------------------------------------------------------------ 국가 검증

def country_totals(client: CustomsClient, code: str,
                   start: str, end: str) -> tuple[dict[str, float], str]:
    """품목별국가별 API 로 그 나라의 **전체 품목** 월별 수출액을 받는다.

    ★ 이 호출은 hsSgn 을 비워 보낸다. 응답 크기가 예측되지 않는 유일한 호출이라
      (국가 합계 1행일 수도, 전 품목 수천 행일 수도 있다) 이 스크립트 전체에
      벽시계 예산을 두고, 한 나라가 실패해도 나머지는 계속 간다.
      EU 처럼 이 API 의 국가코드 체계에 없을 수 있는 값도 섞여 있다.
    """
    acc: dict[str, list[tuple[int, float]]] = {}
    for s, e in chunk_periods(start, end, client.max_months_per_call):
        if client.out_of_budget():
            break
        for r in client.call("item_country", strtYymm=s, endYymm=e, cntyCd=code):
            p = normalize_period(r.get("period", ""))
            v = (r.get("exp_usd") or "").replace(",", "").strip()
            if not p or not v:
                continue
            try:
                acc.setdefault(p, []).append((len(r.get("hs_code") or ""), float(v)))
            except ValueError:
                continue
    # hsSgn 을 비우면 보통 국가 합계 1행이 온다. 혹시 여러 단위가 섞여 오면
    # **가장 깊은 단위만** 더한다 — 섞어 더하면 이중계상이다.
    out, mode = {}, "single"
    for p, vs in acc.items():
        depths = {d for d, _ in vs}
        if len(vs) > 1:
            mode = "rollup"
            deep = max(depths)
            out[p] = sum(v for d, v in vs if d == deep)
        else:
            out[p] = vs[0][1]
    return out, mode


def verify_countries(store: Store, cfg: F.FlashConfig, client: CustomsClient,
                     months_back: int = 12) -> dict:
    months = sorted(flash_series(store, "country", "00"))
    if len(months) < 8:
        return {"ok": False, "note": "국가 속보 데이터가 부족합니다. run_flash.py --kinds country 실행 필요."}
    # 검증에 24개월을 다 쓸 이유가 없다. 12개월이면 상관·레벨 판정에 충분하고
    # 호출 수와 응답 크기가 절반이 된다.
    months = months[-months_back:]
    start, end = months[0].replace("-", ""), months[-1].replace("-", "")

    cands = [(s.slot, s.label, s.code) for s in cfg.country.body() if s.code]
    actual: dict[str, dict[str, float]] = {}
    skipped = []
    for _, label, code in cands:
        if client.out_of_budget():
            skipped.append(label)
            continue
        try:
            ser, mode = country_totals(client, code, start, end)
        except Exception as exc:                  # noqa: BLE001
            # 한 나라가 실패해도 검증 전체를 멈추지 않는다. EU 처럼 이 API 의
            # 국가코드가 아닐 수도 있고, 일시적 네트워크 문제일 수도 있다.
            print(f"  ✗ {label}({code}): {exc}")
            skipped.append(label)
            continue
        actual[code] = ser
        left = client.budget_left()
        print(f"  {label}({code}): {len(ser)}개월 수집 [{mode}] {client.last_elapsed:.1f}s"
              f"{'' if left is None else f' (남은 예산 {left:.0f}s)'}")
    if skipped:
        print(f"  … 건너뜀: {', '.join(skipped)}")

    results, ok_all = [], True
    for slot, label, code in cands:
        fs = flash_series(store, "country", slot)
        scores = []
        for _, lb2, c2 in cands:
            ks, a, b = aligned(fs, actual.get(c2, {}))
            if len(ks) < 8:
                continue
            r = corr(growth(a), growth(b))
            ratios = [x / y for x, y in zip(a, b) if y > 0]
            med = statistics.median(ratios) if ratios else None
            if r is None or med is None:
                continue
            # 레벨이 맞고 움직임이 같은 후보를 고른다
            scores.append((abs(med - 1) + (1 - r), lb2, c2, round(r, 4), round(med, 3)))
        scores.sort()
        best = scores[0] if scores else None
        ok = bool(best and best[2] == code and best[3] >= MIN_R
                  and RATIO_LO <= best[4] <= RATIO_HI)
        ok_all = ok_all and ok
        results.append({"slot": slot, "declared": label, "declaredCode": code,
                        "bestMatch": best[1] if best else None,
                        "r": best[3] if best else None,
                        "ratioMedian": best[4] if best else None, "ok": ok})
    if skipped:
        ok_all = False
    return {"ok": ok_all, "slots": results, "skipped": skipped,
            "note": ("선언 순서가 실측과 일치합니다." if ok_all else
                     (f"{len(skipped)}개국을 확인하지 못했습니다({', '.join(skipped)}). "
                      "국가 슬롯은 닫아 둡니다." if skipped else
                      "선언 순서와 실측이 어긋납니다. config/flash.yaml 의 country.slots 를 "
                      "bestMatch 에 맞춰 고친 뒤 다시 검증하세요."))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--out", default="data/flash_verify.json")
    ap.add_argument("--items", action="store_true")
    ap.add_argument("--countries", action="store_true")
    ap.add_argument("--months", type=int, default=12,
                    help="국가 검증에 쓸 개월 수. 12면 상관·레벨 판정에 충분하다.")
    ap.add_argument("--budget-seconds", type=float, default=300,
                    help="국가 검증의 벽시계 예산. 넘기면 남은 나라를 건너뛰고 닫아 둔다. 0=무제한")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--read-timeout", type=int, default=45)
    args = ap.parse_args()
    if not (args.items or args.countries):
        args.items = True

    cfg = F.load()
    payload = {"checkedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
               "configVersion": cfg.version}
    rc = 0

    with Store(args.db) as store:
        if args.items:
            print("[품목 슬롯 검증] DB 내부 대조 — API 호출 없음")
            r = verify_items(store, cfg)
            payload["item"] = r
            for c in r["checks"]:
                mark = "통과" if c["ok"] else "실패"
                print(f"  [{mark}] {c['name']}  n={c['months']}개월 "
                      f"r={c['r']} 비율중앙값={c['ratioMedian']} "
                      f"(기대 {c['expect'][0]}~{c['expect'][1]})")
                print(f"         {c['why']}")
            if r.get("note"):
                print("  ", r["note"])
            rc = rc or (0 if r["ok"] else 1)

        if args.countries:
            print(f"[국가 슬롯 검증] 품목별국가별 API 로 국가별 월계를 따로 받아 대조"
                  f" (최근 {args.months}개월 · 예산 {args.budget_seconds or '무제한'}s)")
            client = CustomsClient(max_retries=max(1, args.retries),
                                   timeout=(15, args.read_timeout))
            client.set_budget(args.budget_seconds or None)
            r = verify_countries(store, cfg, client, months_back=args.months)
            payload["country"] = r
            for s in r.get("slots", []):
                mark = "일치" if s["ok"] else "불일치"
                print(f"  [{mark}] slot{s['slot']} 선언={s['declared']:<8} "
                      f"최적매칭={str(s['bestMatch']):<8} r={s['r']} 비율={s['ratioMedian']}")
            print("  ", r["note"])
            print(f"  API 호출 {client.calls_made}회 사용")
            rc = rc or (0 if r["ok"] else 1)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n검증 결과 기록: {args.out}")
    if args.countries and payload.get("country", {}).get("ok"):
        print("→ 국가 슬롯이 검증되었습니다. 다음 build_flash.py 실행부터 화면에 표시됩니다.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
