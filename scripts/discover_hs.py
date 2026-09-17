#!/usr/bin/env python3
"""신호가 어느 HS 코드에 실려 있는지 역탐색한다.

파마리서치(강릉) 케이스처럼 "회사가 뭘 만드는지는 알지만 어느 HS 로 나가는지는 모르는" 상황을
데이터로 푼다. 후보 HS 코드(core + probe 전부)를 해당 시도에 대해 수집한 뒤
목표 시군구에서 금액이 실제로 잡히는 코드를 금액순으로 보여준다.

사용법:
    python scripts/discover_hs.py --sido 강원 --sigungu 강릉시 --start 202201
    python scripts/discover_hs.py --hs 330499 --sido 경기 충북 세종 --mode where
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.client import CustomsClient
from kortrade.collect import Collector, hs_codes, latest_available_yymm, load_hs_config
from kortrade.discover import purity_report, scan_hs, scan_sigungu
from kortrade.store import Store

pd.set_option("display.width", 200)
pd.set_option("display.unicode.east_asian_width", True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["what", "where"], default="what",
                    help="what=지역 고정 후 HS 탐색 / where=HS 고정 후 지역 탐색")
    ap.add_argument("--sido", nargs="+", default=["강원"])
    ap.add_argument("--sigungu", default="강릉시")
    ap.add_argument("--hs", default=None, help="mode=where 일 때 대상 HS6")
    ap.add_argument("--start", default="202201")
    ap.add_argument("--end", default=None)
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--tiers", nargs="+", default=["core", "probe"])
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    end = args.end or latest_available_yymm()
    cfg = load_hs_config()

    client = CustomsClient()
    with Store(args.db) as store:
        if not store.sido_codes():
            print("먼저 scripts/bootstrap_codes.py 를 실행하세요.")
            return 1
        col = Collector(client=client, store=store)

        if args.mode == "what":
            cands = hs_codes(cfg, tiers=tuple(args.tiers))
            print(f"후보 HS {len(cands)}개 x 시도 {len(args.sido)}개 스캔 "
                  f"({args.start}~{end})  예상 호출 최대 "
                  f"{len(cands) * len(args.sido) * 3:,}회\n")
            frames = []
            for sido in args.sido:
                df = scan_sigungu(col, sido, args.sigungu, cands, args.start, end)
                if not df.empty:
                    df.insert(0, "sido", sido)
                    frames.append(df)
            if not frames:
                print(f"{args.sigungu} 에서 후보 HS 수출 기록을 찾지 못했습니다.")
                return 0
            out = pd.concat(frames).sort_values("최근12M_USD", ascending=False)
            print(f"\n=== {args.sigungu} 수출 — HS 코드별 ===")
            print(out.to_string(index=False))
            print("\n→ 상위 코드에 대해 purity 진단을 실행하세요:")
            for hs in out.head(3)["hs_code"]:
                print(f"   python scripts/report.py purity --hs {hs} --sigungu {args.sigungu}")

        else:
            if not args.hs:
                print("--hs 가 필요합니다.")
                return 1
            df = scan_hs(col, args.hs, args.sido, args.start, end)
            if df.empty:
                print("데이터 없음")
                return 0
            print(f"\n=== HS {args.hs} 시군구별 수출 (최근 12개월) ===")
            print(df.to_string(index=False))
            print(f"\nHHI={df.attrs.get('HHI')}  1위비중={df.attrs.get('top1_share')}%"
                  f"  순도힌트={df.attrs.get('purity_hint')}")

        print(f"\nAPI 호출 {client.calls_made}회 사용")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
