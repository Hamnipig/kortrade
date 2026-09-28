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
log = logging.getLogger("run_demand")


def _get(url: str, params: dict, tries: int = 2) -> object | None:
    """★ 실패해도 URL 을 로그에 찍지 않는다 — 키가 들어 있다."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            # 204 = 조건에 맞는 행 없음(Census). 오류가 아니다.
            if r.status_code in (204, 404):
                return None
            log.warning("HTTP %s (%s)", r.status_code, url.split("?")[0])
        except Exception as exc:                        # noqa: BLE001
            log.warning("요청 실패: %s", type(exc).__name__)
        time.sleep(1.5 * (i + 1))
    return None


# ── ① 미국 수입 (US Census) ───────────────────────────────────────────────

def collect_census(cfg: D.DemandConfig, store: Store, key: str,
                   start: str, end: str) -> dict:
    """HTS 10단위 × (한국 / 전세계) 월별 수입액.

    한국만 받으면 점유율을 못 낸다 — **전세계 합계를 반드시 함께 받는다.**
    그게 이 축의 존재 이유(시장 규모 대비 우리 몫)이기 때문이다.
    """
    base = f"https://api.census.gov/data/{cfg.dataset}"
    seen, rows = {}, []
    for c in cfg.codes:
        for tag, partner in (("KR", cfg.partner), ("ALL", None)):
            params = {
                "get": "I_COMMODITY,I_COMMODITY_SDESC,GEN_VAL_MO,CTY_CODE",
                "I_COMMODITY": c.code, "COMM_LVL": "HS10",
                "time": f"from+{start}+to+{end}", "key": key,
            }
            if partner:
                params["CTY_CODE"] = partner
            data = _get(base, params)
            if not isinstance(data, list) or len(data) < 2:
                log.info("Census %s/%s: 행 없음", c.code, tag)
                continue
            head = {n: i for i, n in enumerate(data[0])}
            agg: dict[str, float] = {}
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
                seen.setdefault(c.code, str(r[head["I_COMMODITY_SDESC"]]))
            for p, v in agg.items():
                rows.append({"source": "census", "series": f"{c.code}:{tag}",
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
    return {"rows": len(rows), "codes": verdicts}


# ── ② 미국 배터리 저장 가동용량 (EIA) ─────────────────────────────────────

def discover_eia(cfg: D.DemandConfig, key: str) -> dict | None:
    """라우트와 배터리 연료코드를 **문서가 아니라 API 에게 물어서** 찾는다.

    EIA 문서에는 연료코드표가 없고 라우트 표기도 흔들린다(문서 안에서도 두 가지로
    나온다). 코드를 추측해 박아 넣으면 조용히 빈 데이터가 쌓이므로,
    facet 엔드포인트를 훑어 설명에 'batter' 가 들어간 코드를 찾는다.
    """
    for route in cfg.routes:
        meta = _get(f"https://api.eia.gov/v2/{route}", {"api_key": key})
        if not isinstance(meta, dict) or "response" not in meta:
            continue
        facets = [f.get("id") for f in (meta["response"].get("facets") or [])]
        cols = list((meta["response"].get("data") or {}).keys())
        freqs = [f.get("id") for f in (meta["response"].get("frequency") or [])]
        for fid in cfg.facet_candidates:
            if fid not in facets:
                continue
            fv = _get(f"https://api.eia.gov/v2/{route}/facet/{fid}", {"api_key": key})
            if not isinstance(fv, dict):
                continue
            hits = [f for f in (fv.get("response", {}).get("facets") or [])
                    if cfg.match in str(f.get("name", "")).lower()
                    or cfg.match in str(f.get("id", "")).lower()]
            if hits:
                return {"route": route, "facet": fid,
                        "ids": [str(h.get("id")) for h in hits],
                        "names": [str(h.get("name")) for h in hits],
                        "columns": cols, "frequencies": freqs}
    return None


def collect_eia(cfg: D.DemandConfig, store: Store, key: str,
                start: str, end: str) -> dict:
    found = discover_eia(cfg, key)
    if not found:
        return {"rows": 0, "found": None,
                "note": "배터리 연료코드를 찾지 못했습니다. 이 축은 비워 둡니다."}
    # 용량 컬럼 이름이 라우트마다 다르다. 있는 것 중 첫 번째를 쓴다.
    col = next((c for c in ("capacity", "nameplate-capacity-mw", "net-summer-capacity-mw",
                            "total-capacity") if c in (found["columns"] or [])), None)
    if not col:
        return {"rows": 0, "found": found,
                "note": "용량 컬럼을 찾지 못했습니다. columns 를 보고 config 를 조정하세요."}
    freq = "monthly" if "monthly" in (found["frequencies"] or []) else None
    params = {"api_key": key, "data[]": col, "start": start, "end": end,
              "length": 5000, "sort[0][column]": "period", "sort[0][direction]": "asc"}
    if freq:
        params["frequency"] = freq
    for i, fid in enumerate(found["ids"]):
        params[f"facets[{found['facet']}][{i}]"] = fid
    data = _get(f"https://api.eia.gov/v2/{found['route']}/data", params)
    recs = (data or {}).get("response", {}).get("data") or []
    agg: dict[str, float] = {}
    for r in recs:
        p = str(r.get("period", ""))[:7]
        if len(p) != 7:
            continue
        st = str(r.get("statusDescription") or r.get("status") or "")
        # 계획(planned)을 섞으면 '설치됐다'가 아니라 '설치할 것이다'가 된다
        if cfg.status_filter and st and not any(s.lower() in st.lower()
                                                for s in ["operat"] ):
            continue
        try:
            agg[p] = agg.get(p, 0.0) + float(r.get(col))   # ★ 문자열로 온다
        except (TypeError, ValueError):
            continue
    rows = [{"source": "eia", "series": "capacity", "period": p, "value": v,
             "unit": "MW"} for p, v in agg.items()]
    if rows:
        store.upsert_demand(rows)
    return {"rows": len(rows), "found": found, "column": col}


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
                  + (f" · 라우트 {f['route']} · {f['facet']}={f['ids']}" if f else
                     f" · {out['eia'].get('note','')}"))
        else:
            out["eia"] = {"skipped": "키 없음" if not eia_key else "설정에서 꺼짐"}

    VERIFY.parent.mkdir(parents=True, exist_ok=True)
    VERIFY.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"검증 결과 → {VERIFY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
