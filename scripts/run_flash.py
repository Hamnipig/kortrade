#!/usr/bin/env python3
"""속보(10일 단위 잠정치) 수집.

다른 수집기와 호출 규모가 완전히 다르다. 품목·국가 각각 **구간당 1콜**이면 끝난다.
2년치를 통째로 다시 받아도 4콜이다. 그래서 캐시를 쓰지 않고 매번 전량 재수집한다.

전년 동기 비교가 이 레이어의 전부이므로 최소 **2년치**를 받아야 의미가 생긴다.
기본 시작월을 24개월 전으로 잡는 이유다.

사용법:
    export DATA_GO_KR_SERVICE_KEY='...'
    python scripts/run_flash.py
    python scripts/run_flash.py --start 202401 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import flash as F
from kortrade.client import CustomsClient, chunk_periods
from kortrade.collect import Collector, current_yymm, shift_yymm
from kortrade.store import Store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None,
                    help="기본값: 24개월 전 (전년 동기 비교에 2년이 필요하다)")
    ap.add_argument("--end", default=None, help="기본값: 이번 달")
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--kinds", default="item,country",
                    help="item / country / item,country")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = F.load()
    errs = cfg.validate()
    if errs:
        print("속보 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    end = args.end or current_yymm()
    start = args.start or shift_yymm(end, -24)
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    bad = [k for k in kinds if k not in ("item", "country")]
    if bad:
        print(f"알 수 없는 종류: {bad}")
        return 1

    nw = len(list(chunk_periods(start, end, 12)))
    print(f"수집 구간 : {start} ~ {end}  (창 {nw}개)")
    print(f"종류      : {', '.join(kinds)}")
    print(f"호출      : {len(kinds) * nw}콜 / 일 예산 9,000콜")
    print(f"품목 매핑 : {'검증됨' if cfg.item.verified else '미검증'}"
          f" — {', '.join(s.label for s in cfg.item.body())}")
    print(f"국가 매핑 : {'검증됨' if cfg.country.verified else '미검증 (수집만 하고 화면에는 내보내지 않음)'}")

    if args.dry_run:
        return 0

    client = CustomsClient()
    with Store(args.db) as store:
        col = Collector(client=client, store=store)
        st = col.collect_flash(kinds, start, end)
        print(f"\n  적재: {st}")
        print(f"  API 호출 {client.calls_made}회 사용")
        row = store.conn.execute(
            "SELECT COUNT(*) n, MIN(period) a, MAX(period) b,"
            " MAX(CASE WHEN period=(SELECT MAX(period) FROM flash_trade) THEN seq END) s"
            " FROM flash_trade").fetchone()
        print(f"  flash_trade: {row['n']}행 · {row['a']}~{row['b']}"
              f" · 최신월 최종 순 {F.SEQ_LABEL.get(row['s'], '–')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
