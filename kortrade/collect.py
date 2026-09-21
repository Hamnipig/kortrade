"""2레이어 수집기.

레이어 1 (섹터): 전국 품목별 x 국가별.  어떤 품목/지역이 새로 강해지는지 판단용.
레이어 2 (기업): 시군구별 x 품목별.    제조장소 기준 집계 → 개별 기업 실적 프록시.

증분 갱신 원칙
  - 관세청은 매월 15일경 전월 자료를 공표하면서 과거 자료를 정정/취하 반영해 현행화한다.
  - 따라서 "확정 구간"(revision_window 이전)은 fetch_log 로 건너뛰고,
    최근 revision_window 개월은 매번 재수집해 UPSERT 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .client import (CustomsClient, CustomsAPIError, chunk_periods, hs6, month_range,
                     normalize_period, split_sigungu)
from .codes import sido_code_for
from .flash import UNIT_USD as FLASH_UNIT_USD, parse_dt, parse_period
from .store import Store

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 관세청 공표 시차: 매월 15일경 전월까지 현행화 → 안전하게 2개월 래그를 둔다.
PUBLICATION_LAG_MONTHS = 2
# 소급 정정이 실질적으로 발생하는 구간. 이 기간은 매 실행마다 재수집한다.
DEFAULT_REVISION_WINDOW = 6

# ★ 단위 차이 — 실데이터로 확인(2026-09):
#   품목별/품목별국가별 API(expDlr) : 달러
#   시군구별/시도별 API(expUsdAmt)  : **천 달러**
#   검증: 330499 2025-01 → 시군구 17개 시도 합계 562,366(천$) vs 품목별 562,368,250($) = 정확히 1000배
#   DB 에는 전부 달러로 정규화해 저장한다. 안 맞추면 두 레이어 비교가 1000배 틀어진다.
REGION_AMOUNT_UNIT_USD = 1_000


def _scale(v: str | None, factor: int) -> str | None:
    if v is None or v == "":
        return None
    try:
        return str(int(round(float(str(v).replace(",", "").strip()) * factor)))
    except (TypeError, ValueError):
        return None


def latest_available_yymm(today: date | None = None) -> str:
    """오늘 기준으로 조회 가능한 최신 월(YYYYMM)."""
    d = today or date.today()
    y, m = d.year, d.month
    # 15일 이전이면 전전월까지, 이후면 전월까지가 안전
    lag = PUBLICATION_LAG_MONTHS if d.day < 15 else PUBLICATION_LAG_MONTHS - 1
    m -= lag
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}{m:02d}"


def current_yymm(today: date | None = None) -> str:
    """속보는 **이번 달**까지 나온다. 월별 확정치와 달리 공표 래그를 빼면 안 된다."""
    d = today or date.today()
    return f"{d.year:04d}{d.month:02d}"


def shift_yymm(yymm: str, months: int) -> str:
    y, m = int(yymm[:4]), int(yymm[4:6])
    total = y * 12 + (m - 1) + months
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def load_hs_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_DIR / "hs_cosmetics.yaml").read_text(encoding="utf-8"))


def load_companies(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_DIR / "companies.yaml").read_text(encoding="utf-8"))


def hs_codes(cfg: dict, tiers: tuple[str, ...] = ("core",)) -> list[str]:
    out: list[str] = []
    for g in cfg["groups"].values():
        if g.get("tier", "core") in tiers:
            out.extend(g["codes"].keys())
    return sorted(set(out))


def hs_labels(cfg: dict) -> dict[str, str]:
    return {code: name for g in cfg["groups"].values() for code, name in g["codes"].items()}


def hs_group_of(cfg: dict) -> dict[str, str]:
    return {code: gname for gname, g in cfg["groups"].items() for code in g["codes"]}


@dataclass
class Collector:
    client: CustomsClient
    store: Store
    revision_window: int = DEFAULT_REVISION_WINDOW
    # 속보 API 가 긴 조회구간에서 느려지는 경우를 대비해 창 크기를 조절할 수 있게 둔다.
    flash_window_months: int = 12

    # ------------------------------------------------------------ 공통

    def _windows(self, start: str, end: str):
        """(strt, end, force_refetch) 구간들을 생성한다."""
        cutoff = shift_yymm(end, -self.revision_window + 1)
        for s, e in chunk_periods(start, end, self.client.max_months_per_call):
            yield s, e, (e >= cutoff)

    def _fetch(self, endpoint: str, params: dict, force: bool) -> list[dict]:
        if not force and self.store.already_fetched(endpoint, params):
            log.debug("skip (cached): %s %s", endpoint, params)
            return []
        try:
            rows = self.client.call(endpoint, **params)
        except CustomsAPIError as exc:
            if exc.fatal:
                raise
            self.store.mark_fetched(endpoint, params, 0, status=f"error: {exc}")
            log.error("수집 실패 %s %s: %s", endpoint, params, exc)
            return []
        self.store.mark_fetched(endpoint, params, len(rows))
        return rows

    # ------------------------------------------------------------ 레이어 1

    def collect_sector(self, codes: list[str], countries: dict[str, str],
                       start: str, end: str, include_total: bool = True) -> dict[str, int]:
        """전국 HS x 국가 수출입. include_total 이면 국가 합계도 별도 수집."""
        totals = {"inserted": 0, "updated": 0, "unchanged": 0}
        targets = list(countries.items())
        if include_total:
            # cntyCd 는 필수이므로 합계는 item 엔드포인트(국가 구분 없음)로 따로 받는다
            pass

        for hs in codes:
            for cc, cname in targets:
                for s, e, force in self._windows(start, end):
                    params = {"strtYymm": s, "endYymm": e, "hsSgn": hs, "cntyCd": cc}
                    rows = self._fetch("item_country", params, force)
                    recs = []
                    for r in rows:
                        period = normalize_period(r.get("period", ""))
                        code = (r.get("hs_code") or hs).strip()
                        if not period or not code:
                            continue
                        recs.append({
                            "period": period,
                            "hs_code": code,
                            "hs6": hs6(code),
                            "hs_name": r.get("hs_name"),
                            "country_code": (r.get("country_code") or cc).strip() or cc,
                            "country_name": r.get("country_name") or cname,
                            "exp_usd": r.get("exp_usd"), "exp_wgt": r.get("exp_wgt"),
                            "imp_usd": r.get("imp_usd"), "imp_wgt": r.get("imp_wgt"),
                            "bal_usd": r.get("bal_usd"),
                        })
                    # HS 2/4단위 입력 시 하위 코드가 함께 오는 경우가 있어 요청 코드로 필터하지 않는다
                    st = self.store.upsert_sector(recs)
                    for k in totals:
                        totals[k] += st[k]
            log.info("섹터 레이어 %s 완료 (누적 %s)", hs, totals)
        return totals

    def collect_sector_total(self, codes: list[str], start: str, end: str) -> dict[str, int]:
        """국가 구분 없는 전국 합계. country_code='ALL' 로 저장한다."""
        totals = {"inserted": 0, "updated": 0, "unchanged": 0}
        for hs in codes:
            for s, e, force in self._windows(start, end):
                params = {"strtYymm": s, "endYymm": e, "hsSgn": hs}
                rows = self._fetch("item", params, force)
                recs = [{
                    "period": normalize_period(r.get("period", "")),
                    "hs_code": (r.get("hs_code") or hs).strip(),
                    "hs6": hs6(r.get("hs_code") or hs),
                    "hs_name": r.get("hs_name"),
                    "country_code": "ALL", "country_name": "전체",
                    "exp_usd": r.get("exp_usd"), "exp_wgt": r.get("exp_wgt"),
                    "imp_usd": r.get("imp_usd"), "imp_wgt": r.get("imp_wgt"),
                    "bal_usd": r.get("bal_usd"),
                } for r in rows
                    if normalize_period(r.get("period", "")) and (r.get("hs_code") or hs)]
                st = self.store.upsert_sector(recs)
                for k in totals:
                    totals[k] += st[k]
        return totals

    # ------------------------------------------------------------ 레이어 2

    def collect_region(self, sido_name: str, codes: list[str],
                       start: str, end: str) -> dict[str, int]:
        """시군구별 x HS6. sidoCd 한 번 호출로 그 시도의 모든 시군구가 돌아온다."""
        totals = {"inserted": 0, "updated": 0, "unchanged": 0}
        for hs in codes:
            if len(hs) != 6:
                log.warning("시군구 API 는 HS 6단위만 허용. '%s' 건너뜀", hs)
                continue
            for s, e, force in self._windows(start, end):
                try:
                    sido_cd = sido_code_for(self.store, sido_name, normalize_period(s))
                except KeyError:
                    # 그 시점에 존재하지 않던 시도. 2026-07 개편으로 '전남광주통합특별시'는
                    # 개편 전 구간에, '광주광역시'·'전라남도'는 개편 후 구간에 코드가 없다.
                    # 오류가 아니라 정상적인 공백이므로 그 창만 건너뛴다.
                    log.debug("%s 는 %s 구간에 존재하지 않음 — 건너뜀", sido_name, s)
                    continue
                params = {"strtYymm": s, "endYymm": e, "HsSgn": hs, "sidoCd": sido_cd}
                rows = self._fetch("sigungu_item", params, force)
                recs = []
                for r in rows:
                    period = normalize_period(r.get("period", ""))
                    # sggNm 은 '강원특별자치도 강릉시' 처럼 시도명이 앞에 붙어서 온다
                    sido_full, sgg = split_sigungu(r.get("sigungu_name", ""))
                    if not period or not sgg:
                        continue
                    recs.append({
                        "period": period,
                        "hs_code": (r.get("hs_code") or hs).strip(),
                        "hs_name": r.get("hs_name"),
                        "sido_cd": sido_cd,
                        "sido_name": sido_full or sido_name,
                        "sigungu_name": sgg,
                        "exp_cnt": r.get("exp_cnt"),
                        "imp_cnt": r.get("imp_cnt"),
                        # 천달러 -> 달러로 정규화 (레이어 간 비교 가능하게)
                        "exp_usd": _scale(r.get("exp_usd"), REGION_AMOUNT_UNIT_USD),
                        "imp_usd": _scale(r.get("imp_usd"), REGION_AMOUNT_UNIT_USD),
                        "bal_usd": _scale(r.get("bal_usd"), REGION_AMOUNT_UNIT_USD),
                    })
                st = self.store.upsert_region(recs)
                for k in totals:
                    totals[k] += st[k]
            log.info("기업 레이어 %s/%s 완료 (누적 %s)", sido_name, hs, totals)
        return totals

    # ------------------------------------------------------------ 속보 레이어

    def collect_flash(self, kinds: tuple[str, ...], start: str, end: str) -> dict[str, int]:
        """10일 단위 잠정치. 품목/국가 각각 12개월씩 한 번에 받는다.

        캐시(fetch_log)를 쓰지 않고 **매번 다시 받는다.** 이유가 두 가지다.
          - 같은 달의 행이 순(旬)마다 늘어난다. 01~10 만 있던 달에 01~20 이 붙는다.
          - 잠정치는 다음 발표 때 소급 조정된다. 캐시하면 그 조정을 영영 못 본다.
        구간당 1콜, 2년이면 종류별 2콜이라 전부 다시 받아도 부담이 없다.
        """
        totals = {"inserted": 0, "updated": 0, "unchanged": 0}
        window = min(self.client.max_months_per_call, self.flash_window_months)
        for kind in kinds:
            endpoint = f"flash_{kind}"
            for s, e in chunk_periods(start, end, window):
                if self.client.out_of_budget():
                    log.warning("시간 예산 소진 — %s %s~%s 이후는 건너뜁니다", kind, s, e)
                    return totals
                rows = self._fetch(endpoint, {"strtYymm": s, "endYymm": e}, force=True)
                recs = []
                rejected = []
                for r in rows:
                    # ★ 달력에 없는 달은 여기서 막는다. 한 번 저장되면 빌드가 죽는다
                    #   (실측: priodMon 에 '20' 이 들어와 monthrange(2026, 20) 에서 터졌다).
                    #   버린 행은 원문 그대로 남겨 로그에 찍는다 — 응답이 실제로 어떤
                    #   모양인지 다음 실행 로그만 보면 알 수 있어야 한다.
                    period = parse_period(r.get("year"), r.get("month"))
                    day_to, seq = parse_dt(r.get("dt", ""))
                    if not period or not seq:
                        rejected.append(r)
                        continue
                    for i in range(11):
                        raw = r.get(f"v{i:02d}")
                        usd = _scale(raw, FLASH_UNIT_USD)
                        # 아직 발표되지 않은 순은 빈 값/0 으로 온다. 0 을 넣으면
                        # 전년 대비 계산에서 '-100%'라는 가짜 신호가 만들어진다.
                        if usd is None or int(usd) <= 0:
                            continue
                        recs.append({
                            "period": period, "seq": seq, "kind": kind,
                            "slot": f"{i:02d}", "dt": (r.get("dt") or "").strip(),
                            "day_to": day_to, "exp_usd": usd,
                        })
                if rows:
                    # 기간을 어떻게 읽었는지 매번 한 줄 남긴다. 이 API 는 문서가
                    # 부실해서, 원문과 해석을 나란히 봐야 매핑이 틀린 걸 알아챈다.
                    r0 = rows[0]
                    log.info("속보 %s 기간 해석 — 원문 year=%r month=%r dt=%r → %s seq=%s",
                             kind, r0.get("year"), r0.get("month"), r0.get("dt"),
                             parse_period(r0.get("year"), r0.get("month")),
                             parse_dt(r0.get("dt", ""))[1])
                st = self.store.upsert_flash(recs)
                for k in totals:
                    totals[k] += st[k]
                left = self.client.budget_left()
                log.info("속보 %s %s~%s: %.1fs · %d행 → %s%s", kind, s, e,
                         self.client.last_elapsed, len(recs), st,
                         "" if left is None else f" (남은 예산 {left:.0f}s)")
                if rejected:
                    # 원문 그대로 찍는다. 이 API 는 문서가 부실해서, 버려진 행의
                    # 실제 모양을 보는 것이 스펙을 알아내는 가장 빠른 길이다.
                    log.warning("속보 %s %s~%s: 형식이 맞지 않아 버린 행 %d개",
                                kind, s, e, len(rejected))
                    for r in rejected[:5]:
                        log.warning("  버린 행 원문: %s",
                                    {k2: v for k2, v in r.items() if v not in (None, "")})
        return totals

    def collect_for_companies(self, companies: dict, start: str, end: str) -> dict[str, int]:
        """companies.yaml 의 hs_watch 를 시도 단위로 묶어 최소 호출로 수집한다."""
        by_sido: dict[str, set[str]] = {}
        for name, c in companies.get("companies", {}).items():
            for site in [c] + list(c.get("secondary_sites") or []):
                sido = site.get("sido")
                if not sido:
                    continue
                by_sido.setdefault(sido, set()).update(c.get("hs_watch", []))

        totals = {"inserted": 0, "updated": 0, "unchanged": 0}
        for sido, codes in sorted(by_sido.items()):
            st = self.collect_region(sido, sorted(codes), start, end)
            for k in totals:
                totals[k] += st[k]
        return totals


def estimate_calls(n_hs: int, n_countries: int, start: str, end: str,
                   max_months: int = 12) -> int:
    n_windows = len(list(chunk_periods(start, end, max_months)))
    return n_hs * max(n_countries, 1) * n_windows
