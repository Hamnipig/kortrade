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
import time
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
    ap.add_argument("--window-months", type=int, default=12,
                    help="1콜당 조회 개월. 긴 구간에서 응답이 느리면 6이나 3으로 줄인다.")
    ap.add_argument("--budget-seconds", type=float, default=420,
                    help="벽시계 예산. 넘기면 멈추고 어디까지 했는지 보고한다. 0=무제한")
    ap.add_argument("--retries", type=int, default=2,
                    help="속보는 10일마다 다시 받으므로 실패한 콜을 오래 붙들 이유가 없다")
    ap.add_argument("--read-timeout", type=int, default=45)
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

    nw = len(list(chunk_periods(start, end, args.window_months)))
    print(f"수집 구간 : {start} ~ {end}  (창 {nw}개 x {args.window_months}개월)")
    print(f"종류      : {', '.join(kinds)}")
    print(f"호출      : {len(kinds) * nw}콜 / 일 예산 9,000콜"
          f" · 재시도 {args.retries}회 · 읽기 타임아웃 {args.read_timeout}s"
          f" · 시간 예산 {args.budget_seconds or '무제한'}s")
    print(f"품목 매핑 : {'검증됨' if cfg.item.verified else '미검증'}"
          f" — {', '.join(s.label for s in cfg.item.body())}")
    print(f"국가 매핑 : {'검증됨' if cfg.country.verified else '미검증 (수집만 하고 화면에는 내보내지 않음)'}")

    if args.dry_run:
        return 0

    client = CustomsClient(max_retries=max(1, args.retries),
                           timeout=(15, args.read_timeout))

    # ── 프로브: 1개월 1콜을 먼저 던져 응답 속도를 잰다 ──────────────────────
    # 잡이 통째로 타임아웃되면 어느 단계가 먹었는지 로그에 남지 않는다. 그래서
    # 본 수집 전에 **가장 작은 요청**으로 이 API 가 살아 있는지와 얼마나 걸리는지
    # 먼저 찍어 둔다. 이 한 줄이 다음 실패 때 원인을 바로 가리킨다.
    client.set_budget(min(90.0, args.budget_seconds or 90.0))
    t0 = time.monotonic()
    try:
        probe = client.call(f"flash_{kinds[0]}", strtYymm=end, endYymm=end)
        print(f"  프로브: flash_{kinds[0]} {end} 1개월 → {len(probe)}행 "
              f"/ {client.last_elapsed:.1f}s")
        if client.last_elapsed > 20:
            print(f"  ⚠ 1개월 요청에 {client.last_elapsed:.0f}초 걸렸습니다. "
                  f"--window-months 를 6 이하로 줄이세요.")
    except Exception as exc:                      # noqa: BLE001
        print(f"  ✗ 프로브 실패 ({time.monotonic()-t0:.0f}s): {exc}")
        print("    → 이 엔드포인트가 응답하지 않습니다. 활용신청 승인 여부와 "
              "네트워크를 확인하세요. 본 수집은 건너뜁니다.")
        return 2

    with Store(args.db) as store:
        col = Collector(client=client, store=store,
                        flash_window_months=args.window_months)
        client.set_budget(args.budget_seconds or None)
        t0 = time.monotonic()
        st = col.collect_flash(kinds, start, end)
        print(f"\n  적재: {st}  ({time.monotonic()-t0:.0f}s 소요)")
        print(f"  API 호출 {client.calls_made}회 사용")
        if client.out_of_budget():
            print("  ⚠ 시간 예산을 소진해 일부 구간을 건너뛰었습니다. "
                  "다음 실행이 이어받습니다(수집은 멱등).")
        row = store.conn.execute(
            "SELECT COUNT(*) n, MIN(period) a, MAX(period) b,"
            " MAX(CASE WHEN period=(SELECT MAX(period) FROM flash_trade) THEN seq END) s"
            " FROM flash_trade").fetchone()
        print(f"  flash_trade: {row['n']}행 · {row['a']}~{row['b']}"
              f" · 최신월 최종 순 {F.SEQ_LABEL.get(row['s'], '–')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
