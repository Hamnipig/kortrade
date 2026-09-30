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
from datetime import datetime, timezone
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


def _save(verify: dict) -> None:
    """★ 어느 경로로 끝나든 진단을 남긴다. 이 파일이 없으면 '왜 비었나'를
    추측으로만 답하게 된다 — 실제로 한 바퀴 돌았다."""
    VERIFY.parent.mkdir(parents=True, exist_ok=True)
    VERIFY.write_text(json.dumps(verify, ensure_ascii=False, indent=1), encoding="utf-8")


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
# ① 통계표 찾기 — **점수로 고르고, 메타로 검증한다**
#
# 2026-09-30 실측 사고. searchWord='電子回路基板' 하나만 돌고 첫 결과에서 멈췄더니
# 47건이 전부 surveyDate 2010xx 의 옛 표였고, 그중
#   「主要製品統計表（時系列）１３４．電子回路基板」(0003040037, 42행,
#    time 클래스 '年月(H19～H25)' = 2007~2013)
# 이 1위로 뽑혔다. 이 표는 제목 자체가 한 品目이라 **品目 분해가 아예 없다** —
# 클래스가 統計項目(2개)과 time 뿐이다. 그래서 品目 매칭이 구조적으로 0이 됐다.
#
# 고친 것 셋.
#   (1) 검색어 하나에서 멈추지 않는다. **전부 돌려 후보를 합친다.**
#       옛 표에는 '電子回路基板'이 제목에 있고 최신 時系列表에는 없다 —
#       첫 검색어에서 멈추면 최신 표를 영원히 못 만난다.
#   (2) 점수에 **최신성**을 넣는다. surveyDate 연도가 오래되면 강하게 깎는다.
#   (3) ★ 점수로 확정하지 않는다. 상위 후보에 **getMetaInfo 를 실제로 걸어**
#       品目·表章項目·최근 월이 모두 잡히는 첫 표를 채택한다. 점수는 시도 순서일
#       뿐이고, 채택 여부는 메타가 정한다. 제목만 보고 고르면 이번 같은 사고가
#       또 난다.
# ══════════════════════════════════════════════════════════════════════════

def _txt(v) -> str:
    """e-Stat 은 TITLE 을 문자열로도, {'@no':..,'$':..} 로도 준다."""
    if isinstance(v, dict):
        return str(v.get("$") or "")
    return "" if v is None else str(v)


def score_table(t: dict, cfg: I.IntlConfig, this_year: int) -> int:
    text = f"{t['stat']} {t['title']}"
    sc = 0
    # 월차가 아니면 트래킹에 못 쓴다 — 연차표는 시차가 1년이다
    sc += 10 if any(c in t["cycle"] for c in cfg.cycle_prefer) else -20
    sc += sum(3 for b in cfg.title_bonus if b in text)
    # 제목에 品目 이름이 있으면 약한 가점. **강하게 주면 안 된다** — '１３４．
    # 電子回路基板' 처럼 제목이 곧 한 品目인 표가 1위로 올라온다(실측 사고).
    sc += min(4, sum(2 for it in cfg.items for w in it.match if w and w in text))
    # 최신성. 이게 없어서 2010년 표가 뽑혔다.
    y = t.get("year")
    if y is None:
        sc += 0
    elif y >= this_year - 2:
        sc += 16
    elif y >= this_year - 6:
        sc += 4
    elif y < 2015:
        sc -= 18
    # 행이 많다 = 品目 분해를 들고 있을 가능성이 크다
    if t["rows"] >= 100:
        sc += 4
    elif t["rows"] < 50:
        sc -= 3
    return sc


def search_tables(cfg: I.IntlConfig, app_id: str) -> tuple[list[dict], list[dict]]:
    """검색어를 **전부** 돌려 후보를 합친다. 첫 결과에서 멈추지 않는다."""
    this_year = int(datetime.now(timezone.utc).strftime("%Y"))
    since = f"{cfg.start.replace('-', '')}"
    passes = [{"statsCode": cfg.stats_code, "surveyYears": f"{since}-{this_year}12"},
              {"statsCode": cfg.stats_code},
              {}]                     # 마지막은 통계코드 제한도 푼다
    found: dict[str, dict] = {}
    tried: list[dict] = []
    for pi, extra in enumerate(passes):
        for word in cfg.search_words:
            params = {"appId": app_id, "searchWord": word, "limit": 200}
            params.update({k: v for k, v in extra.items() if v})
            payload = _get(f"{cfg.base}/getStatsList", params)
            st, msg = I.status_of(payload or {}, "GET_STATS_LIST")
            rec = {"pass": pi, "searchWord": word,
                   "statsCode": extra.get("statsCode"),
                   "surveyYears": extra.get("surveyYears"),
                   "status": st, "http": LAST.get("status"),
                   "msg": (msg or LAST.get("body", ""))[:160], "n": 0, "new": 0}
            tables = [] if payload is None else I.as_list(
                (((payload.get("GET_STATS_LIST") or {}).get("DATALIST_INF") or {})
                 .get("TABLE_INF")))
            rec["n"] = len(tables)
            before = len(found)
            for t in tables:
                tid = str(t.get("@id") or "")
                if not tid or tid in found:
                    continue
                found[tid] = {
                    "id": tid, "title": _txt(t.get("TITLE")),
                    "stat": _txt(t.get("STATISTICS_NAME")),
                    "cycle": str(t.get("CYCLE") or ""),
                    "surveyDate": t.get("SURVEY_DATE"),
                    "year": I.survey_year(t.get("SURVEY_DATE")),
                    "updated": t.get("UPDATED_DATE"),
                    "rows": int(str(t.get("OVERALL_TOTAL_NUMBER") or 0) or 0)
                    if str(t.get("OVERALL_TOTAL_NUMBER") or "0").isdigit() else 0,
                }
            rec["new"] = len(found) - before
            tried.append(rec)
        # 최근 표가 충분히 모였으면 다음 패스는 돌지 않는다
        recent = [c for c in found.values() if (c["year"] or 0) >= this_year - 2]
        if len(recent) >= 5:
            break
    cands = list(found.values())
    for c in cands:
        c["score"] = score_table(c, cfg, this_year)
    cands.sort(key=lambda r: (-r["score"], -(r["year"] or 0), -r["rows"]))
    return cands, tried


# ══════════════════════════════════════════════════════════════════════════
# ② 메타 — 品目과 表章項目을 **이름으로** 맞추고, 그걸로 표를 검증한다
# ══════════════════════════════════════════════════════════════════════════

def meta_from_classes(cfg: I.IntlConfig, objs: list) -> tuple[dict, dict]:
    """CLASS_OBJ[] → 우리가 쓰는 매핑.

    getMetaInfo 와 getStatsData(metaGetFlg=Y) 가 **같은 구조**를 준다. 한 함수로
    쓰는 이유는 vintage 를 이어붙일 때 메타를 따로 받으면 호출이 두 배가 되기 때문이다.
    """
    diag: dict = {"classes": [], "itemHits": [], "measHits": []}
    out: dict = {"items": {}, "meas": {}, "time": {}, "unit": {}}
    for o in objs:
        cid = str(o.get("@id") or "")
        cname = str(o.get("@name") or "")
        classes = I.as_list(o.get("CLASS"))
        diag["classes"].append({"id": cid, "name": cname, "n": len(classes),
                                "sample": [str(c.get("@name")) for c in classes[:10]]})
        if cid == "time":
            for c in classes:
                p = I.parse_period(c.get("@name"), c.get("@code"))
                if p:
                    out["time"][str(c.get("@code"))] = p
            continue
        mhit = {}
        for c in classes:
            nm = str(c.get("@name") or "")
            # ★ first-match 로 고르면 설정에 적은 차례가 결과를 만든다.
            #   구체적인 쪽이 이기게 하고, exclude 로 出荷·在庫를 떼어낸다.
            m = I.pick_measure(nm, cfg.measures)
            if m:
                mhit[str(c.get("@code"))] = m.key
                # 단위는 @unit 이 아니라 **이름 괄호 안**에 있는 경우가 많다
                #   (실측: '生産　金額(百万円)')
                out["unit"][str(c.get("@code"))] = (str(c.get("@unit") or "")
                                                    or I.unit_from_name(nm))
                # 이름 하나에 品目·지표·단위가 다 들어 있는 축이 있다
                # (실측: '製品_0127_…（１０層以上）_B_生産金額_百万円')
        ihit = {}
        for c in classes:
            nm = str(c.get("@name") or "")
            it = I.pick_item(nm, cfg.items)
            if it:
                ihit[str(c.get("@code"))] = it.key
        if mhit:
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


def read_meta(cfg: I.IntlConfig, app_id: str, stats_id: str) -> tuple[dict, dict]:
    """getMetaInfo → meta_from_classes. 후보 검증(probe)에서만 쓴다."""
    payload = _get(f"{cfg.base}/getMetaInfo", {"appId": app_id, "statsDataId": stats_id})
    st, msg = I.status_of(payload or {}, "GET_META_INFO")
    if payload is None or st != 0:
        return ({"items": {}, "meas": {}, "time": {}, "unit": {}},
                {"status": st, "msg": (msg or LAST.get("body", ""))[:200],
                 "http": LAST.get("status"), "classes": [], "itemHits": [], "measHits": []})
    objs = I.as_list((((payload.get("GET_META_INFO") or {}).get("METADATA_INF") or {})
                      .get("CLASS_INF") or {}).get("CLASS_OBJ"))
    out, diag = meta_from_classes(cfg, objs)
    diag.update({"status": st, "msg": (msg or "")[:200], "http": LAST.get("status")})
    return out, diag


# ══════════════════════════════════════════════════════════════════════════
# vintage 이어붙이기
#
# ★ 실측(2026-09-30): 「製品月表 ３５．電子部品」 은 **표 하나가 한 달**이다.
#   같은 제목의 표가 발표월마다 따로 있다(surveyDate 201001, 201002, …).
#   한 표만 받으면 시계열이 1개월이라 전년 동월 비교가 아예 안 된다.
#   반대로 「主要製品統計表（時系列）」 은 표 하나가 여러 달을 들고 있다.
#   어느 쪽인지는 **받아 보면 안다** — 월 수로 판정하고 필요할 때만 이어붙인다.
# ══════════════════════════════════════════════════════════════════════════

def pull_table(cfg: I.IntlConfig, app_id: str, tid: str) -> tuple[list[dict], dict]:
    """표 하나를 메타와 함께 **한 번의 호출로** 받는다 (metaGetFlg=Y).

    vintage 가 100개인데 메타를 따로 받으면 호출이 200회가 된다. 횟수 제한은
    없지만 워크플로 단계 타임아웃이 있다.
    """
    payload = _get(f"{cfg.base}/getStatsData",
                   {"appId": app_id, "statsDataId": tid, "limit": LIMIT,
                    "metaGetFlg": "Y", "cntGetFlg": "N"})
    st, msg = I.status_of(payload or {}, "GET_STATS_DATA")
    d = {"id": tid, "status": st, "http": LAST.get("status"),
         "msg": (msg or LAST.get("body", ""))[:160], "values": 0, "kept": 0}
    if payload is None or st != 0:
        return [], d
    sd = ((payload.get("GET_STATS_DATA") or {}).get("STATISTICAL_DATA") or {})
    objs = I.as_list(((sd.get("CLASS_INF") or {}).get("CLASS_OBJ")))
    meta, _ = meta_from_classes(cfg, objs)
    vals = I.as_list((sd.get("DATA_INF") or {}).get("VALUE"))
    d["values"] = len(vals)
    rows, rd = to_rows(vals, meta, cfg)
    d["kept"], d["skipped"] = len(rows), rd["skipped"]
    return rows, d


def siblings(cands: list[dict], table: dict, cfg: I.IntlConfig) -> list[dict]:
    """같은 계열의 다른 vintage. 최신부터, 시작 연도 이후만.

    ★ 제목을 **그대로** 비교하면 안 된다. 「2025年 時系列表(…)」 계열은 같은 표의
      연도판이 제목 앞 연도만 다르다 — 그대로 비교해서 형제가 자기 자신뿐이었고,
      시계열이 12개월로 끝났다(2026-09-30 실측). 연도 접두사를 떼고 비교한다.
    """
    start_y = int(cfg.start[:4])
    key = I.norm_title(table["title"])

    def sd(c):
        t = str(c.get("surveyDate") or "")
        return int(t[:6]) if t[:6].isdigit() else 0

    out = [c for c in cands
           if I.norm_title(c["title"]) == key and (c["year"] or 0) >= start_y]
    out.sort(key=lambda c: -sd(c))
    return out[:cfg.max_tables]


def probe(cfg: I.IntlConfig, app_id: str, cand: dict) -> tuple[dict, dict]:
    """후보 하나를 **실제 메타로** 검증한다. 점수가 아니라 이게 채택을 정한다."""
    meta, mdiag = read_meta(cfg, app_id, cand["id"])
    n_items = sum(len(m) for m in meta["items"].values())
    n_meas = sum(len(m) for m in meta["meas"].values())
    recent = sorted(p for p in meta["time"].values() if p >= cfg.start)
    ok = bool(n_items and n_meas and recent)
    if ok:
        reason = "채택"
    elif not meta["time"] and not mdiag["classes"]:
        reason = f"메타를 못 읽음 (status {mdiag['status']})"
    elif not n_items:
        reason = ("品目 분해가 없음 — 클래스 " +
                  "/".join(f"{c['id']}({c['n']})" for c in mdiag["classes"]))
    elif not n_meas:
        reason = "表章項目(金額·数量) 매칭 0"
    else:
        last = max(meta["time"].values()) if meta["time"] else "없음"
        reason = f"월 데이터가 {cfg.start} 이후로 없음 (최신 {last})"
    return meta, {"id": cand["id"], "title": cand["title"], "score": cand["score"],
                  "year": cand["year"], "rows": cand["rows"], "ok": ok, "reason": reason,
                  "nItems": n_items, "nMeas": n_meas,
                  "months": len(recent), "first": recent[0] if recent else None,
                  "last": recent[-1] if recent else None, "meta": mdiag}


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
    agg: dict = {}
    seen_items, merged = {}, {}
    skipped = {"noPeriod": 0, "noItem": 0, "noMeas": 0, "nan": 0}
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
        # ★ 여러 品目 코드가 한 키로 모이는 경우가 있다 — '片面'과 '両面'이 둘 다
        #   single_double 로 간다. 마지막 값으로 덮으면 **한쪽이 통째로 사라져**
        #   비중 분모가 틀린다. 같은 (계열, 월)은 합산한다.
        k = (key, period)
        if k in agg:
            agg[k]["value"] += num
            merged[key] = merged.get(key, 0) + 1
        else:
            agg[k] = {"source": I.SOURCE, "series": key, "period": period,
                      "value": num, "unit": str(unit or v.get("@unit") or "")}
    out = list(agg.values())
    return out, {"kept": len(out), "skipped": skipped, "bySeries": seen_items,
                 "merged": merged}


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
        _save(verify)
        return 0

    cands, tried = search_tables(cfg, app_id)
    verify["search"] = {"tried": tried, "found": len(cands), "top": cands[:20]}
    if not cands:
        print("통계표 후보가 하나도 없습니다. verify 의 search.tried 를 보세요.")
        verify["estat"] = {"ok": False, "note": "통계표 후보 0"}
        _save(verify)
        return 0
    print(f"후보 {len(cands)}건 (검색 {len(tried)}회). 상위 {cfg.probe_top}건을 "
          f"메타로 검증합니다 — **점수가 아니라 메타가 채택을 정합니다.**")

    table = meta = None
    probes = []
    for cand in cands[:cfg.probe_top]:
        m, d = probe(cfg, app_id, cand)
        probes.append(d)
        mark = "✓" if d["ok"] else "✗"
        print(f"  {mark} {cand['id']}  {str(cand['title'])[:46]:<46} "
              f"score {cand['score']:>3} · {d['reason']}")
        if d["ok"]:
            table, meta = cand, m
            break
    verify["probes"] = probes
    if table is None:
        print("상위 후보 중 品目 분해 + 최근 월을 모두 가진 표가 없습니다.")
        print("  → verify 의 probes[].meta.classes 에 각 표의 실제 항목 이름이 있습니다. "
              "config/pcb_intl.yaml 의 match 를 거기에 맞추거나 search_words 를 넓히세요.")
        verify["estat"] = {"ok": False,
                           "note": f"검증 통과 표 없음 ({len(probes)}건 확인)"}
        _save(verify)
        return 0

    n_items = sum(len(m) for m in meta["items"].values())
    n_meas = sum(len(m) for m in meta["meas"].values())
    print(f"통계표: {table['id']}  {table['stat']} / {table['title']}  "
          f"[{table['cycle']}] {table['rows']:,}행")
    print(f"메타: 品目 매칭 {n_items}개 · 表章項目 매칭 {n_meas}개 · "
          f"월 {len(meta['time'])}개")

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

    # ── vintage 이어붙이기 ───────────────────────────────────────────────
    # 「製品月表」 계열은 표 하나가 한 달이다. 한 표만 받으면 시계열이 1개월이라
    # 전년 동월 비교가 아예 안 된다 — 화면은 "월 수가 1개뿐" 으로 비어 보이고,
    # 왜 비었는지가 '키 문제'처럼 읽힌다. 월 수를 보고 필요할 때만 이어붙인다.
    got = sorted({r["period"] for r in rows})
    if len(got) < cfg.min_months_single:
        sibs = siblings(cands, table, cfg)
        # 2회차부터는 전 구간을 다시 받지 않는다. 일본도 소급 정정을 하므로 최근
        # 구간은 다시 받아야 하지만, 매달 90여 개 표를 전부 받을 이유는 없다.
        # ★ DB 에 이미 충분히 쌓였을 때만 줄인다 — 첫 수집은 반드시 전 구간이다.
        have = 0
        if not args.dry_run:
            try:
                with Store(args.db) as _s:
                    have = max((len(v) for v in _s.demand(I.SOURCE).values()),
                               default=0)
            except Exception:                            # noqa: BLE001
                have = 0
        full = have < cfg.min_months_single
        if not full:
            sibs = sibs[:cfg.revision_vintages]
        print(f"  이 표는 {len(got)}개월뿐입니다 — 같은 계열 vintage "
              f"{len(sibs)}건을 이어붙입니다 (표 하나가 한 달인 계열, "
              f"{'전 구간' if full else f'최근 {cfg.revision_vintages}개 vintage'}; "
              f"DB 보유 {have}개월).")
        def vintage(c) -> int:
            t = str(c.get("surveyDate") or "")
            return int(t[:6]) if t[:6].isdigit() else 0

        # ★ 같은 (series, period) 가 여러 vintage 에 들어 있다. 일본도 소급 정정을
        #   하므로 **최신 vintage 가 옳다.** 받은 순서로 덮어쓰면 채택된 표가 최신이
        #   아닐 때 옛 값이 이긴다 — 그 사고를 vintage 태그로 막는다.
        tagged = [(vintage(table), r) for r in rows]
        seen = {str(table["id"])}
        pulls, added = [], 0
        for c in sibs:
            if str(c["id"]) in seen:
                continue
            seen.add(str(c["id"]))
            more, d = pull_table(cfg, app_id, str(c["id"]))
            pulls.append(d)
            tagged += [(vintage(c), r) for r in more]
            added += len(more)
        best: dict = {}
        for v, r in tagged:
            k = (r["series"], r["period"])
            if k not in best or v > best[k][0]:
                best[k] = (v, r)
        rows = [r for _, r in best.values()]
        verify["stitch"] = {"family": I.norm_title(table["title"]),
                            "siblings": len(sibs), "pulled": len(pulls),
                            "addedRows": added, "fullRefresh": full,
                            "haveMonths": have,
                            "ids": [{"id": c["id"], "year": c.get("year"),
                                     "surveyDate": c.get("surveyDate")} for c in sibs],
                            "calls": pulls[:40]}
        print(f"  이어붙임: {added:,}행 추가 · 중복 제거 후 {len(rows):,}행")

    if not rows:
        print("데이터 0행. verify 파일의 fetch 를 보세요.")
        verify["estat"] = {"ok": False, "note": "데이터 0행"}
        _save(verify)
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
    _save(verify)

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
