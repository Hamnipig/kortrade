#!/usr/bin/env python3
"""최종 수요 축 수집 — 미국 수입(US Census) · 미국 배터리 저장 가동용량(EIA).

왜 이게 필요한가
    對미 ESS 셀 수출이 −13% 일 때, 한국 수출 데이터만으로는
      (a) 수요 위축  (b) 현지 생산 전환  (c) 점유율 상실
    을 가를 수 없다. 특히 **(c)는 원리적으로 볼 수 없다** — 한국 수출만 보고 있으면
    미국 시장이 두 배가 됐는지 반토막 났는지 알 방법이 자체에 없기 때문이다.
    (b)와 (c)는 투자 판단이 정반대라서 이 구분이 중요하다.

두 축 모두 **무료지만 각자 키가 필요**하다.
    CENSUS_API_KEY   https://api.census.gov/data/key_signup.html
    EIA_API_KEY      https://www.eia.gov/opendata/register.php

★ 키가 없으면 **조용히 건너뛰고 0 으로 끝난다.** 이 축은 곁다리이고,
  여기서 죽으면 관세청 파이프라인 전체가 멈춘다. 없는 축은 화면에서 비워 둔다.

★ 코드 검증: 미국 HTS 통계품목(8507600030 등)은 USITC 원문을 직접 확인하지 못했다
  (JS 렌더). 그래서 config 는 draft 로 잠겨 있고, 이 스크립트가 **실제 응답의
  품목 설명**을 대조해 data/demand_verify.json 에 기록한다. 설정 파일을 손으로
  고쳐 여는 것이 아니라 기계가 확인한 것만 쓴다 — 속보 레이어와 같은 방식이다.

사용법:
    export CENSUS_API_KEY=... EIA_API_KEY=...
    python scripts/run_demand.py --start 2022-01
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade import demand as D
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "data" / "demand_verify.json"
TIMEOUT = 30
PAGE = 5000        # EIA 요청당 행 상한 (문서 명시)
# 이 라우트는 **발전기 x 월** 단위다. 미국 배터리 설비는 2026년 기준 수천 기이고
# 2021-01~ 이면 월수까지 곱해져 10만 행을 넘는다. 12페이지(6만)로는 최근월이 잘린다.
MAX_PAGES = 45     # 225,000행까지
# 원산지별로 남길 상위 국가 수. 전수(200여 개)를 담으면 DB 만 커지고
# 화면에서 읽히지도 않는다. 상위 12개면 미국 BESS 수입의 대부분을 덮는다.
TOP_ORIGINS = 12
log = logging.getLogger("run_demand")


LAST: dict = {}     # 마지막 호출의 진단 정보 (상태코드·오류본문). 키는 절대 안 담는다.


def _get(url: str, params: dict, tries: int = 2) -> object | None:
    """★ 실패해도 URL 을 로그에 찍지 않는다 — 키가 들어 있다.

    대신 **상태코드와 오류 본문 앞부분**을 LAST 에 남긴다. 이게 없으면
    '행 없음'과 '파라미터 오류'를 구분할 수 없어서 추측만 반복하게 된다
    (2026-09-28: time 파라미터 인코딩 문제를 이것 없이 찾느라 한 바퀴 돌았다).
    """
    global LAST
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            LAST = {"status": r.status_code, "body": r.text[:300].strip()}
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    LAST["note"] = "JSON 이 아님"
                    return None
            if r.status_code in (204, 404):
                return None
            log.warning("HTTP %s (%s) %s", r.status_code, url.split("?")[0],
                        r.text[:160].replace("\n", " "))
        except Exception as exc:                        # noqa: BLE001
            LAST = {"status": None, "body": f"{type(exc).__name__}"}
            log.warning("요청 실패: %s", type(exc).__name__)
        time.sleep(1.5 * (i + 1))
    return None


# ── ① 미국 수입 (US Census) ───────────────────────────────────────────────

def collect_census(cfg: D.DemandConfig, store: Store, key: str,
                   start: str, end: str) -> dict:
    """HTS 10단위 × (한국 / 전세계 / **원산지별**) 월별 수입액.

    한국만 받으면 점유율을 못 낸다 — 전세계 합계를 반드시 함께 받는다.

    ★ 원산지별을 따로 저장하는 이유 (2026-09-29)
      "미국 수입에서 한국 비중이 빠졌다"는 사실 하나로는 원인을 못 가른다:
        (a) 중국 등 경쟁사에 밀렸다
        (b) 한국 기업이 미국 현지 생산으로 옮겼다   ← 수입 통계에서 아예 사라진다
        (c) 한국 기업이 폴란드·헝가리 공장에서 미국으로 보낸다 ← 그 나라 수입이 는다
      (c)는 **원산지 분해만으로 바로 보인다.** 그리고 이건 공짜다 —
      전세계 조회 응답에 이미 국가별 행이 다 들어 있고, 지금은 합산해서 버리고 있었다.
      호출은 한 건도 늘지 않는다.
    """
    base = f"https://api.census.gov/data/{cfg.dataset}"
    seen, rows, diag, names = {}, [], {}, {}
    for c in cfg.codes:
        for tag, partner in (("KR", cfg.partner), ("ALL", None)):
            params = {
                "get": "I_COMMODITY,I_COMMODITY_SDESC,GEN_VAL_MO,CTY_CODE,CTY_NAME",
                "I_COMMODITY": c.code, "COMM_LVL": "HS10",
                # ★ 공백이어야 한다. "from+X+to+Y" 를 그대로 넣으면 requests 가
                #   '+' 를 %2B(리터럴 플러스)로 인코딩해 Census 가 못 읽는다.
                #   공백을 넣어야 '+' 로 인코딩돼 문서의 예시와 같아진다.
                "time": f"from {start} to {end}", "key": key,
            }
            if partner:
                params["CTY_CODE"] = partner
            data = _get(base, params)
            if not isinstance(data, list) or len(data) < 2:
                # 범위 문법 문제인지 코드가 없는 건지 가른다 — 단월로 한 번 더
                probe = dict(params); probe["time"] = end
                p2 = _get(base, probe)
                diag[f"{c.code}:{tag}"] = {
                    "range": "행 없음", "single": ("행 있음" if isinstance(p2, list)
                                                  and len(p2) > 1 else "행 없음"),
                    "status": LAST.get("status"), "body": LAST.get("body", "")[:200],
                }
                if isinstance(p2, list) and len(p2) > 1:
                    data = p2          # 단월이라도 건진다
                else:
                    log.info("Census %s/%s: 행 없음 (HTTP %s) %s", c.code, tag,
                             LAST.get("status"), LAST.get("body", "")[:120])
                    continue
            head = {n: i for i, n in enumerate(data[0])}
            agg: dict[str, float] = {}
            by_cty: dict[str, dict[str, float]] = {}
            for r in data[1:]:
                # 전세계는 국가별 행이 전부 오므로 기간별로 합산한다.
                # CTY_CODE '-' 나 집계행이 섞이면 이중계상이 되므로 4자리 숫자만 센다.
                cc = str(r[head["CTY_CODE"]])
                if partner is None and not (cc.isdigit() and len(cc) == 4):
                    continue
                p = str(r[head["time"]]) if "time" in head else None
                if not p or len(p) != 7:
                    continue
                try:
                    v = float(r[head["GEN_VAL_MO"]])
                except (TypeError, ValueError):
                    continue
                agg[p] = agg.get(p, 0.0) + v
                if partner is None:                     # 전세계 조회에서만 원산지 분해
                    by_cty.setdefault(cc, {})[p] = by_cty.get(cc, {}).get(p, 0.0) + v
                    if "CTY_NAME" in head:
                        names[cc] = str(r[head["CTY_NAME"]])
                seen.setdefault(c.code, str(r[head["I_COMMODITY_SDESC"]]))
            for p, v in agg.items():
                rows.append({"source": "census", "series": f"{c.code}:{tag}",
                             "period": p, "value": v, "unit": "USD"})

            # ── 원산지별 — 금액 상위 국가만 남긴다 (전수를 담으면 DB 가 커진다) ──
            if partner is None and by_cty:
                top = sorted(by_cty, key=lambda k: -sum(by_cty[k].values()))[:TOP_ORIGINS]
                for cc in top:
                    for p, v in by_cty[cc].items():
                        rows.append({"source": "census",
                                     "series": f"{c.code}:C:{cc}",
                                     "period": p, "value": v, "unit": "USD"})
    if rows:
        store.upsert_demand(rows)
    # 설명 대조 — 기계가 확인한 것만 active 로 승격한다
    verdicts = {}
    for c in cfg.codes:
        desc = seen.get(c.code, "")
        verdicts[c.code] = {
            "label": c.label, "desc": desc,
            "seen": bool(desc),
            "matched": bool(desc) and c.expect.lower() in desc.lower(),
            "expect": c.expect,
        }
    return {"rows": len(rows), "codes": verdicts, "diag": diag,
            "ctyNames": names}


# ── ② 미국 배터리 저장 가동용량 (EIA) ─────────────────────────────────────

def _label(f: dict) -> str:
    """facet 값의 사람이 읽는 이름. 필드 이름이 라우트마다 다르다."""
    for k in ("name", "description", "alias", "value"):
        v = f.get(k)
        if v:
            return str(v)
    return ""


def discover_eia(cfg: D.DemandConfig, key: str) -> tuple[dict | None, dict]:
    """라우트와 배터리 축을 **문서가 아니라 API 에게 물어서** 찾는다.

    EIA 문서에는 코드표가 없고 라우트 표기도 흔들린다(문서 안에서도 두 가지로 나온다).
    추측해 박아 넣으면 조용히 빈 데이터가 쌓이므로 facet 엔드포인트를 훑는다.

    ★ 'batter' 하나로 찾으면 못 찾는다 (2026-09-28 실측 실패).
      EIA-860M 에서 배터리를 가리키는 축이 둘인데 표기가 전혀 다르다:
        technology         = "Batteries"                          ← 'batter' 로 잡힌다
        energy_source_code = "MWH" / "Electricity used for energy storage"
                                                                  ← 'batter' 로 **안 잡힌다**
      그래서 match_any 에 'storage' 를, match_codes 에 'MWH' 를 넣고 둘 다 본다.

    ★ 두 번째 반환값은 **진단**이다. 못 찾았을 때 어떤 라우트에 어떤 facet 이 있고
      값이 어떻게 생겼는지를 남긴다. 이게 없으면 다음 실행도 똑같이 깜깜하다.
    """
    diag: dict = {}
    for route in cfg.routes:
        meta = _get(f"https://api.eia.gov/v2/{route}", {"api_key": key})
        if not isinstance(meta, dict) or "response" not in meta:
            diag[route] = {"meta": f"응답 없음 (HTTP {LAST.get('status')})",
                           "body": LAST.get("body", "")[:160]}
            continue
        resp = meta["response"]
        facets = [str(f.get("id")) for f in (resp.get("facets") or [])]
        cols = list((resp.get("data") or {}).keys())
        freqs = [str(f.get("id")) for f in (resp.get("frequency") or [])]
        diag[route] = {"facets": facets, "columns": cols, "frequencies": freqs,
                       "samples": {}}
        for fid in cfg.facet_candidates:
            if fid not in facets:
                continue
            # ★ EIA 문서 예시는 끝에 **슬래시**가 붙어 있다
            #   (.../facet/sectorid/?api_key=...). 없으면 404 로 떨어져서
            #   "배터리 축을 못 찾았다"로 보인다 — 실제로는 부르지도 못한 것이다.
            fv = _get(f"https://api.eia.gov/v2/{route}/facet/{fid}/", {"api_key": key})
            vals = (fv or {}).get("response", {}).get("facets") or []
            if not vals:        # 혹시 슬래시 없는 쪽을 받는 라우트가 있으면 대비
                fv = _get(f"https://api.eia.gov/v2/{route}/facet/{fid}", {"api_key": key})
                vals = (fv or {}).get("response", {}).get("facets") or []
            # 못 찾았을 때 보라고 값 표본을 남긴다 (너무 길면 화면이 안 읽힌다)
            diag[route]["samples"][fid] = [
                f"{f.get('id')}={_label(f)}" for f in vals[:25]]
            hits = [f for f in vals
                    if any(m in _label(f).lower() or m in str(f.get("id", "")).lower()
                           for m in cfg.match_any)
                    or str(f.get("id", "")).upper() in cfg.match_codes]
            if hits:
                return ({"route": route, "facet": fid, "all_facets": facets,
                         "ids": [str(h.get("id")) for h in hits],
                         "names": [_label(h) for h in hits],
                         "columns": cols, "frequencies": freqs}, diag)
    return None, diag


def collect_eia(cfg: D.DemandConfig, store: Store, key: str,
                start: str, end: str) -> dict:
    found, diag = discover_eia(cfg, key)
    if not found:
        return {"rows": 0, "found": None, "diag": diag,
                "note": "배터리 축(facet)을 찾지 못했습니다. demand_verify.json 의 "
                        "eia.diag 에 라우트별 facet 목록과 값 표본이 있습니다."}
    # 용량 컬럼 이름이 라우트마다 다르다. 있는 것 중 첫 번째를 쓴다.
    col = next((c for c in ("capacity", "nameplate-capacity-mw", "net-summer-capacity-mw",
                            "total-capacity") if c in (found["columns"] or [])), None)
    if not col:
        return {"rows": 0, "found": found, "diag": diag,
                "note": f"용량 컬럼을 찾지 못했습니다. 이 라우트의 컬럼: {found['columns']}"}
    freq = "monthly" if "monthly" in (found["frequencies"] or []) else None
    base = {"api_key": key, "data[]": col, "start": start, "end": end,
            "length": PAGE, "sort[0][column]": "period", "sort[0][direction]": "asc"}
    if freq:
        base["frequency"] = freq
    for i, fid in enumerate(found["ids"]):
        base[f"facets[{found['facet']}][{i}]"] = fid
    # ★ status 를 서버에서 거르면 계획·폐지 설비가 아예 안 와서 행수가 크게 준다
    #   (이 표는 '운전 중'이 아니라 **가동 가능 발전기 인벤토리**라 계획분도 들어 있다).
    #   status facet 이 있을 때만 건다. 결과가 0행이면 아래에서 필터 없이 다시 받는다.
    if cfg.status_filter and "status" in (found.get("all_facets") or []):
        for i, sc in enumerate(cfg.status_filter):
            base[f"facets[status][{i}]"] = sc

    # ★ 페이지네이션이 반드시 필요하다. 이 라우트는 **발전기 단위**로 돌아오므로
    #   미국 배터리 설비만 해도 월 수천 행이고, EIA 는 요청당 5,000행이 상한이다.
    #   한 번만 부르면 앞쪽 몇 달치만 받고 뒤를 통째로 날린다 — 그러면 최근월이
    #   비어서 YoY 가 안 나오고, 화면에는 그냥 '데이터 없음'으로 보인다.
    agg: dict[str, float] = {}
    got = total = 0
    for page in range(MAX_PAGES):
        params = dict(base); params["offset"] = page * PAGE
        data = _get(f"https://api.eia.gov/v2/{found['route']}/data", params)
        resp = (data or {}).get("response") or {}
        recs = resp.get("data") or []
        try:
            total = int(resp.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if not recs:
            break
        for r in recs:
            pp = str(r.get("period", ""))[:7]
            if len(pp) != 7:
                continue
            st = str(r.get("statusDescription") or r.get("status") or "")
            # 계획(planned)을 섞으면 '설치됐다'가 아니라 '설치할 것이다'가 된다.
            # ★ status 는 코드('OP')로 올 수도 설명('Operating')으로 올 수도 있다.
            #   설명만 보고 거르면 코드로 오는 응답에서 **전부 걸러져 0행**이 된다.
            if cfg.status_filter and st:
                ok = (st.upper() in [x.upper() for x in cfg.status_filter]
                      or "operat" in st.lower())
                if not ok:
                    continue
            try:
                agg[pp] = agg.get(pp, 0.0) + float(r.get(col))   # ★ 문자열로 온다
            except (TypeError, ValueError):
                continue
        got += len(recs)
        if got >= total or len(recs) < PAGE:
            break
    if total and got < total:
        log.warning("EIA 응답 %d행 중 %d행만 받았습니다 (MAX_PAGES 상한). "
                    "최근월이 잘렸을 수 있습니다.", total, got)

    # status 코드를 잘못 짚었을 수 있다 — 0행이면 필터를 빼고 한 번만 더 본다.
    # (틀린 코드로 전부 걸러진 것과 정말 데이터가 없는 것은 다른 이야기다)
    retried = False
    if not agg and any(k.startswith("facets[status]") for k in base):
        retried = True
        nf = {k: v for k, v in base.items() if not k.startswith("facets[status]")}
        data = _get(f"https://api.eia.gov/v2/{found['route']}/data", nf)
        for r in ((data or {}).get("response") or {}).get("data") or []:
            pp = str(r.get("period", ""))[:7]
            if len(pp) == 7:
                try:
                    agg[pp] = agg.get(pp, 0.0) + float(r.get(col))
                except (TypeError, ValueError):
                    pass
        if agg:
            log.warning("status=%s 로는 0행이라 필터 없이 받았습니다 — "
                        "계획 설비가 섞였을 수 있습니다.", cfg.status_filter)
    rows = [{"source": "eia", "series": "capacity", "period": pp, "value": v,
             "unit": "MW"} for pp, v in agg.items()]
    if rows:
        store.upsert_demand(rows)
    return {"rows": len(rows), "found": found, "column": col, "diag": diag,
            "fetched": got, "total": total, "statusRetry": retried,
            "months": f"{min(agg)}~{max(agg)}" if agg else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--start", default="2021-01", help="YYYY-MM")
    ap.add_argument("--end", default=None, help="YYYY-MM (기본: 2개월 전)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    cfg = D.load()
    errs = cfg.validate()
    if errs:
        print("최종 수요 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1

    end = args.end
    if not end:
        from datetime import date
        t = date.today()
        m = t.month - 2 + 12 * t.year
        end = f"{m // 12:04d}-{m % 12 + 1:02d}"

    census_key = os.environ.get("CENSUS_API_KEY", "").strip()
    eia_key = os.environ.get("EIA_API_KEY", "").strip()
    out = {"start": args.start, "end": end}

    if not census_key and not eia_key:
        # ★ 실패가 아니다. 이 축은 곁다리이고 없으면 화면에서 비워 둔다.
        print("CENSUS_API_KEY / EIA_API_KEY 가 없어 최종 수요 축을 건너뜁니다.")
        print("  둘 다 무료입니다: api.census.gov/data/key_signup.html · "
              "eia.gov/opendata/register.php")
        VERIFY.parent.mkdir(parents=True, exist_ok=True)
        VERIFY.write_text(json.dumps({"skipped": "키 없음"}, ensure_ascii=False,
                                     indent=1), encoding="utf-8")
        return 0

    with Store(args.db) as store:
        if census_key and cfg.imports_enabled:
            out["census"] = collect_census(cfg, store, census_key, args.start, end)
            print(f"미국 수입 — {out['census']['rows']}행")
            for code, v in out["census"]["codes"].items():
                mark = "확인" if v["matched"] else ("응답은 있으나 설명 불일치"
                                                    if v["seen"] else "행 없음")
                print(f"  {code} {v['label']:<20} {mark}  \"{v['desc'][:60]}\"")
        else:
            out["census"] = {"skipped": "키 없음" if not census_key else "설정에서 꺼짐"}

        if eia_key and cfg.cap_enabled:
            out["eia"] = collect_eia(cfg, store, eia_key, args.start, end)
            f = out["eia"].get("found")
            print(f"미국 설치용량 — {out['eia']['rows']}행"
                  + (f" · 라우트 {f['route']} · {f['facet']}={f['ids']}"
                     f" · 컬럼 {out['eia'].get('column')}"
                     f" · {out['eia'].get('fetched')}/{out['eia'].get('total')}행"
                     f" · {out['eia'].get('months')}" if f else
                     f" · {out['eia'].get('note','')}"))
            # ★ 실패했으면 진단을 **로그에 그대로 찍는다.** 파일을 열지 않아도
            #   Actions 로그만 보고 원인을 알 수 있어야 한다 — 왕복이 줄어든다.
            if not out["eia"].get("rows"):
                for route, d in (out["eia"].get("diag") or {}).items():
                    print(f"  [진단] {route}")
                    if d.get("meta"):
                        print(f"     {d['meta']} {d.get('body','')[:120]}")
                        continue
                    print(f"     facets  : {d.get('facets')}")
                    print(f"     columns : {d.get('columns')}")
                    print(f"     freq    : {d.get('frequencies')}")
                    for fid, vals in (d.get("samples") or {}).items():
                        print(f"     {fid} 값 표본: {vals[:15]}")
        else:
            out["eia"] = {"skipped": "키 없음" if not eia_key else "설정에서 꺼짐"}

    VERIFY.parent.mkdir(parents=True, exist_ok=True)
    VERIFY.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"검증 결과 → {VERIFY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
