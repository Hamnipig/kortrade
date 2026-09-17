#!/usr/bin/env python3
"""정기 갱신 엔트리포인트.

매월 관세청 공표(15일경) 이후 1회 실행하면 된다. 멱등(idempotent)하므로 여러 번 돌려도 안전하다.
확정 구간은 fetch_log 로 건너뛰고, 최근 6개월만 재수집해 소급 정정을 반영한다.

사용법:
    export DATA_GO_KR_SERVICE_KEY='발급받은키'
    python scripts/run_update.py                       # 기본: 2019-01 ~ 최신
    python scripts/run_update.py --start 202201
    python scripts/run_update.py --dry-run             # 호출 수만 추정
    python scripts/run_update.py --layer sector        # 섹터 레이어만
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.client import CustomsClient
from kortrade.collect import (Collector, estimate_calls, hs_codes, latest_available_yymm,
                              load_companies, load_hs_config)
from kortrade.codes import VERIFIED_SIDO_CODES
from kortrade.sectors import load_sectors
from kortrade.store import Store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="201901", help="수집 시작 YYYYMM")
    ap.add_argument("--end", default=None, help="수집 종료 YYYYMM (기본: 조회 가능한 최신월)")
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--layer", choices=["all", "sector", "company"], default="all")
    ap.add_argument("--sectors", default=None,
                    help="'all' 또는 쉼표구분 키(cosmetics,semiconductor). 지정하면 "
                         "config/sectors/*.yaml 의 active 섹터를 전국 시군구로 수집한다.")
    ap.add_argument("--revision-window", type=int, default=6,
                    help="매번 재수집할 최근 개월 수 (소급 정정 반영)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("run_update")

    end = args.end or latest_available_yymm()
    hs_cfg = load_hs_config()
    comp_cfg = load_companies()

    core = hs_codes(hs_cfg, tiers=("core",))
    countries = hs_cfg["countries"]

    company_hs = sorted({h for c in comp_cfg.get("companies", {}).values()
                         for h in c.get("hs_watch", [])})
    sidos = sorted({c["sido"] for c in comp_cfg.get("companies", {}).values() if c.get("sido")}
                   | {s["sido"] for c in comp_cfg.get("companies", {}).values()
                      for s in (c.get("secondary_sites") or []) if s.get("sido")})

    print(f"수집 구간      : {args.start} ~ {end}")

    if args.sectors:
        # 섹터 모드는 시군구 API 만 쓴다. 위 숫자(국가별/기업 레이어)는 돌지 않으므로 찍지 않는다.
        want = None if args.sectors == "all" else set(args.sectors.split(","))
        secs = [s for s in load_sectors(include_draft=False) if want is None or s.key in want]
        n_sido = len(VERIFIED_SIDO_CODES)   # 개편 전/후 합집합이라 실제로는 1~2개 더 많다
        est = 0
        for sec in secs:
            n = estimate_calls(len(sec.codes), n_sido, args.start, end)
            est += n
            print(f"  섹터 '{sec.key}' : HS {len(sec.codes)}개 x 시도 {n_sido}개 → 최대 {n:,}콜")
        print(f"합계(최초 1회) : 최대 {est:,}콜  / 일 예산 9,000콜")
    else:
        est_sector = estimate_calls(len(core), len(countries), args.start, end)
        est_total = estimate_calls(len(core), 1, args.start, end)
        est_company = estimate_calls(len(company_hs), len(sidos), args.start, end)
        print(f"섹터 레이어    : HS {len(core)}개 x 국가 {len(countries)}개  → 최대 {est_sector:,}콜")
        print(f"섹터 합계      : HS {len(core)}개                        → 최대 {est_total:,}콜")
        print(f"기업 레이어    : HS {len(company_hs)}개 x 시도 {len(sidos)}개      → 최대 {est_company:,}콜")
        print(f"합계(최초 1회) : 최대 {est_sector + est_total + est_company:,}콜"
              f"  / 일 예산 9,000콜")
    print("  ※ 2회차부터는 확정 구간을 건너뛰므로 실제 호출은 이보다 훨씬 적습니다.")

    if args.dry_run:
        return 0

    client = CustomsClient()
    with Store(args.db) as store:
        if not store.all_sido_names():
            # DB 파일은 있는데 표가 비어 있는 경우가 실제로 있었다(부트스트랩 중단).
            # 파일 존재로 판단하는 가드에 걸리지 않도록 메시지를 명확히 한다.
            print(f"\n{args.db} 에 시도코드 표가 비어 있습니다."
                  "\n  python scripts/bootstrap_codes.py --post <최신월> --if-missing"
                  "\n를 먼저 실행하세요. (DB 파일이 있어도 표는 비어 있을 수 있습니다)")
            return 1

        col = Collector(client=client, store=store, revision_window=args.revision_window)

        # --- 섹터 모드: 전국 17개 시도 x 섹터 HS6 (대시보드가 쓰는 데이터) ---
        if args.sectors:
            want = None if args.sectors == "all" else set(args.sectors.split(","))
            secs = [s for s in load_sectors(include_draft=False)
                    if want is None or s.key in want]
            if not secs:
                log.warning("수집할 active 섹터가 없습니다 (draft 는 제외됩니다)")
            # 개편 전/후 시도명을 **합집합**으로 돈다. store.sido_codes() 는 최신 표만
            # 주므로 개편 전 구간의 '광주광역시'·'전라남도'를 통째로 놓친다.
            # 각 시도가 존재하지 않던 구간은 collect_region 이 건너뛴다.
            all_sido = store.all_sido_names() or list(VERIFIED_SIDO_CODES.values())
            for sec in secs:
                log.info("=== 섹터 '%s' — HS %d개 x 시도 %d개 ===",
                         sec.key, len(sec.codes), len(all_sido))
                for sido in all_sido:
                    st = col.collect_region(sido, sec.codes, args.start, end)
                    log.debug("  %s: %s", sido, st)

        if args.layer in ("all", "sector") and not args.sectors:
            log.info("=== 레이어 1: 섹터 (전국 HS x 국가) ===")
            st = col.collect_sector(core, countries, args.start, end)
            log.info("섹터 국가별: %s", st)
            st = col.collect_sector_total(core, args.start, end)
            log.info("섹터 합계:   %s", st)

        if args.layer in ("all", "company"):
            log.info("=== 레이어 2: 기업 (시군구 x HS6) ===")
            st = col.collect_for_companies(comp_cfg, args.start, end)
            log.info("기업 레이어: %s", st)

        cov = store.coverage()
        print("\n--- 적재 현황 ---")
        for t, info in cov.items():
            print(f"  {t}: {info}")
        print(f"  API 호출 {client.calls_made}회 사용")

        if cov["revisions"]:
            rev = store.frame(
                "SELECT table_name, field, COUNT(*) n FROM revisions"
                " WHERE noticed_at >= datetime('now','-1 day')"
                " GROUP BY table_name, field"
            )
            if not rev.empty:
                print("\n--- 이번 실행에서 감지된 소급 정정 ---")
                print(rev.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
