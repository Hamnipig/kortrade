"""e-Stat 응답을 **실측 모양 그대로** 흉내낸다.

2026-09-30 실물(data/pcb_intl_verify.json)에서 확인한 구조를 그대로 옮겼다.
추정으로 만든 replica 로 테스트하면 테스트만 통과하고 실물에서 또 깨진다.

재현하는 것
  · 「{연도}年 時系列表(2350_機械器具月報（その３５）電子部品)」 — 연도마다 표가
    따로 있고 **제목 앞 연도만 다르다.** 한 표가 12개월.
  · cat01 **한 축에 品目·지표·단위가 다 들어 있다**:
      製品_0127_リジッド多層プリント配線板（１０層以上）_B_生産金額_百万円
    그래서 같은 축에서 品目과 表章項目이 동시에 매칭된다.
  · 수량 단위가 千個가 아니라 **m²(면적)**.
  · time 코드는 'YYYYMM' 6자리, 이름은 '2025年1月'.
  · 무관한 電子部品(チップ抵抗器 등)이 같은 표에 섞여 있다.
  · 미끼: 2010년 「主要製品統計表（時系列）」 — 品目 축이 **없고** time 이 和暦.
"""
from __future__ import annotations

# (製品번호, 이름, 우리 키) — 마지막 둘은 매칭되면 안 되는 미끼
PRODUCTS = [
    ("0104", "チップ抵抗器", None),
    ("0105", "その他の固定抵抗器", None),
    ("0123", "リジッド片面プリント配線板", "rigid_single"),
    ("0124", "リジッド両面プリント配線板", "rigid_double"),
    ("0125", "リジッド多層プリント配線板（４層）", "ml_4"),
    ("0126", "リジッド多層プリント配線板（６～８層）", "ml_68"),
    ("0127", "リジッド多層プリント配線板（１０層以上）", "ml_10p"),
    ("0128", "リジッドビルドアップ多層配線板", "buildup"),
    ("0129", "片面フレキシブル配線板", "flex_single"),
    ("0130", "両面・多層フレキシブル配線板", "flex_multi"),
]
YEARS = list(range(2019, 2027))
LAST_MONTH = {2026: 8}                      # 2026 은 8월까지만 나온 상태

# 기울기: 고부가(10층 이상·빌드업)는 크고 범용은 준다 → 믹스 상승
SLOPE = {"ml_10p": +0.016, "buildup": +0.013, "ml_68": +0.002, "ml_4": -0.004,
         "flex_multi": +0.001, "flex_single": -0.005,
         "rigid_double": -0.008, "rigid_single": -0.010, None: 0.0}
BASE = {"ml_10p": 9000.0, "buildup": 12000.0, "ml_68": 11000.0, "ml_4": 7000.0,
        "flex_multi": 14000.0, "flex_single": 6000.0,
        "rigid_double": 8000.0, "rigid_single": 4000.0, None: 25000.0}


def months_of(year: int):
    return [f"{year}-{m:02d}" for m in range(1, LAST_MONTH.get(year, 12) + 1)]


def all_months():
    return [p for y in YEARS for p in months_of(y)]


def _code(pi: int, which: int) -> str:
    return f"2350{10000 + pi * 2 + which:06d}"


def class_obj(year: int):
    cls = []
    for pi, (no, name, _key) in enumerate(PRODUCTS):
        cls.append({"@code": _code(pi, 0),
                    "@name": f"製品_{no}_{name}_A_生産数量_m2"})
        cls.append({"@code": _code(pi, 1),
                    "@name": f"製品_{no}_{name}_B_生産金額_百万円"})
    times = [{"@code": p.replace("-", ""), "@name": f"{p[:4]}年{int(p[5:])}月"}
             for p in months_of(year)]
    return [{"@id": "cat01", "@name": f"{year}_2350_表側", "CLASS": cls},
            {"@id": "time", "@name": f"{year}_実数表_時間軸_表頭", "CLASS": times}]


def class_obj_legacy():
    """2010년 표 — 品目 축이 없고 time 이 和暦. 채택되면 안 된다."""
    return [
        {"@id": "cat01", "@name": "統計項目(機械統計１)",
         "CLASS": [{"@code": "0005100", "@name": "生産　数量(千個)"},
                   {"@code": "0005000", "@name": "生産　金額(百万円)"}]},
        {"@id": "time", "@name": "年月(H19～H25)",
         "CLASS": [{"@code": "2007000000", "@name": "平成19年"},
                   {"@code": "2007000000", "@name": "平成19年度"},
                   {"@code": "2008001111", "@name": "平成20年11月"},
                   {"@code": "2008001212", "@name": "平成20年12月"}]},
    ]


def table_inf():
    out = []
    for i in range(12):                       # 미끼 — 2010년 시계열표
        out.append({"@id": f"00030400{37 + i:02d}",
                    "STATISTICS_NAME": "経済産業省生産動態統計 機械統計 確報（１）生産・出荷・在庫統計",
                    "TITLE": {"@no": "134", "$": "主要製品統計表（時系列） １３４．電子回路基板"},
                    "CYCLE": "月次", "SURVEY_DATE": int(f"2010{i + 1:02d}"),
                    "UPDATED_DATE": "2025-04-14", "OVERALL_TOTAL_NUMBER": "42"})
    for y in YEARS:                           # 진짜 — 연도마다 한 표
        lm = LAST_MONTH.get(y, 12)
        out.append({"@id": f"0004{60000 + y - 2019}",
                    "STATISTICS_NAME": "経済産業省生産動態統計 機械統計 確報（１）生産・出荷・在庫統計",
                    "TITLE": f"{y}年 時系列表(2350_機械器具月報（その３５）電子部品)",
                    "CYCLE": "年次", "SURVEY_DATE": f"{y}01-{y}{lm:02d}",
                    "UPDATED_DATE": "2026-09-20", "OVERALL_TOTAL_NUMBER": "900"})
        # 같은 해의 다른 분야 표 — 계열이 섞이면 안 된다
        out.append({"@id": f"0004{70000 + y - 2019}",
                    "STATISTICS_NAME": "経済産業省生産動態統計 機械統計 確報（１）生産・出荷・在庫統計",
                    "TITLE": f"{y}年 時系列表(2360_機械器具月報（その３６）電子管、半導体素子及び集積回路)",
                    "CYCLE": "年次", "SURVEY_DATE": f"{y}01-{y}{lm:02d}",
                    "UPDATED_DATE": "2026-09-20", "OVERALL_TOTAL_NUMBER": "3168"})
    return out


ID2YEAR = {f"0004{60000 + y - 2019}": y for y in YEARS}
OTHER_IDS = {f"0004{70000 + y - 2019}" for y in YEARS}


def _values(year: int):
    idx = all_months()
    out = []
    for p in months_of(year):
        i = idx.index(p)
        for pi, (_no, _name, key) in enumerate(PRODUCTS):
            v = BASE[key] * max(0.2, 1 + SLOPE[key] * i)
            out.append({"@cat01": _code(pi, 1), "@time": p.replace("-", ""),
                        "$": f"{v:.0f}"})
            out.append({"@cat01": _code(pi, 0), "@time": p.replace("-", ""),
                        "$": f"{v / 2:.0f}"})
    # 비수치 기호 — 0 으로 넣으면 안 되는 것
    out.append({"@cat01": _code(4, 1), "@time": months_of(year)[0].replace("-", ""),
                "$": "-"})
    return out


def fake_get(url: str, params: dict, tries: int = 3):
    """run_pcb_intl._get 를 대체한다."""
    if not params.get("appId"):
        return {"GET_STATS_LIST": {"RESULT": {"STATUS": 1, "ERROR_MSG": "appIdが不正です。"}}}
    if url.endswith("getStatsList"):
        tables = table_inf()
        sy = str(params.get("surveyYears") or "")
        if "-" in sy:
            lo, hi = sy.split("-", 1)
            tables = [t for t in tables
                      if lo[:4] <= str(t["SURVEY_DATE"])[:4] <= hi[:4]]
        return {"GET_STATS_LIST": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。"},
            "DATALIST_INF": {"TABLE_INF": tables}}}
    tid = str(params.get("statsDataId") or "")
    legacy = tid.startswith("00030400")
    if url.endswith("getMetaInfo"):
        objs = class_obj_legacy() if legacy else class_obj(ID2YEAR.get(tid, 2026))
        return {"GET_META_INFO": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。"},
            "METADATA_INF": {"CLASS_INF": {"CLASS_OBJ": objs}}}}
    if url.endswith("getStatsData"):
        if legacy or tid in OTHER_IDS:
            objs = class_obj_legacy() if legacy else class_obj(2026)
            return {"GET_STATS_DATA": {"RESULT": {"STATUS": 0}, "STATISTICAL_DATA": {
                "CLASS_INF": {"CLASS_OBJ": objs},
                "DATA_INF": {"VALUE": []}, "RESULT_INF": {"TOTAL_NUMBER": 0}}}}
        y = ID2YEAR.get(tid)
        if y is None:
            return {"GET_STATS_DATA": {"RESULT": {"STATUS": 1, "ERROR_MSG": "該当データなし"}}}
        return {"GET_STATS_DATA": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。"},
            "STATISTICAL_DATA": {
                "CLASS_INF": {"CLASS_OBJ": class_obj(y)},
                "DATA_INF": {"VALUE": _values(y)},
                "RESULT_INF": {"TOTAL_NUMBER": 900}}}}
    return None
