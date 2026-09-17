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
from kortrade.codes import bootstrap
from kortrade.store import Store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre", default="202601", help="개편 전 기준월 YYYYMM")
    ap.add_argument("--post", default=None, help="개편 후 기준월 YYYYMM (예: 202608)")
    ap.add_argument("--db", default="data/kortrade.sqlite")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = CustomsClient()
    with Store(args.db) as store:
        tables = bootstrap(client, store, pre_reorg_yymm=args.pre, post_reorg_yymm=args.post)

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
