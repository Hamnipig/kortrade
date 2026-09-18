#!/usr/bin/env python3
"""전산업 워치리스트 수집.

섹터 수집(run_update.py)과 분리돼 있다. 쓰는 API 가 다르기 때문이다.
  run_update.py   시군구별 품목별  → 지역 분해 (기업 프록시)
  이 스크립트      품목별 국가별    → 국가 분해 + 중량 (P·Q 분해)

품목별 국가별 API 는 6단위로 요청하면 하위 10단위를 전부 돌려주므로,
워치리스트의 22개 6단위만 받으면 23개 품목의 10단위가 모두 채워진다.

사용법:
    export DATA_GO_KR_SERVICE_KEY='...'
    python scripts/run_watchlist.py --start 202001
    python scripts/run_watchlist.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import chains as C
from kortrade import watchlist as W
from kortrade.client import CustomsClient, chunk_periods
from kortrade.collect import Collector, latest_available_yymm
from kortrade.store import Store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="202001")
    ap.add_argument("--end", default=None)
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--revision-window", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-countries", action="store_true",
                    help="국가 분해 없이 전국 합계만 (콜 수 1/7)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("watchlist")

    wl = W.load()
    errs = wl.validate()
    if errs:
        print("워치리스트 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    # 밸류체인(해외 현지생산 보정)이 쓰는 코드도 같은 API 로 받는다.
    cs = C.load()
    cerrs = cs.validate()
    if cerrs:
        print("체인 설정 오류:")
        for e in cerrs:
            print("  -", e)
        return 1

    end = args.end or latest_available_yymm()
    parents = sorted(set(wl.all_parents()) | set(cs.all_parents()))
    nw = len(list(chunk_periods(args.start, end, 12)))

    # (6단위, 국가) 조합 — 품목마다 필요한 국가가 다르므로 전조합을 돌지 않는다
    pairs = {(p, c) for i in wl.active() for p in i.parents for c in i.countries}
    # 현지화는 국가 단위로 일어난다 — 체인이 지정한 시장은 반드시 국가 분해가 필요하다
    for ch in cs.chains:
        for code in ch.codes():
            for mkt in ch.markets:
                pairs.add((code[:6], mkt))
    pairs = sorted(pairs)

    print(f"수집 구간   : {args.start} ~ {end}  (창 {nw}개)")
    print(f"체인        : {len(cs.chains)}개 ({', '.join(c.name for c in cs.chains)})")
    print(f"품목        : active {len(wl.active())} / 전체 {len(wl.items)}"
          f"  (draft {[i.key for i in wl.items if not i.active]})")
    print(f"전국 합계   : HS6 {len(parents)}개            → {len(parents) * nw:,}콜")
    if not args.no_countries:
        print(f"국가 분해   : (HS6,국가) {len(pairs)}조합  → {len(pairs) * nw:,}콜")
    total = (len(parents) + (0 if args.no_countries else len(pairs))) * nw
    print(f"합계(최초)  : 최대 {total:,}콜 / 일 예산 9,000콜")
    print("  ※ 2회차부터는 확정 구간을 건너뛰므로 실제 호출은 훨씬 적습니다.")

    if args.dry_run:
        return 0

    client = CustomsClient()
    with Store(args.db) as store:
        col = Collector(client=client, store=store, revision_window=args.revision_window)

        log.info("=== 워치리스트: 전국 합계 (품목별 API) ===")
        st = col.collect_sector_total(parents, args.start, end)
        log.info("전국 합계: %s", st)

        if not args.no_countries:
            log.info("=== 워치리스트: 국가 분해 (품목별 국가별 API) ===")
            by_parent: dict[str, list[str]] = {}
            for p, c in pairs:
                by_parent.setdefault(p, []).append(c)
            for p, cs in sorted(by_parent.items()):
                st = col.collect_sector([p], {c: wl.countries.get(c, c) for c in cs},
                                        args.start, end)
                log.info("  %s (%d개국): %s", p, len(cs), st)

        print(f"\n  API 호출 {client.calls_made}회 사용")
        cov = store.coverage()
        print(f"  sector_trade 적재: {cov.get('sector_trade')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
