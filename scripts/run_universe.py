#!/usr/bin/env python3
"""전산업 유니버스 수집 — 97개 장 전수.

품목별 API 는 hsSgn 에 2단위(장)를 넣으면 그 장의 **모든 10단위**를 한 번에 돌려준다
(실측: 85류 12개월 = 8,534행, 02류 12개월 = 18,294행, 잘림 없음).
따라서 97콜 x 창 수로 대한민국 수출 전량을 덮을 수 있다.

담을 때는 **4단위로 집계**한다. 10단위 전수는 약 96만행이라 DB 가 GitHub 100MB
파일 제한을 넘는다. 10단위가 필요한 품목은 watchlist 로 따로 받는다.

사용법:
    export DATA_GO_KR_SERVICE_KEY='...'
    python scripts/run_universe.py --start 202001
    python scripts/run_universe.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.client import CustomsClient, CustomsAPIError, chunk_periods, normalize_period
from kortrade.collect import Collector, latest_available_yymm
from kortrade.store import Store

CONFIG = Path(__file__).resolve().parent.parent / "config" / "hs_chapters.yaml"

# 77류는 HS 상 유보(미사용)라 조회해도 늘 빈 응답이다.
SKIP = {"77"}


def chapters() -> dict[str, str]:
    d = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["chapters"]
    return {str(k).zfill(2): v for k, v in d.items() if str(k).zfill(2) not in SKIP}


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def collect(col: Collector, chs: dict[str, str], start: str, end: str) -> dict[str, int]:
    totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    for i, (cd, name) in enumerate(sorted(chs.items()), 1):
        for s, e, force in col._windows(start, end):
            params = {"strtYymm": s, "endYymm": e, "hsSgn": cd}
            try:
                rows = col._fetch("item", params, force)
            except CustomsAPIError as exc:
                logging.getLogger("universe").error("%s류 %s~%s 실패: %s", cd, s, e, exc)
                continue

            # (period, hs4) 로 집계하면서, 그 항에서 금액 1위인 10단위 품명을 라벨로 잡는다
            agg: dict[tuple[str, str], dict] = defaultdict(
                lambda: {"usd": 0.0, "kg": 0.0, "imp": 0.0, "top": ("", 0.0)})
            for r in rows:
                period = normalize_period(r.get("period", ""))
                code = (r.get("hs_code") or "").strip()
                # '총계' 행과 코드가 '-' 인 합계 행은 제외 (넣으면 두 배가 된다)
                if not period or not code or not code.isdigit() or len(code) < 4:
                    continue
                hs4 = code[:4]
                a = agg[(period, hs4)]
                usd = _num(r.get("exp_usd"))
                a["usd"] += usd
                a["kg"] += _num(r.get("exp_wgt"))
                a["imp"] += _num(r.get("imp_usd"))
                nm = (r.get("hs_name") or "").strip()
                if usd > a["top"][1] and nm and nm not in ("기타", "-"):
                    a["top"] = (nm, usd)

            recs = [{"period": p, "hs4": h, "hs2": h[:2],
                     "top_name": v["top"][0] or None,
                     "exp_usd": int(round(v["usd"])), "exp_wgt": int(round(v["kg"])),
                     "imp_usd": int(round(v["imp"]))}
                    for (p, h), v in agg.items()]
            st = col.store.upsert_universe(recs)
            for k in totals:
                totals[k] += st[k]
        logging.getLogger("universe").info("[%2d/%d] %s류 %s 완료 (누적 %s)",
                                           i, len(chs), cd, name, totals)
    return totals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="202001")
    ap.add_argument("--end", default=None)
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--revision-window", type=int, default=6)
    ap.add_argument("--only", default=None, help="쉼표구분 장 코드 (예: 85,87)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    end = args.end or latest_available_yymm()
    chs = chapters()
    if args.only:
        want = {c.strip().zfill(2) for c in args.only.split(",")}
        chs = {k: v for k, v in chs.items() if k in want}
    nw = len(list(chunk_periods(args.start, end, 12)))

    print(f"수집 구간 : {args.start} ~ {end}  (창 {nw}개)")
    print(f"대상      : {len(chs)}개 장 (77류 유보 제외)")
    print(f"호출      : {len(chs)} x {nw} = 최대 {len(chs) * nw:,}콜 / 일 예산 9,000콜")
    print(f"적재 단위 : HS 4단위 (10단위는 watchlist 로 따로)")
    if args.dry_run:
        return 0

    client = CustomsClient()
    with Store(args.db) as store:
        col = Collector(client=client, store=store, revision_window=args.revision_window)
        st = collect(col, chs, args.start, end)
        print(f"\n적재: {st}")
        print(f"API 호출 {client.calls_made}회")
        n = store.frame("SELECT COUNT(*) n, COUNT(DISTINCT hs4) h4,"
                        " MIN(period) f, MAX(period) t FROM universe_trade")
        print(f"universe_trade: {n.to_dict('records')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
