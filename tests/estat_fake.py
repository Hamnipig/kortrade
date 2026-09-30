"""e-Stat 응답을 **실측 모양 그대로** 흉내낸다 (2026-09-30 실물 기준).

재현하는 것:
  · 「主要製品統計表（時系列）１３４．電子回路基板」 — 2010 vintage, 42행,
    品目 축이 **없다**(제목이 곧 한 品目), time 이름이 和暦
  · 「製品月表 ３５．電子部品」 — vintage(발표월)마다 표가 따로, 122행,
    品目 축이 **있다**, time 은 한 달뿐
이 둘을 섞어 놓아야 '왜 첫 수집이 0행이었는지'가 테스트로 고정된다.
"""
from __future__ import annotations

ITEMS = [("0001", "多層プリント配線板"), ("0002", "ビルドアップ多層プリント配線板"),
         ("0003", "フレキシブルプリント配線板"), ("0004", "片面プリント配線板"),
         ("0005", "両面プリント配線板"), ("0006", "電子回路基板")]
MEAS = [("0005000", "生産　金額(百万円)"), ("0005100", "生産　数量(千個)"),
        ("0006000", "在庫　金額(百万円)"), ("0007000", "出荷　金額(百万円)")]

# 발표월(vintage) 목록 — 2019-01 ~ 2026-08
VINTAGES = [f"{y}{m:02d}" for y in range(2019, 2027) for m in range(1, 13)
            if not (y == 2026 and m > 8)]


def _wareki(period: str) -> str:
    y, m = int(period[:4]), int(period[5:])
    return f"令和{y - 2018}年{m}月" if y >= 2019 else f"平成{y - 1988}年{m}月"


def _ym(v: str) -> str:
    return f"{v[:4]}-{v[4:]}"


def table_inf():
    out = []
    # 옛 시계열표 12건 (2010) — 여기 걸리면 안 된다
    for i, m in enumerate(range(1, 13)):
        out.append({"@id": f"00030400{37 + i:02d}",
                    "STATISTICS_NAME": "経済産業省生産動態統計 機械統計 確報（１）生産・出荷・在庫統計",
                    "TITLE": {"@no": "134", "$": "主要製品統計表（時系列） １３４．電子回路基板"},
                    "CYCLE": "月次", "SURVEY_DATE": int(f"2010{m:02d}"),
                    "UPDATED_DATE": "2025-04-14", "OVERALL_TOTAL_NUMBER": "42"})
    # 製品月表 — vintage 마다 하나
    for i, v in enumerate(VINTAGES):
        out.append({"@id": f"0004{100000 + i}",
                    "STATISTICS_NAME": "経済産業省生産動態統計 機械統計 確報（１）生産・出荷・在庫統計",
                    "TITLE": {"@no": "35", "$": "製品月表 ３５．電子部品"},
                    "CYCLE": "月次", "SURVEY_DATE": int(v),
                    "UPDATED_DATE": "2026-09-20", "OVERALL_TOTAL_NUMBER": "122"})
    return out


def _class_obj_monthly(period: str):
    return [
        {"@id": "cat01", "@name": "品目",
         "CLASS": [{"@code": c, "@name": n} for c, n in ITEMS]},
        {"@id": "cat02", "@name": "統計項目",
         "CLASS": [{"@code": c, "@name": n} for c, n in MEAS]},
        {"@id": "time", "@name": "時間軸(月次)",
         "CLASS": [{"@code": period.replace("-", "") + period[5:], "@name": _wareki(period)}]},
    ]


def _class_obj_legacy():
    # 品目 축이 없다. time 은 和暦 + 年/年度가 섞여 있다.
    return [
        {"@id": "cat01", "@name": "統計項目(機械統計１)",
         "CLASS": [{"@code": "0005100", "@name": "生産　数量(千個)"},
                   {"@code": "0005000", "@name": "生産　金額(百万円)"}]},
        {"@id": "time", "@name": "年月(H19～H25)",
         "CLASS": ([{"@code": f"{y}000000", "@name": f"平成{y - 1988}年"} for y in (2007, 2008)]
                   + [{"@code": f"{y}000000", "@name": f"平成{y - 1988}年度"} for y in (2007,)]
                   + [{"@code": f"2008{m:02d}{m:02d}", "@name": f"平成20年{m}月"}
                      for m in (11, 12)])},
    ]


def _values(period: str, i: int):
    out = []
    # 고부가는 크고 저부가는 줄어드는 그림 — 리포트가 말하는 AI 서버향 전환.
    # 둘 다 크기만 하면 **비중이 포화**해서 YoY %p 변화가 문턱을 못 넘는다
    # (실제로 그렇게 만들었다가 판정이 '믹스 미확인'으로 나왔다).
    g = max(0.35, 1 - 0.006 * i)
    hv = 1 + 0.013 * i
    amt = {"0001": 30000 * hv, "0002": 12000 * hv, "0003": 18000 * g,
           "0004": 4000 * g, "0005": 5000 * g, "0006": 69000 * g}
    tcode = period.replace("-", "") + period[5:]
    for code, _ in ITEMS:
        out.append({"@cat01": code, "@cat02": "0005000", "@time": tcode,
                    "@unit": "百万円", "$": f"{amt[code]:.0f}"})
        out.append({"@cat01": code, "@cat02": "0005100", "@time": tcode,
                    "@unit": "千個", "$": f"{amt[code] / 3:.0f}"})
        out.append({"@cat01": code, "@cat02": "0006000", "@time": tcode,
                    "@unit": "百万円", "$": f"{amt[code] * 0.4:.0f}"})
        # 비수치 기호 — 0 으로 넣으면 안 되는 것
        out.append({"@cat01": code, "@cat02": "0007000", "@time": tcode,
                    "@unit": "百万円", "$": "-"})
    return out


ID2VINTAGE = {f"0004{100000 + i}": v for i, v in enumerate(VINTAGES)}


def fake_get(url: str, params: dict, tries: int = 3):
    """run_pcb_intl._get 를 대체한다. appId 가 없으면 e-Stat 처럼 거절한다."""
    if not params.get("appId"):
        return {"GET_STATS_LIST": {"RESULT": {"STATUS": 1, "ERROR_MSG": "appIdが不正です。"}}}
    if url.endswith("getStatsList"):
        tables = table_inf()
        sy = str(params.get("surveyYears") or "")
        if "-" in sy:
            lo, hi = sy.split("-", 1)
            tables = [t for t in tables if lo <= str(t["SURVEY_DATE"]) <= hi]
        return {"GET_STATS_LIST": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。"},
            "DATALIST_INF": {"TABLE_INF": tables}}}
    tid = str(params.get("statsDataId") or "")
    legacy = tid.startswith("00030400")
    if url.endswith("getMetaInfo"):
        objs = _class_obj_legacy() if legacy else _class_obj_monthly(
            _ym(ID2VINTAGE.get(tid, "202608")))
        return {"GET_META_INFO": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。"},
            "METADATA_INF": {"CLASS_INF": {"CLASS_OBJ": objs}}}}
    if url.endswith("getStatsData"):
        if legacy:
            return {"GET_STATS_DATA": {
                "RESULT": {"STATUS": 0}, "STATISTICAL_DATA": {
                    "CLASS_INF": {"CLASS_OBJ": _class_obj_legacy()},
                    "DATA_INF": {"VALUE": []}, "RESULT_INF": {"TOTAL_NUMBER": 0}}}}
        v = ID2VINTAGE.get(tid)
        if v is None:
            return {"GET_STATS_DATA": {"RESULT": {"STATUS": 1, "ERROR_MSG": "該当データなし"}}}
        per = _ym(v)
        return {"GET_STATS_DATA": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。"},
            "STATISTICAL_DATA": {
                "CLASS_INF": {"CLASS_OBJ": _class_obj_monthly(per)},
                "DATA_INF": {"VALUE": _values(per, VINTAGES.index(v))},
                "RESULT_INF": {"TOTAL_NUMBER": 122}}}}
    return None
