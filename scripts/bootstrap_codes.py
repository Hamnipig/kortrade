#!/usr/bin/env python3
"""시도코드 표를 API 로부터 자동 생성한다. 최초 1회만 실행하면 된다.

사용법:
    export DATA_GO_KR_SERVICE_KEY='발급받은키'
    python scripts/bootstrap_codes.py                    # 개편 전 기준만
    python scripts/bootstrap_codes.py --post 202608      # 개편(2026-07) 후 표도 함께
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.client import CustomsClient
from kortrade.codes import MIN_PLAUSIBLE_SIDO, bootstrap
from kortrade.store import Store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre", default="202601", help="개편 전 기준월 YYYYMM")
    ap.add_argument("--post", default=None, help="개편 후 기준월 YYYYMM (예: 202608)")
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--exhaustive", action="store_true",
                    help="00~99 전수 탐색(100콜/표). 코드 체계가 또 바뀌었을 때만 쓴다.")
    ap.add_argument("--if-missing", action="store_true",
                    help="이미 표가 채워져 있으면 아무것도 하지 않는다 (워크플로용).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = CustomsClient()
    with Store(args.db) as store:
        # ★ DB **파일의 존재**로 판단하면 안 된다. Store 를 여는 것만으로 파일이
        #    생기므로, 부트스트랩이 중간에 죽어도 빈 파일이 남아 다음 실행이
        #    "이미 있음"으로 오판하고 수집 단계에서 멈춘다. 내용으로 판단한다.
        if args.if_missing and len(store.all_sido_names()) >= MIN_PLAUSIBLE_SIDO:
            print(f"시도코드 표 이미 있음 ({len(store.all_sido_names())}개) — 건너뜁니다")
            return 0
        tables = bootstrap(client, store, pre_reorg_yymm=args.pre,
                           post_reorg_yymm=args.post, exhaustive=args.exhaustive)

    if not tables:
        print("시도코드를 하나도 찾지 못했습니다. 인증키와 기준월을 확인하세요.")
        return 1

    for valid_from, table in tables.items():
        print(f"\n[{valid_from} 이후 유효] {len(table)}개")
        for cd, nm in sorted(table.items()):
            print(f"  {cd}  {nm}")
    print(f"\nAPI 호출 {client.calls_made}회 사용. -> {args.db} 에 저장 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
