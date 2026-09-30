#!/usr/bin/env python3
"""PCB 해외 대조축 수집 — 일본 METI 생산동태통계 (e-Stat API).

왜 이 축이 필요한가
    한국 HS 8534 에는 층수 구분이 없다. HSK 세 개는 '형성된 회로의 종류'로
    갈리고 층수 세분은 중국 체계다. 그래서 고다층·패키지기판으로의 **믹스 전환을
    한국 데이터로는 직접 볼 수 없고** ASP($/kg)로 간접 추정할 뿐이다.
    그런데 ASP 에는 구리 가격·환율·두께가 같이 섞인다.

    일본 METI 는 品目別(片面/両面/多層/ビルドアップ多層/フレキシブル)로 쪼개
    준다. 한국 ASP 상승이 진짜 믹스 상승인지 가르는 **유일한 외부 대조군**이다.

★ 키가 없으면 **조용히 건너뛰고 0 으로 끝난다.** 곁다리 축이고, 여기서 죽으면
  관세청 파이프라인 전체가 멈춘다. 없는 축은 화면에서 비워 둔다.

★ statsDataId 를 상수로 박지 않는다. 최종 수요 축에서 EIA 라우트를 박았다가
  '배터리 축 못 찾음'만 반복한 적이 있다. 통계표는 매번 getStatsList 로 찾고,
  品目은 코드가 아니라 **이름**으로 맞추고, 고른 것과 후보 전체를
  data/pcb_intl_verify.json 에 남긴다. 화면이 비면 그 파일이 이유를 말한다.

★ URL 을 절대 로그에 찍지 않는다 — appId 가 들어 있다.

사용법:
    export EJ_ESTAT_APP_ID=...
    python scripts/run_pcb_intl.py --start 2019-01
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

from kortrade import intl as I
from kortrade.store import Store

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "data" / "pcb_intl_verify.json"
TIMEOUT = 40
LIMIT = 100_000        # 1회 반환 상한 (사양서 명시). 초과분은 NEXT_KEY 로 이어받는다
MAX_PAGES = 12
log = logging.getLogger("run_pcb_intl")

LAST: dict = {}        # 마지막 호출의 상태코드·본문 앞부분. 키는 절대 안 담는다


def _get(url: str, params: dict, tries: int = 3) -> dict | None:
    """★ 실패해도 URL 을 찍지 않는다 — appId 가 들어 있다.

    대신 상태코드와 본문 앞부분을 LAST 에 남긴다. 이게 없으면 '행 없음'과
    '파라미터 오류'를 구분할 수 없어 추측만 반복한다 (Census time 인코딩
    문제를 이것 없이 찾느라 한 바퀴 돌았다).
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
            # ★ url 이 아니라 **경로만** 찍는다
            log.warning("HTTP %s (%s)", r.status_code, url.split("?")[0])
        except Exception as exc:                        # noqa: BLE001
            LAST = {"status": None, "body": type(exc).__name__}
            log.warning("요청 실패: %s", type(exc).__name__)
        time.sleep(1.5 * (i + 1))
    return None


# ══════════════════════════════════════════════════════════════════════════
# ① 통계표 찾기 — ID 를 박지 않는 이유가 여기에 다 있다
# ══════════════════════════════════════════════════════════════════════════

def discover(cfg: I.IntlConfig, app_id: str) -> tuple[dict | None, dict]:
    """getStatsList 로 월차 電子回路基板 통계표를 찾는다.

    반환 (고른 표, 진단). 진단에는 **후보 전체**를 담는다 — 잘못 고른 경우와
    아예 못 찾은 경우는 대응이 다르다.
    """
    diag: dict = {"tried": [], "candidates": [], "picked": None}
    for word in cfg.search_words:
        payload = _get(f"{cfg.base}/getStatsList",
                       {"appId": app_id, "statsCode": cfg.stats_code,
                        "searchWord": word, "limit": 200})
        st, msg = I.status_of(payload or {}, "GET_STATS_LIST")
        entry = {"searchWord": word, "status": st, "msg": msg[:160],
                 "http": LAST.get("status"), "n": 0}
        if payload is None:
            entry["msg"] = entry["msg"] or LAST.get("body", "")[:160]
            diag["tried"].append(entry)
            continue
        tables = I.as_list((((payload.get("GET_STATS_LIST") or {})
                             .get("DATALIST_INF") or {}).get("TABLE_INF")))
        entry["n"] = len(tables)
        diag["tried"].append(entry)
        if not tables:
            continue

        scored = []
        for t in tables:
            title = t.get("TITLE")
            title = title.get("$") if isinstance(title, dict) else title
            sname = t.get("STATISTICS_NAME")
            sname = sname.get("$") if isinstance(sname, dict) else sname
            cycle = str(t.get("CYCLE") or "")
            text = f"{sname or ''} {title or ''}"
            score = 0
            # 월차가 아니면 트래킹에 못 쓴다. 연차표는 시차가 1년이다.
            if any(c in cycle for c in cfg.cycle_prefer):
                score += 10
            else:
                score -= 20
            score += sum(3 for b in cfg.title_bonus if b in text)
            # 찾는 品目 이름이 제목에 직접 있으면 강한 신호
            score += sum(4 for it in cfg.items for w in it.match if w and w in text)
            try:
                rows = int(t.get("OVERALL_TOTAL_NUMBER") or 0)
            except (TypeError, ValueError):
                rows = 0
            scored.append({"id": t.get("@id"), "title": title, "stat": sname,
                           "cycle": cycle, "rows": rows,
                           "surveyDate": t.get("SURVEY_DATE"),
                           "updated": t.get("UPDATED_DATE"), "score": score})
        scored.sort(key=lambda r: (-r["score"], -r["rows"]))
        diag["candidates"] = scored[:20]
        top = scored[0] if scored else None
        if top and top["score"] > 0:
            diag["picked"] = top
            return top, diag
    return None, diag


# ══════════════════════════════════════════════════════════════════════════
# ② 메타 — 品目과 表章項目을 **이름으로** 맞춘다
# ══════════════════════════════════════════════════════════════════════════

def read_meta(cfg: I.IntlConfig, app_id: str, stats_id: str) -> tuple[dict, dict]:
    """getMetaInfo → {'items': {classId: {code: itemKey}}, 'meas': {...}, 'time': {...}}"""
    payload = _get(f"{cfg.base}/getMetaInfo", {"appId": app_id, "statsDataId": stats_id})
    st, msg = I.status_of(payload or {}, "GET_META_INFO")
    diag: dict = {"status": st, "msg": msg[:200], "http": LAST.get("status"),
                  "classes": [], "itemHits": [], "measHits": []}
    out: dict = {"items": {}, "meas": {}, "time": {}, "unit": {}}
    if payload is None or st != 0:
        diag["msg"] = diag["msg"] or LAST.get("body", "")[:200]
        return out, diag

    objs = I.as_list((((payload.get("GET_META_INFO") or {}).get("METADATA_INF") or {})
                      .get("CLASS_INF") or {}).get("CLASS_OBJ"))
    for o in objs:
        cid = str(o.get("@id") or "")
        cname = str(o.get("@name") or "")
        classes = I.as_list(o.get("CLASS"))
        diag["classes"].append({"id": cid, "name": cname, "n": len(classes),
                                "sample": [str(c.get("@name")) for c in classes[:8]]})
        if cid == "time":
            for c in classes:
                p = I.parse_period(c.get("@name"), c.get("@code"))
                if p:
                    out["time"][str(c.get("@code"))] = p
            continue
        # 表章項目(tab) 후보 — 金額/数量
        mhit = {}
        for c in classes:
            nm = str(c.get("@name") or "")
            m = next((m for m in cfg.measures if I.matches(nm, m.match)), None)
            if m:
                mhit[str(c.get("@code"))] = m.key
                out["unit"][str(c.get("@code"))] = str(c.get("@unit") or "")
        # 品目 후보
        ihit = {}
        for c in classes:
            nm = str(c.get("@name") or "")
            it = I.pick_item(nm, cfg.items)
            if it:
                ihit[str(c.get("@code"))] = it.key
        if mhit and cid != "time":
            out["meas"][cid] = mhit
            diag["measHits"].append({"class": cid, "name": cname, "n": len(mhit),
                                     "map": {k: v for k, v in list(mhit.items())[:12]}})
        if ihit:
            out["items"][cid] = ihit
            diag["itemHits"].append({
                "class": cid, "name": cname, "n": len(ihit),
                "map": [{"code": k, "item": v,
                         "name": next((str(c.get("@name")) for c in classes
                                       if str(c.get("@code")) == k), "")}
                        for k, v in list(ihit.items())[:20]]})
    return out, diag


# ══════════════════════════════════════════════════════════════════════════
# ③ 데이터 — 코드 필터로 받고, 0행이면 **필터 없이 다시** 받는다
#
# 서버측 필터가 통하지 않는 경우가 실제로 있었다(EIA status 필터). 그때
# 0행과 '필터가 틀렸다'가 구분되지 않아 한 바퀴를 돌았다. 여기서는 처음부터
# 무필터 재시도를 넣어 둔다 — 느릴 뿐 틀리지 않는다.
# ══════════════════════════════════════════════════════════════════════════

def fetch_values(cfg: I.IntlConfig, app_id: str, stats_id: str,
                 meta: dict, use_filter: bool) -> tuple[list[dict], dict]:
    params = {"appId": app_id, "statsDataId": stats_id, "limit": LIMIT,
              "metaGetFlg": "N", "cntGetFlg": "N"}
    if use_filter:
        # e-Stat 의 코드 필터는 클래스마다 이름이 다르다: cat01 → cdCat01, tab → cdTab.
        # 콤마 구분 최대 100개(사양서 명시)라 넘치면 자른다 — 잘린 만큼은
        # to_rows() 가 클라이언트에서 다시 거르므로 결과는 같고 응답만 커진다.
        for cid, mapping in list(meta["items"].items()) + list(meta["meas"].items()):
            codes = list(mapping)[:100]
            if codes:
                params[_cd_param(cid)] = ",".join(codes)

    rows, diag, pos = [], {"pages": 0, "total": None, "status": None, "msg": ""}, None
    for _ in range(MAX_PAGES):
        p = dict(params)
        if pos:
            p["startPosition"] = pos
        payload = _get(f"{cfg.base}/getStatsData", p)
        st, msg = I.status_of(payload or {}, "GET_STATS_DATA")
        diag["status"], diag["msg"] = st, msg[:200]
        diag["http"] = LAST.get("status")
        if payload is None or st != 0:
            diag["msg"] = diag["msg"] or LAST.get("body", "")[:200]
            break
        sd = ((payload.get("GET_STATS_DATA") or {}).get("STATISTICAL_DATA") or {})
        vals = I.as_list((sd.get("DATA_INF") or {}).get("VALUE"))
        rows += vals
        diag["pages"] += 1
        ri = sd.get("RESULT_INF") or {}
        diag["total"] = ri.get("TOTAL_NUMBER")
        nxt = ri.get("NEXT_KEY")
        if not nxt or not vals:
            break
        pos = nxt
    diag["rows"] = len(rows)
    return rows, diag


def _cd_param(class_id: str) -> str:
    """'cat01' → 'cdCat01', 'tab' → 'cdTab', 'area' → 'cdArea'."""
    cid = class_id.strip()
    return "cd" + cid[:1].upper() + cid[1:]


def to_rows(values: list[dict], meta: dict, cfg: I.IntlConfig) -> tuple[list[dict], dict]:
    """응답 VALUE[] → demand_series 행. 서버 필터가 안 먹었어도 여기서 걸러진다."""
    out, seen_items, skipped = [], {}, {"noPeriod": 0, "noItem": 0, "noMeas": 0, "nan": 0}
    for v in values:
        period = None
        for k, val in v.items():
            if k.lstrip("@") == "time":
                period = meta["time"].get(str(val)) or I.parse_period(None, str(val))
                break
        if not period:
            skipped["noPeriod"] += 1
            continue
        item_key = meas_key = unit = None
        for cid, mapping in meta["items"].items():
            code = v.get("@" + cid)
            if code is not None and str(code) in mapping:
                item_key = mapping[str(code)]
                break
        for cid, mapping in meta["meas"].items():
            code = v.get("@" + cid)
            if code is not None and str(code) in mapping:
                meas_key = mapping[str(code)]
                unit = meta["unit"].get(str(code)) or v.get("@unit")
                break
        if not item_key:
            skipped["noItem"] += 1
            continue
        if not meas_key:
            skipped["noMeas"] += 1
            continue
        raw = v.get("$")
        try:
            num = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            skipped["nan"] += 1        # '-' '***' 등 비수치 기호. 0 으로 넣으면 안 된다
            continue
        if period < cfg.start:
            continue
        key = I.series_key(item_key, meas_key)
        seen_items[key] = seen_items.get(key, 0) + 1
        out.append({"source": I.SOURCE, "series": key, "period": period,
                    "value": num, "unit": str(unit or v.get("@unit") or "")})
    return out, {"kept": len(out), "skipped": skipped, "bySeries": seen_items}


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/kortrade.sqlite")
    ap.add_argument("--start", default=None, help="YYYY-MM (기본: 설정값)")
    ap.add_argument("--dry-run", action="store_true", help="DB 에 쓰지 않는다")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = I.load()
    errs = cfg.validate()
    if errs:
        print("해외 대조축 설정 오류:")
        for e in errs:
            print("  -", e)
        return 1
    if args.start:
        cfg.start = args.start

    app_id = (os.environ.get(cfg.env_key) or "").strip()
    verify: dict = {"version": cfg.version, "start": cfg.start, "credit": cfg.credit}
    if not app_id:
        # ★ 여기서 죽으면 안 된다. 곁다리 축이다.
        print(f"{cfg.env_key} 가 없습니다 — 일본 축을 건너뜁니다. "
              f"(무료: https://www.e-stat.go.jp/mypage/user/preregister 등록 후 "
              f"마이페이지 「API機能(アプリケーションID発行)」)")
        verify["estat"] = {"ok": False, "note": f"{cfg.env_key} 미설정"}
        VERIFY.parent.mkdir(parents=True, exist_ok=True)
        VERIFY.write_text(json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    table, ddiag = discover(cfg, app_id)
    verify["discover"] = ddiag
    if not table:
        print("통계표를 찾지 못했습니다. data/pcb_intl_verify.json 의 discover 를 보세요.")
        verify["estat"] = {"ok": False, "note": "통계표 후보 없음"}
        VERIFY.write_text(json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    print(f"통계표: {table['id']}  {table['stat']} / {table['title']}  "
          f"[{table['cycle']}] {table['rows']:,}행  점수 {table['score']}")

    meta, mdiag = read_meta(cfg, app_id, str(table["id"]))
    verify["meta"] = mdiag
    n_items = sum(len(m) for m in meta["items"].values())
    n_meas = sum(len(m) for m in meta["meas"].values())
    print(f"메타: 品目 매칭 {n_items}개 · 表章項目 매칭 {n_meas}개 · 시간 {len(meta['time'])}개")
    if not n_items or not n_meas:
        print("  매칭이 0 입니다 — verify 파일의 meta.classes 에 실제 항목 이름이 있습니다. "
              "config/pcb_intl.yaml 의 match 문자열을 거기에 맞추세요.")
        verify["estat"] = {"ok": False, "note": "品目 또는 表章項目 매칭 0"}
        VERIFY.write_text(json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    values, fdiag = fetch_values(cfg, app_id, str(table["id"]), meta, use_filter=True)
    rows, rdiag = to_rows(values, meta, cfg)
    verify["fetch"] = {"filtered": fdiag, "parsed": rdiag}
    if not rows:
        # 서버측 코드 필터가 안 먹는 경우가 실제로 있다. 무필터로 한 번 더 받는다.
        print("  필터 응답이 비어 무필터로 재시도합니다.")
        values, fdiag2 = fetch_values(cfg, app_id, str(table["id"]), meta, use_filter=False)
        rows, rdiag2 = to_rows(values, meta, cfg)
        verify["fetch"]["unfiltered"] = fdiag2
        verify["fetch"]["parsedUnfiltered"] = rdiag2

    if not rows:
        print("데이터 0행. verify 파일의 fetch 를 보세요.")
        verify["estat"] = {"ok": False, "note": "데이터 0행"}
        VERIFY.write_text(json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    periods = sorted({r["period"] for r in rows})
    by, units = {}, {}
    for r in rows:
        by[r["series"]] = by.get(r["series"], 0) + 1
        # 단위는 응답이 준 문자열(百万円 / 千個 …)을 그대로 보관한다.
        # 우리가 환산하면 그 환산이 숫자를 만든다 — 화면에 원문 단위로 싣는다.
        if r["unit"] and r["series"] not in units:
            units[r["series"]] = r["unit"]
    verify["estat"] = {"ok": True, "statsDataId": table["id"], "title": table["title"],
                       "cycle": table["cycle"], "rows": len(rows),
                       "from": periods[0], "to": periods[-1],
                       "series": by, "units": units,
                       "missing": [I.series_key(i.key, m.key) for i in cfg.items
                                   for m in cfg.measures
                                   if I.series_key(i.key, m.key) not in by]}
    VERIFY.parent.mkdir(parents=True, exist_ok=True)
    VERIFY.write_text(json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"수집 {len(rows):,}행 · {periods[0]} ~ {periods[-1]}")
    for k in sorted(by):
        p = I.split_series(k)
        it = cfg.item(p[0]) if p else None
        me = cfg.measure(p[1]) if p else None
        print(f"  {k:<28} {by[k]:>5}행  {it.label if it else '?'} / {me.label if me else '?'}")
    if verify["estat"]["missing"]:
        print(f"  ※ 못 받은 계열: {verify['estat']['missing']} "
              f"— 일본 통계표에 해당 品目이 없을 수 있습니다(verify 의 meta.classes 확인)")

    if args.dry_run:
        print("(dry-run — DB 에 쓰지 않았습니다)")
        return 0
    with Store(args.db) as store:
        st = store.upsert_demand(rows)
    print(f"적재: {st}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
