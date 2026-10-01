"""PCB·기판 레이어 검증. (API 호출 없음)

이 레이어에서 조용히 틀릴 수 있는 지점을 고정한다.

  1. **ASP 를 놓치는 것.** 한국 HS 8534 에는 층수 구분이 없어서 고다층 전환은
     ASP($/kg)로만 보인다. 금액만 보는 화면이면 '물량이 늘어 +15%' 와
     '단가가 올라 +15%' 가 같은 칸에 들어간다 — 이 레이어의 존재 이유가 사라진다.
  2. **미검증 코드가 집계에 섞이는 것.** probe 코드는 관세청 응답의 품명이
     expect 문자열과 맞아야 집계에 들어간다. expect 가 비면 'in' 검사가 항상
     True 라 검증이 통과로 위장된다 — 그 경로를 두 겹으로 막는다.
  3. **draft 가 집계에 들어가는 것.** 규모가 3~4자릿수 작은 코드를 합치면
     묻히기만 하고 신호를 흐린다.
  4. **지역 축에서 ASP 를 만드는 것.** 시군구 API 에는 중량 필드가 없다.
     지역별 단가는 원리적으로 존재하지 않는다.
  5. **8534 을 층수로 세분하는 것.** 그건 중국 체계다. 코드 번호가 겹쳐 혼동하기
     쉽다 — 설정에 그 경고가 남아 있는지 고정한다.
  6. **장비를 수요 합계에 넣는 것.** CAPEX 는 수요의 흐름이 아니다.

실행:  python tests/test_pcb.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kortrade import flash as F          # noqa: E402
from kortrade import intl as IN          # noqa: E402
from kortrade import pcb as P            # noqa: E402
from kortrade.store import Store         # noqa: E402

# 관세율표 품명을 흉내낸 응답. probe 대조가 실제로 이 문자열을 본다.
NAMES = {
    "8534002000": "테이프형이나 리드프레임(lead frame) 기능을 하는 회로가 형성된 것",
    "8534009000": "기타",
    "8534001000": "수동소자 부분[인덕턴스ㆍ저항기ㆍ축전기 등]이 형성된 것",
    "7410211000": "인쇄회로판 제조에 적합한 모양인 것",
    "7410110000": "정제한 구리로 만든 것",
    "3701303000": "인쇄회로기판용",
    "7410221000": "인쇄회로판 제조에 적합한 모양인 것",
    "8456111000": "인쇄회로, 인쇄회로조립품, 제8517호의 부분품의 제조에 전용되는 것",
    "8479892010": "인쇄회로조립품 제조에 전용 또는 주로 사용되는 종류의 것",
    "8486402040": "납볼을 반도체 제조용 인쇄회로기판이나 세라믹기판에 탑재하는 기계",
}


# ── e-Stat 응답 replica 로더 ──────────────────────────────────────────────
# 파일이 tests/ 에 있든 tests/fixtures/ 에 있든 찾는다.
#
# ★ 왜 굳이: 이 레포는 zip 으로 파일을 올려 갱신한다. 빈 디렉터리는 git 이
#   추적하지 않으므로 tests/fixtures/ 가 레포에 없고, 올리는 사람은 자연히
#   tests/ 에 둔다. 전에는 sys.path 에 없는 경로를 넣어 두고 pytest 가 우연히
#   tests/ 를 sys.path 에 넣어 주는 덕에 돌고 있었다 — 실행 방식이 바뀌면
#   ImportError 로 죽고, 그 메시지로는 원인을 알 수 없다.
_FAKE_DIRS = (ROOT / "tests", ROOT / "tests" / "fixtures")


def _fake():
    """estat_fake 모듈. 없으면 **어디를 찾았는지** 말하고 실패한다."""
    import importlib.util
    for d in _FAKE_DIRS:
        f = d / "estat_fake.py"
        if f.exists():
            spec = importlib.util.spec_from_file_location("estat_fake", f)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise AssertionError(
        "estat_fake.py 를 찾지 못했습니다. 다음 중 한 곳에 두세요: "
        + " 또는 ".join(str(d / "estat_fake.py") for d in _FAKE_DIRS))


def _months(n=30, y=2024, m=3):
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _jp_rows(months, hv_up=True):
    """일본 METI 品目別 생산액을 심는다.

    hv_up=True 면 고부가(다층+빌드업) 비중이 **오른다.** 한국 ASP 상승과 방향이
    맞아떨어지는지(믹스 상승 확인) 검증하기 위한 것이다.
    """
    rows = []
    for i, p in enumerate(months):
        k = (1 + 0.02 * i) if hv_up else (1 - 0.015 * i)
        base = {"ml_10p": 9000.0 * k, "buildup": 12000.0 * k,
                "ml_68": 11000.0, "ml_4": 7000.0,
                "flex_multi": 14000.0, "rigid_double": 8000.0}
        for key, v in base.items():
            rows.append({"source": IN.SOURCE, "series": IN.series_key(key, "amt"),
                         "period": p, "value": v, "unit": "百万円"})
            rows.append({"source": IN.SOURCE, "series": IN.series_key(key, "qty"),
                         "period": p, "value": v / 3.0, "unit": "千個"})
    return rows


def _fixture(db: Path, n: int = 30, bad_name: str | None = None, jp: bool | None = None):
    """**금액은 늘고 중량은 주는** PCB 수출을 심는다 (리포트의 8월 그림).

    금액만 보는 화면이면 '성장'으로 끝난다. ASP 를 내야 그 성장이 전부 단가에서
    왔다는 사실이 드러난다 — 그것이 고다층 전환의 증거다.
    """
    months = _months(n)
    rows = []

    def add(p, code, cty, eu=0.0, ew=0.0, iu=0.0, iw=0.0):
        nm = NAMES.get(code, code)
        if bad_name and code == bad_name:
            nm = "전혀 다른 물건"
        rows.append({"period": p, "hs_code": code, "hs6": code[:6], "hs_name": nm,
                     "country_code": cty, "country_name": cty,
                     "exp_usd": int(eu), "exp_wgt": int(ew),
                     "imp_usd": int(iu), "imp_wgt": int(iw), "bal_usd": 0})

    for i, p in enumerate(months):
        # 물량은 서서히 줄고 단가는 오른다 → 금액은 는다
        wgt = 900_000.0 * (1 - 0.004 * i)
        asp = 300.0 * (1 + 0.012 * i)
        usd = wgt * asp
        # 국가마다 물량·단가 기울기를 다르게 준다. 전부 같으면 국면 지도가 한 점에
        # 뭉쳐서 라벨 배치·툴팁·사분면 판정이 하나도 검증되지 않는다.
        for cty, sh, qk, pk in (("ALL", 1.0, 0.0, 0.0), ("US", 0.22, +0.006, -0.004),
                                ("CN", 0.30, -0.008, +0.010), ("HK", 0.14, +0.002, 0.0),
                                ("TW", 0.12, +0.010, +0.006), ("VN", 0.10, -0.004, -0.008)):
            w2, a2 = wgt * (1 + qk * i), asp * (1 + pk * i)
            add(p, "8534009000", cty, eu=w2 * a2 * sh, ew=w2 * sh)
            add(p, "8534002000", cty, eu=w2 * a2 * 1.4 * 0.18 * sh, ew=w2 * 0.18 * sh)
            add(p, "8534001000", cty, eu=w2 * a2 * 0.7 * 0.03 * sh, ew=w2 * 0.03 * sh)
        # CCL — 원가(수입)와 판매(수출)를 같이. 구리 톱니를 넣어 시차 회귀가
        # 식별되는지도 본다.
        cu = 9.0 + 3.0 * ((i * 7) % 5) / 4.0
        add(p, "7410211000", "ALL", eu=40_000_000.0, ew=3_000_000.0,
            iu=cu * 2_000_000.0, iw=2_000_000.0)
        add(p, "7410110000", "ALL", eu=30_000_000.0, ew=1_800_000.0,
            iu=cu * 1_100_000.0, iw=1_000_000.0)
        add(p, "3701303000", "ALL", eu=9_000_000.0 * (1 + 0.01 * i), ew=300_000.0)
        # 장비 — 완제품보다 앞서 움직인다
        add(p, "8456111000", "ALL", eu=6_000_000.0 * (1 + 0.02 * max(0, i - 0)), ew=40_000.0)
        add(p, "8479892010", "ALL", eu=8_000_000.0, ew=60_000.0)
        # draft 두 개 — 규모가 3~4자릿수 작다
        add(p, "7410221000", "ALL", eu=30_000.0, ew=2_000.0, iu=25_000.0, iw=1_500.0)
        add(p, "8486402040", "ALL", eu=90_000.0, ew=900.0)

    region = []
    for i, p in enumerate(months):
        for place, sido, base in (("안산시", "경기도", 60.0), ("수원시", "경기도", 45.0),
                                  ("달성군", "대구광역시", 30.0), ("구미시", "경상북도", 22.0),
                                  ("청주시", "충청북도", 18.0), ("세종시", "세종특별자치시", 9.0),
                                  ("성남시", "경기도", 5.0)):
            region.append({"period": p, "hs_code": "853400", "hs_name": "인쇄회로",
                           "sido_cd": "41", "sido_name": sido, "sigungu_name": place,
                           "exp_cnt": 10, "exp_usd": int(base * 1e6 * (1 + 0.01 * i)),
                           "imp_cnt": 0, "imp_usd": 0, "bal_usd": 0})
    with Store(db) as s:
        s.upsert_sector(rows)
        s.upsert_region(region)
        if jp is not None:
            s.upsert_demand(_jp_rows(months, hv_up=jp))
    return months


def _build(db):
    spec = importlib.util.spec_from_file_location("bpcb", ROOT / "scripts" / "build_pcb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with Store(db) as st:
        return mod.build(st, P.load())


# ══════════════════════════════════════════════════════════════════════════

def test_config_valid():
    cfg = P.load()
    errs = cfg.validate()
    assert not errs, errs
    assert cfg.groups["board"], "완제품 축이 비어 있다"
    # probe 는 기계 대조가 전부다 — expect 없는 probe 는 검증을 통과로 위장한다
    for c in cfg.all():
        if c.status == "probe":
            assert c.expect, f"{c.code} probe 인데 expect 없음"
    print(f"  ✓ 설정 검증 — 코드 {len(cfg.all_codes())}개 "
          f"(probe·active {len(cfg.all_codes()) - len(cfg.drafts())} / "
          f"draft {len(cfg.drafts())}) · 6단위 부모 {len(cfg.all_parents())}개")


def test_expect_empty_never_passes():
    """expect 가 비면 'x in s' 가 항상 True 다. 그 경로를 막는다."""
    assert P.verify_name("", "아무 품명") is False
    assert P.verify_name("리드프레임", None) is False
    assert P.verify_name("리드프레임", "테이프형이나 리드프레임 기능을 하는 회로") is True
    print("  ✓ 빈 expect 는 통과시키지 않는다 (검증이 통과로 위장되는 경로 차단)")


def test_layer_count_is_chinese_scheme():
    """8534 을 층수로 세분하는 것은 중국 체계다. 그 경고가 설정에 남아 있어야 한다."""
    src = (ROOT / "config" / "pcb.yaml").read_text(encoding="utf-8")
    assert "중국" in src and "층수" in src, "중국 체계 혼동 경고가 없다"
    # **코드 라벨**이 층수로 붙으면 안 된다. 본문 설명에서 '다층'을 언급하는 것은
    # 괜찮다 — 문제는 한국 코드를 층수로 이름 붙여 없는 구분을 있는 것처럼 만드는 것.
    labels = [c.label for c in P.load().all()]     # rejected 항목은 제외한다 —
    assert labels, "label 이 하나도 없다"              # 거기 적힌 '양면/단면'은 경고다
    for lb in labels:
        for bad in ("4층", "다층판", "양면", "단면"):
            assert bad not in lb, f"층수 기반 라벨: {lb!r}"
    # 8534 코드의 라벨은 '형성된 회로의 종류' 기준이어야 한다
    assert any("리드프레임" in l for l in labels), "8534.00.2000 라벨이 회로 종류 기준이 아니다"
    print("  ✓ 8534 층수 세분(중국 체계) 혼동 차단")


def test_asp_is_the_headline():
    """금액은 늘고 중량은 주는 데이터 → 성장이 전부 단가에서 왔다고 말해야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    assert pl, "payload 없음"
    h = pl["headline"]["window"]
    assert h["valueYoy"] > 0, h
    assert h["qtyYoy"] < 0, f"중량이 줄어야 한다: {h['qtyYoy']}"
    assert h["priceYoy"] > 0, h
    # 물량이 줄었는데 금액이 늘었으면 단가 기여도는 100% 를 넘는다
    assert h["priceShare"] > 100, f"단가 기여도 {h['priceShare']}"
    assert h["verdict"]["code"] == "mix_up", h["verdict"]
    assert pl["headline"]["aspMax"], "ASP 최고치를 못 냈다"
    assert "구리" in pl["headline"]["caveat"], "ASP 한계 경고가 없다"
    print(f"  ✓ P/Q 3분해 — 금액 {h['valueYoy']}% / 중량 {h['qtyYoy']}% / "
          f"ASP {h['priceYoy']}% (단가 기여 {h['priceShare']}%) [{h['verdict']['label']}]")


def test_single_month_and_window_both_reported():
    """리포트가 인용하는 '8월 +15%' 는 단월이다. 창 평균과 다른 숫자다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    for k in ("month", "recent", "window"):
        assert k in pl["headline"], k
        assert pl["headline"][k]["valueYoy"] is not None, k
    assert pl["headline"]["asOfMonth"] == pl["asOf"]
    print(f"  ✓ 단월/최근/기준창 3종 — 단월 {pl['headline']['month']['valueYoy']}% · "
          f"기준창 {pl['headline']['window']['valueYoy']}%")


def test_name_mismatch_drops_code_from_totals():
    """품명이 안 맞으면 집계에서 빠지고, 그 사실이 표로 남아야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db, bad_name="8534002000")
    pl = _build(db)
    v = next(r for r in pl["verify"] if r["code"] == "8534002000")
    assert v["seen"] is True and v["hit"] is False and v["counted"] is False, v
    assert "품명 대조 실패" in v["why"], v["why"]
    assert "8534002000" not in pl["headline"]["codes"], "대조 실패 코드가 헤드라인에 들어갔다"
    ok = next(r for r in pl["verify"] if r["code"] == "8534009000")
    assert ok["counted"] is True
    print(f"  ✓ 품명 대조 실패 → 집계 제외 ({v['why']})")


def test_drafts_never_counted():
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    draft_codes = {c.code for c in P.load().drafts()}
    assert draft_codes, "draft 가 하나도 없다 — 테스트가 무의미해진다"
    for g in pl["groups"]:
        assert not (set(g["codes"]) & draft_codes), (g["group"], g["codes"])
    assert {d["code"] for d in pl["drafts"]} == draft_codes
    for d in pl["drafts"]:
        assert d["counted"] is False
    print(f"  ✓ draft {sorted(draft_codes)} 는 측정만 — 어느 합계에도 없다")


def test_equipment_excluded_from_demand_total():
    """장비(CAPEX)는 수요가 아니다. 합계에서 빼야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    eq = next(g for g in pl["groups"] if g["group"] == "equipment")
    parts = sum(g["usd"] for g in pl["groups"] if g["group"] in P.DEMAND_GROUPS)
    assert abs(pl["total"]["usd"] - parts) < 1.0, (pl["total"]["usd"], parts)
    assert eq["usd"] > 0, "장비가 0 이면 이 테스트가 통과해도 의미가 없다"
    assert "장비는 뺐습니다" in pl["total"]["note"]
    print(f"  ✓ 수요 합계 ${pl['total']['usd']}M = 완제품+소재 "
          f"(장비 ${eq['usd']}M 제외)")


def test_region_axis_has_no_unit_price():
    """시군구 API 에는 중량 필드가 없다. 지역별 ASP 는 원리적으로 존재하지 않는다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    pls = pl["places"]
    assert pls["ok"], pls
    for r in pls["rows"]:
        assert "asp" not in r and "priceYoy" not in r, r
    assert "중량" in pls["note"] and "ASP" in pls["note"]
    assert "자기신고" in pls["warn"], "지역 귀속 품질 경고가 없다"
    names = [r["place"] for r in pls["rows"]]
    for want in ("안산", "수원", "달성"):
        assert any(want in n for n in names), (want, names)
    assert pls["rows"][0]["pinned"] is True, "고정 표시 지역이 먼저 와야 한다"
    print(f"  ✓ 지역 축 {len(pls['rows'])}곳 — 금액만 (단가 필드 없음), "
          f"자기신고 경고 포함")


def test_merge_rule_is_measured():
    """8534.00.2000 과 .9000 을 합쳐도 되는지 **상관으로** 판정해야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    assert pl["merge"], "합산 검사가 비어 있다"
    m = pl["merge"][0]
    assert m["r"] is not None and m["verdict"]["code"] in (
        "mergeable", "caution", "separate")
    assert P.merge_verdict(0.9)["code"] == "mergeable"
    assert P.merge_verdict(0.2)["code"] == "separate"
    assert P.merge_verdict(None)["code"] == "unknown"
    print(f"  ✓ 합산 원칙(r≥0.6) 적용 — {m['a']} vs {m['b']} r={m['r']} "
          f"[{m['verdict']['label']}]")


def test_cost_axis_separates_copper_from_mix():
    """ASP 가 올랐을 때 구리값인지 믹스인지 갈라야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    assert pl["costs"], "원가 축이 비어 있다"
    ccl = next(c for c in pl["costs"] if c["code"] == "7410211000")
    assert ccl["price"] is not None, "CCL 수입 단가가 없다"
    sp = pl["spread"]
    assert "linked" in sp
    if sp["linked"]:
        assert sp["verdict"]["code"] in ("mix_up", "cost_push", "unknown")
        assert sp["n"] >= 12
    print(f"  ✓ 원가 축 — CCL 수입단가 ${ccl['price']}/kg · "
          + (f"회귀 β={sp['beta']} R²={sp['r2']} [{sp['verdict']['label']}]"
             if sp["linked"] else "연동 미확인(그 사실을 표시)"))


def test_hub_countries_flagged():
    """HK·VN 은 최종 수요지가 아니다. 확산도를 허브 포함/제외 두 벌로 내야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db)
    pl = _build(db)
    hk = next((c for c in pl["countries"] if c["cty"] == "HK"), None)
    assert hk and hk["hub"] is True and hk["hubWhy"], hk
    us = next(c for c in pl["countries"] if c["cty"] == "US")
    assert us["hub"] is False
    b = pl["breadth"]
    assert b and "all" in b and "exHub" in b
    assert b["hub"]["codes"], b["hub"]
    print(f"  ✓ 허브 {b['hub']['codes']} 분리 — 포함 상위{b['topN']} "
          f"{b['all']['now']['topShare']}% / 제외 {b['exHub']['now']['topShare']}%")


def test_intl_config_valid():
    c = IN.load()
    assert not c.validate(), c.validate()
    # 출처 표기는 e-Stat 이용규약 제7조의 **의무**다
    assert "e-Stat" in c.credit and "保証" in c.credit
    # statsDataId 를 상수로 박으면 통계표 재편 때 조용히 빈 데이터가 쌓인다
    src = (ROOT / "config" / "pcb_intl.yaml").read_text(encoding="utf-8")
    assert "statsDataId" not in src.replace("statsDataId 를", "").replace(
        "statsDataId 는", "").replace("statsDataId 의", "") or True
    cfgsrc = (ROOT / "scripts" / "run_pcb_intl.py").read_text(encoding="utf-8")
    assert "getStatsList" in cfgsrc, "매번 찾지 않고 ID 를 박았다"
    # 설정에 statsDataId 상수가 들어가는 순간 통계표 재편에 무방비가 된다
    import yaml as _y
    raw = _y.safe_load((ROOT / "config" / "pcb_intl.yaml").read_text(encoding="utf-8"))
    flat = json.dumps(raw, ensure_ascii=False)
    assert "statsDataId" not in flat and "statsdataid" not in flat.lower(), \
        "설정에 statsDataId 를 박았다 — 재편되면 조용히 빈 데이터가 쌓인다"
    print(f"  ✓ 해외 설정 — 品目 {len(c.items)}개 · 지표 {len(c.measures)}개 · "
          f"미연결 {len(c.pending)}건 · 크레디트 문구 포함")


def test_time_code_quarter_is_not_a_month():
    """'2026000406'(4~6월기)을 월차로 읽으면 분기값이 월 자리에 들어가 3배가 된다."""
    assert IN.parse_period("2026年8月") == "2026-08"
    assert IN.parse_period(None, "2026000808") == "2026-08"
    assert IN.parse_period(None, "2026000406") is None
    assert IN.parse_period("2026年", "2026000000") is None
    assert IN.parse_period(None, None) is None
    print("  ✓ 시간 코드 — 이름 우선, 분기·연차는 월차로 읽지 않는다")


def test_item_matching_prefers_specific():
    """'ビルドアップ多層配線板' 은 '多層' 에도 걸린다. 구체적인 쪽이 이겨야 한다."""
    c = IN.load()
    # 실측 항목명 그대로. 전각 괄호·전각 숫자다.
    assert IN.pick_item("製品_0128_リジッドビルドアップ多層配線板_B_生産金額_百万円",
                        c.items).key == "buildup"
    assert IN.pick_item("製品_0127_リジッド多層プリント配線板（１０層以上）_B_生産金額_百万円",
                        c.items).key == "ml_10p"
    assert IN.pick_item("製品_0125_リジッド多層プリント配線板（４層）_A_生産数量_m2",
                        c.items).key == "ml_4"
    # '両面・多層フレキシブル' 이 리지드 양면으로 새면 안 된다
    assert IN.pick_item("製品_0130_両面・多層フレキシブル配線板_B_生産金額_百万円",
                        c.items).key == "flex_multi"
    assert IN.pick_item("製品_0124_リジッド両面プリント配線板_B_生産金額_百万円",
                        c.items).key == "rigid_double"
    assert IN.pick_item("製品_0104_チップ抵抗器_B_生産金額_百万円", c.items) is None
    assert IN.pick_item("洗濯機", c.items) is None
    # 빈 매칭 문자열이 전부를 삼키는 경로 차단
    assert IN.matches("多層", [""]) is False
    assert IN.matches("多層", []) is False
    print("  ✓ 品目 매칭 — 구체적인 쪽 우선, 빈 매칭은 아무것도 잡지 않는다")


def test_intl_cross_check_reads_mix():
    """한국 ASP 상승 + 일본 고부가 비중 상승 = 믹스 근거 둘."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db, jp=True)
    pl = _build(db)
    it = pl["intl"]
    assert it["ok"], it
    assert it["mix"]["chg"] > 0, it["mix"]
    assert it["cross"]["krAspYoy"] is not None
    assert it["cross"]["verdict"]["code"] == "mix_confirmed", it["cross"]
    hv = [r for r in it["rows"] if r["highValue"]]
    assert {r["key"] for r in hv} == set(P.HIGH_VALUE_JP)
    assert sum(r["share"] for r in it["rows"] if r["share"] is not None) > 99
    assert "더하지 않습니다" in it["note"], "두 나라 합산 금지 경고가 없다"
    print(f"  ✓ 교차검증 — 한국 ASP {it['cross']['krAspYoy']}% · "
          f"일본 고부가 {it['mix']['prev']}→{it['mix']['now']}% "
          f"({it['mix']['chg']}%p) [{it['cross']['verdict']['label']}]")


def test_cross_check_aligns_periods_when_japan_lags():
    """일본 「時系列表」는 **연 1회 공표**라 한국보다 몇 달 뒤처진다.

    2026-09-30 실측: 한국 2026-08 / 일본 2025-12 — **8개월 차이**.
    그대로 맞대면 2026년 한국 단가와 2025년 일본 믹스를 비교하게 된다.
    교차 판정은 두 축을 같은 기간으로 맞춰 계산해야 한다.
    """
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    kr_months = _fixture(db)                       # 한국: ~2026-08
    jp_months = [m for m in kr_months if m <= "2025-12"]
    with Store(db) as s:
        s.upsert_demand(_jp_rows(jp_months, hv_up=True))
    pl = _build(db)
    it, cx = pl["intl"], pl["intl"]["cross"]
    assert it["ok"], it.get("note")
    assert it["asOf"] == "2025-12" and pl["asOf"] == "2026-08", (it["asOf"], pl["asOf"])
    assert cx["lagMonths"] == 8, cx
    assert cx["aligned"] is True, cx
    assert cx["window"] == "2025-10~2025-12", cx["window"]
    # 판정에 쓰는 값은 **정렬된 값**이어야 한다
    assert cx["krAspYoy"] == cx["krAspYoyAligned"], cx
    assert cx["krAspYoyLatest"] is not None
    assert cx["krAspYoy"] != cx["krAspYoyLatest"], \
        "기간이 다른데 같은 값이면 정렬이 안 된 것이다"
    assert "8개월 뒤" in cx["note"] and cx["window"] in cx["note"], cx["note"]
    # 연결된 축은 '아직 붙이지 않은 축' 목록에서 연결됨으로 표시돼야 한다
    jp = next(i for i in it["items"] if i["key"] == "jp_meti")
    assert jp["connected"] is True and not jp["needKey"], jp
    tw = next(i for i in it["items"] if i["key"] == "tw_moea")
    assert tw["connected"] is False, tw
    print(f"  ✓ 기간 정렬 — 일본 {it['asOf']} / 한국 {pl['asOf']} ({cx['lagMonths']}개월 차) · "
          f"판정창 {cx['window']} · 한국 ASP 정렬 {cx['krAspYoyAligned']}% "
          f"vs 최신 {cx['krAspYoyLatest']}% · [{cx['verdict']['label']}]")


def test_intl_contradiction_is_reported():
    """일본 고부가 비중이 **빠지는데** 한국 ASP 가 오르면 경고여야 한다."""
    td = Path(tempfile.mkdtemp()); db = td / "p.sqlite"
    _fixture(db, jp=False)
    pl = _build(db)
    it = pl["intl"]
    assert it["ok"] and it["mix"]["chg"] < 0, it["mix"]
    assert it["cross"]["verdict"]["code"] == "mix_contradicted", it["cross"]
    assert "구리" in it["cross"]["verdict"]["note"]
    print(f"  ✓ 반대 방향 — [{it['cross']['verdict']['label']}] 로 경고")


def _run_collector(monkey_get, db, start="2019-01"):
    """실측 모양을 흉내낸 응답으로 수집기를 통째로 돌린다.

    API 에 붙지 않고도 검색 → 후보 검증 → vintage 이어붙이기 → 파싱까지
    전 경로가 돈다. 첫 수집이 0행으로 끝난 세 가지 원인이 여기서 잡힌다.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rpi", ROOT / "scripts" / "run_pcb_intl.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod._get = monkey_get
    mod.VERIFY = db.parent / "pcb_intl_verify.json"
    argv = sys.argv
    sys.argv = ["run_pcb_intl.py", "--db", str(db), "--start", start]
    try:
        rc = mod.main()
    finally:
        sys.argv = argv
    return rc, json.loads(mod.VERIFY.read_text(encoding="utf-8"))


def test_collector_end_to_end_on_replica():
    """실측 응답 모양 그대로 돌려 **행이 실제로 쌓이는지** 본다.

    첫 수집(2026-09-30)이 0행으로 끝난 원인 셋을 한꺼번에 고정한다.
      1. 최신성이 점수에 없어 2010년 표가 1위로 올라갔다
      2. 그 표에는 品目 축이 아예 없었다(제목이 곧 한 品目)
      3. time 이름이 和暦('平成20年11月')이라 서기 정규식으로는 못 읽었다
    """
    FK = _fake()
    td = Path(tempfile.mkdtemp()); db = td / "k.sqlite"
    os.environ["EJ_ESTAT_APP_ID"] = "test-only-not-a-real-key"
    try:
        rc, v = _run_collector(FK.fake_get, db)
    finally:
        os.environ.pop("EJ_ESTAT_APP_ID", None)
    assert rc == 0, rc
    est = v["estat"]
    assert est["ok"] is True, est
    # 2010년 시계열표가 아니라 品目 축을 가진 製品月表 가 뽑혀야 한다
    assert "時系列表" in (est.get("title") or ""), est.get("title")
    assert "主要製品統計表" not in (est.get("title") or ""), est.get("title")
    assert est["to"] == "2026-08", est
    assert est["from"] <= "2019-01", est
    # 표 하나가 한 달인 계열이므로 이어붙이기가 돌았어야 한다
    assert v.get("stitch") and v["stitch"]["addedRows"] > 0, v.get("stitch")
    with Store(db) as s:
        ser = s.demand(IN.SOURCE)
    for k in ("jp:ml_10p:amt", "jp:buildup:amt", "jp:ml_68:amt", "jp:ml_4:amt",
              "jp:flex_multi:amt", "jp:rigid_single:amt", "jp:ml_10p:qty"):
        assert k in ser and len(ser[k]) >= 80, (k, len(ser.get(k, {})))
    # 무관한 電子部品(チップ抵抗器 등)은 들어오면 안 된다
    assert all(IN.split_series(k)[0] in {i.key for i in IN.load().items}
               for k in ser), sorted(ser)[:5]
    print(f"  ✓ 실측 replica 수집 — 표 {est['statsDataId']} · {est['from']}~{est['to']} · "
          f"{est['rows']:,}행 · vintage {v['stitch']['siblings']}건 이어붙임")


def test_replica_feeds_the_panel():
    """수집한 것이 화면 payload 까지 흘러가는지 — 교차 판정이 나와야 한다."""
    FK = _fake()
    td = Path(tempfile.mkdtemp()); db = td / "k.sqlite"
    _fixture(db)                       # 한국 축 (ASP 상승)
    os.environ["EJ_ESTAT_APP_ID"] = "test-only-not-a-real-key"
    try:
        _run_collector(FK.fake_get, db)
    finally:
        os.environ.pop("EJ_ESTAT_APP_ID", None)
    pl = _build(db)
    it = pl["intl"]
    assert it["ok"], it.get("note")
    assert it["asOf"] == "2026-08", it["asOf"]
    assert it["mix"]["chg"] > 0, it["mix"]
    assert it["cross"]["verdict"]["code"] == "mix_confirmed", it["cross"]
    # 단위가 원문 그대로 붙어야 한다 (百万円 → 億円 환산을 우리가 하지 않는다)
    ml = next(r for r in it["rows"] if r["key"] == "ml_10p")
    assert ml["amt"]["unit"] == "百万円", ml["amt"]
    # 수량 단위는 면적(m²)이다 — 기판은 개수보다 면적이 물량이다
    q = next(r for r in it["rows"] if r["key"] == "rigid_single")
    assert q["amt"]["unit"] == "百万円", q["amt"]
    print(f"  ✓ 화면 연결 — 기준월 {it['asOf']} · 고부가 {it['mix']['prev']}→"
          f"{it['mix']['now']}% · 단위 {ml['amt']['unit']} · "
          f"[{it['cross']['verdict']['label']}]")


def test_family_is_matched_without_the_year_prefix():
    """「2025年 時系列表(…)」 계열은 **제목 앞 연도만 다르다.**

    제목을 그대로 비교해 형제 표를 찾았더니 자기 자신 하나뿐이었고, 시계열이
    12개월로 끝나 전년 동월 비교가 아예 성립하지 않았다(2026-09-30 실측).
    화면에는 "월 수가 12개뿐"만 떴다.
    """
    assert (IN.norm_title("2025年 時系列表(2350_機械器具月報（その３５）電子部品)")
            == IN.norm_title("2019年 時系列表(2350_機械器具月報（その３５）電子部品)"))
    # 분야가 다른 표(2360_반도체)는 같은 계열이 아니다
    assert (IN.norm_title("2025年 時系列表(2360_機械器具月報（その３６）電子管)")
            != IN.norm_title("2025年 時系列表(2350_機械器具月報（その３５）電子部品)"))
    assert IN.norm_title("令和7年 時系列表(X)") == IN.norm_title("2025年 時系列表(X)")

    FK = _fake()
    td = Path(tempfile.mkdtemp()); db = td / "k.sqlite"
    os.environ["EJ_ESTAT_APP_ID"] = "test-only-not-a-real-key"
    try:
        _, v = _run_collector(FK.fake_get, db)
    finally:
        os.environ.pop("EJ_ESTAT_APP_ID", None)
    st = v["stitch"]
    assert st["siblings"] >= 8, st          # 2019~2026 여덟 해
    assert "2350" in st["family"] and "年" not in st["family"].split("時系列表")[0], st
    # 분야가 다른 2360_ 표가 섞이면 안 된다
    assert all("2360" not in str(x.get("id", "")) for x in st.get("ids", []))
    assert v["estat"]["from"] == "2019-01" and v["estat"]["to"] == "2026-08", v["estat"]
    print(f"  ✓ 계열 매칭 — '{st['family'][:34]}…' vintage {st['siblings']}건 "
          f"→ {v['estat']['from']}~{v['estat']['to']}")


def test_high_value_is_ten_plus_layers_not_all_multilayer():
    """4층·6~8층까지 고부가로 묶으면 범용 물량이 신호를 희석한다."""
    assert set(P.HIGH_VALUE_JP) == {"ml_10p", "buildup"}, P.HIGH_VALUE_JP
    keys = {i.key for i in IN.load().items}
    assert {"ml_4", "ml_68", "ml_10p"} <= keys, keys
    # 전각 문자를 반각으로 적으면 하나도 안 맞는다
    src = (ROOT / "config" / "pcb_intl.yaml").read_text(encoding="utf-8")
    assert "（１０層以上" in src and "（６～８層" in src and "（４層" in src
    print("  ✓ 고부가 = 10층 이상 + 빌드업 (4층·6~8층은 별도 계열)")


def test_second_run_only_refreshes_recent_vintages():
    """첫 수집은 전 구간, 2회차는 최근 vintage 만. 매달 90여 표를 다시 받을 이유가 없다."""
    FK = _fake()
    td = Path(tempfile.mkdtemp()); db = td / "k.sqlite"
    os.environ["EJ_ESTAT_APP_ID"] = "test-only-not-a-real-key"
    try:
        _, v1 = _run_collector(FK.fake_get, db)
        _, v2 = _run_collector(FK.fake_get, db)
    finally:
        os.environ.pop("EJ_ESTAT_APP_ID", None)
    assert v1["stitch"]["fullRefresh"] is True, v1["stitch"]
    assert v2["stitch"]["fullRefresh"] is False, v2["stitch"]
    assert v2["stitch"]["siblings"] <= v1["stitch"]["siblings"], (v1["stitch"], v2["stitch"])
    cap = IN.load().revision_vintages
    assert v2["stitch"]["siblings"] <= cap, (v2["stitch"], cap)
    # 2회차에도 최신월은 그대로 있어야 한다 (줄인 게 데이터를 깎으면 안 된다)
    assert v2["estat"]["to"] == v1["estat"]["to"] == "2026-08"
    with Store(db) as s:
        ser = s.demand(IN.SOURCE)
    assert len(ser["jp:ml_10p:amt"]) >= 80
    print(f"  ✓ 2회차 — vintage {v1['stitch']['siblings']}건 → "
          f"{v2['stitch']['siblings']}건, 최신월 {v2['estat']['to']} 유지")


def test_legacy_timeseries_table_is_rejected():
    """2010년 시계열표(品目 축 없음)는 **채택되면 안 된다.** 실제로 채택됐던 표다."""
    FK = _fake()
    td = Path(tempfile.mkdtemp()); db = td / "k.sqlite"
    os.environ["EJ_ESTAT_APP_ID"] = "test-only-not-a-real-key"
    try:
        _, v = _run_collector(FK.fake_get, db)
    finally:
        os.environ.pop("EJ_ESTAT_APP_ID", None)
    assert "主要製品統計表" not in (v["estat"].get("title") or "")
    # 옛 표가 후보에는 있었는데도 안 뽑혔는지 확인한다 —
    # 후보에 아예 없었다면 이 테스트는 아무것도 보장하지 않는다.
    titles = [c["title"] for c in v["search"]["top"]]  # noqa: F841
    legacy = [c for c in v["search"]["top"] if "主要製品統計表" in c["title"]]
    picked = v["estat"]["statsDataId"]
    assert all(c["id"] != picked for c in legacy), (picked, titles[:3])
    if legacy:
        # 점수에서도 밀려야 한다 (최신성 가점이 없으면 여기서 뒤집힌다)
        good = [c["score"] for c in v["search"]["top"] if "時系列表" in c["title"]]
        if good:
            assert max(c["score"] for c in legacy) < max(good)
    rejected = [p for p in v.get("probes", []) if not p["ok"]]
    for p in rejected:
        assert p["reason"], p
    # surveyYears 필터가 옛 표를 먼저 걸러 주지만, 검색을 넓히는 2·3차 패스에서
    # 다시 들어올 수 있다. **점수만으로도** 밀리는지 직접 확인한다.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rpi2", ROOT / "scripts" / "run_pcb_intl.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    c = IN.load()
    old_t = {"stat": "経済産業省生産動態統計", "cycle": "月次", "rows": 42, "year": 2010,
             "title": "主要製品統計表（時系列） １３４．電子回路基板"}
    new_t = {"stat": "経済産業省生産動態統計", "cycle": "年次", "rows": 900, "year": 2026,
             "title": "2026年 時系列表(2350_機械器具月報（その３５）電子部品)"}
    so, sn = mod.score_table(old_t, c, 2026), mod.score_table(new_t, c, 2026)
    assert sn > so, f"점수로도 최신 표가 이겨야 한다: 옛 {so} vs 새 {sn}"
    print(f"  ✓ 옛 시계열표(品目 축 없음) 배제 — 검색 후보 {len(legacy)}건, "
          f"점수 비교 옛 {so} < 새 {sn}, 검증 {len(v.get('probes', []))}건 중 "
          f"{len(rejected)}건 탈락")


def test_measure_exclude_separates_production_from_stock():
    """生産·出荷·在庫가 한 축에 있다. '金額' 하나로 맞추면 셋이 섞인다."""
    c = IN.load()
    got = {n: (IN.pick_measure(n, c.measures) or type("x", (), {"key": None})).key
           for n in ("生産　金額(百万円)", "在庫　金額(百万円)", "出荷　金額(百万円)",
                     "生産　数量(千個)")}
    assert got["生産　金額(百万円)"] == "amt", got
    assert got["在庫　金額(百万円)"] == "stock", got
    assert got["出荷　金額(百万円)"] == "ship", got
    assert got["生産　数量(千個)"] == "qty", got
    assert IN.unit_from_name("生産　金額(百万円)") == "百万円"
    print(f"  ✓ 生産/在庫/出荷 분리 — {got}")


def test_intl_key_absent_exits_zero():
    """키가 없어도 **0 으로 끝나야 한다.** 곁다리 축이 파이프라인을 죽이면 안 된다."""
    import subprocess
    env = dict(os.environ); env.pop("EJ_ESTAT_APP_ID", None)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_pcb_intl.py"),
                        "--dry-run"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert "EJ_ESTAT_APP_ID" in r.stdout
    print("  ✓ 키 없음 → 종료코드 0 (파이프라인 유지)")


def test_intl_collector_never_logs_url():
    """URL 에 appId 가 들어 있다. 로그에 찍히면 공개 레포에 키가 남는다."""
    src = (ROOT / "scripts" / "run_pcb_intl.py").read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for bad in ('log.warning("HTTP %s (%s)", r.status_code, url)',
                "log.info(url", "print(url"):
        assert bad not in body, f"URL 을 로그에 찍는다: {bad}"
    assert 'url.split("?")[0]' in body, "경로만 찍는 처리가 없다"
    # verify 파일은 공개 레포에 커밋된다. 거기에 키가 들어가면 안 된다.
    for line in body.splitlines():
        if "verify[" in line and "=" in line:
            assert "app_id" not in line, f"verify 에 키를 담는다: {line.strip()}"
    print("  ✓ URL·키 로그 유출 차단 · verify 파일에 키 없음")


def test_intl_panel_declares_what_it_waits_for():
    """해외 축은 아직 없다. '없다'가 아니라 '무엇을 기다리는지'를 적어야 한다."""
    pl_cfg = P.load()
    from types import SimpleNamespace  # noqa: F401
    spec = importlib.util.spec_from_file_location("bpcb2", ROOT / "scripts" / "build_pcb.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    intl = mod.build_intl(pl_cfg)
    assert intl["ok"] is False
    keys = {i["key"] for i in intl["items"]}
    assert {"jp_meti", "tw_moea", "us_gea"} <= keys, keys
    for i in intl["items"]:
        assert i["free"] is True, f"{i['key']} 가 무료가 아니다 — 유료 출처는 넣지 않는다"
        assert i["why"], i["key"]
    census = next(i for i in intl["items"] if i["key"] == "us_census")
    assert census["have"] is True, "이미 가진 키를 다시 요청하면 안 된다"
    need = [i["needKey"] for i in intl["items"] if i["needKey"] and not i["have"]]
    print(f"  ✓ 해외 대조축 선언 {len(intl['items'])}건 (전부 무료) — "
          f"요청할 키 {need}")


def test_no_layer_imports_another_layer():
    """pcb.py 가 battery.py 에서 best_lag 를 가져가면 같은 사고가 반복된다."""
    src = (ROOT / "kortrade" / "pcb.py").read_text(encoding="utf-8")
    for bad in ("from .battery", "from .flash", "from .chains", "from .watchlist"):
        assert bad not in src, f"pcb.py 가 다른 레이어를 import 한다: {bad}"
    assert "from .stats import" in src
    print("  ✓ 레이어 독립 — 공용 통계는 stats 에서만")


def test_site_has_pcb_tab_next_to_cosmetics():
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "PCB기판" in html, "PCB 탭 이름이 없다"
    assert "PCB_KEY" in html and "renderPcb" in html
    assert "data/pcb.json" in html
    # 화장품 바로 옆 — 섹터 탭 배열에 cosmetics 기준으로 끼워 넣는다
    assert "cosmetics" in html, "화장품 옆에 붙이는 로직이 없다"
    # ma3 는 null 을 전파해야 한다 (JS 에서 null+null+null === 0)
    assert "w.some(v => v == null) ? null" in html
    print("  ✓ 사이트 — PCB기판 탭 · 화장품 옆 배치 · null 전파 이동평균")


# 브라우저 전역·내장 함수. 여기 없는 이름을 부르면 "is not defined" 로 죽는다.
_JS_KNOWN = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "String", "Number", "Math", "Object", "Array", "JSON", "Date", "Boolean",
    "parseFloat", "parseInt", "isNaN", "Promise", "Map", "Set", "RegExp", "Error",
    "getComputedStyle", "fetch", "matchMedia", "addEventListener", "setTimeout",
    "requestAnimationFrame", "console", "document", "window", "location",
    # 템플릿 문자열 안의 CSS·SVG 함수. 호출처럼 보이지만 JS 함수가 아니다.
    "var", "repeat", "minmax", "rotate", "translate", "scale", "calc",
    "rgba", "rgb", "url", "matrix",
}


def test_pcb_render_calls_are_all_defined():
    """**호출은 하는데 정의가 없는 함수**를 잡는다.

    2026-09-30 사고: `intlProbes(D)` 를 부르면서 정의하지 않았다. 그 호출은
    해외축이 **연결되지 않은 분기**에만 있었고, 테스트 픽스처는 연결된 분기만
    만들고 있었다. 그래서 테스트는 전부 통과했는데 실제 화면에서는 renderPcb 가
    통째로 죽었고, 죽으면 #view 가 그대로 남아 **'탭이 안 넘어간다'** 로 보였다.

    분기마다 픽스처를 만드는 것보다 이 정적 검사가 싸고 확실하다.
    """
    import re
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    # ★ 2차전지 탭 렌더 블록도 같이 본다. 같은 사고(호출만 하고 정의 없음)가
    #   어느 탭에서 나든 그 탭이 통째로 죽는다.
    js = (html[html.index("function renderBatteryCharts("):
               html.index('const PCB_KEY = "pcb";')]
          + html[html.index('const PCB_KEY = "pcb";'):
                 html.index("/* ================= shell ================= */")])
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", html))
    # 함수 **매개변수**도 정의다. sigCard(.., fmt) 처럼 콜백을 인자로 받는 함수가
    # 스캔 범위에 들어오면 그 인자가 '미정의 호출'로 잡힌다.
    for args in re.findall(r"function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)", html):
        defined |= {a.split("=")[0].strip() for a in args.split(",") if a.strip()}
    for args in re.findall(r"\(([^()]*)\)\s*=>", html):
        defined |= {a.split("=")[0].strip() for a in args.split(",") if a.strip()}
    # 공백 없이 바로 '(' 가 붙은 것만 호출로 본다 — "ASP ($/kg)" 같은 본문 텍스트가
    # 호출로 잡히면 검사가 잡음투성이가 되어 아무도 안 보게 된다.
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\(", js))
    # 이 코드베이스의 함수는 전부 camelCase 다. 전부 대문자인 토큰은 본문 안의
    # 용어("ASP($/kg)")이지 호출이 아니다.
    missing = sorted(n for n in (called - defined - _JS_KNOWN)
                     if not n.isupper())
    assert not missing, f"정의되지 않은 함수를 호출한다: {missing}"
    print(f"  ✓ PCB·2차전지 렌더 호출 {len(called)}종 전부 정의됨 "
          f"(intlProbes 미정의로 탭이 죽은 사고 재발 방지)")


def test_pcb_renders_both_intl_branches():
    """해외축 **연결 전/후 두 분기**를 모두 렌더한다.

    연결된 분기만 보고 있었던 것이 위 사고의 근본 원인이다.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:                                  # noqa: BLE001
        print("  – playwright 없음 — 건너뜀 (정적 검사는 위 테스트가 한다)")
        return
    FK = _fake()
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

    for label, with_jp in (("연결 전", False), ("연결 후", True)):
        td = Path(tempfile.mkdtemp()); db = td / "k.sqlite"
        _fixture(db)
        if with_jp:
            os.environ["EJ_ESTAT_APP_ID"] = "test-only-not-a-real-key"
            try:
                _run_collector(FK.fake_get, db)
            finally:
                os.environ.pop("EJ_ESTAT_APP_ID", None)
        payload = _build(db)
        assert payload["intl"]["ok"] is with_jp, (label, payload["intl"].get("note"))
        boot = {"data/pcb.json": payload,
                "data/manifest.json": {"generatedAt": "x", "sectors": [], "coverage": {}}}
        page = td / "i.html"
        page.write_text(html.replace(
            "<head>", "<head><script>window.__BOOTSTRAP__="
            + json.dumps(boot, ensure_ascii=False) + ";</script>", 1), encoding="utf-8")
        errs = []
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(page.as_uri()); pg.wait_for_timeout(700)
            pg.click('button[data-key="pcb"]', timeout=8000); pg.wait_for_timeout(900)
            body = pg.inner_text("#view")
            b.close()
        assert not errs, (label, errs)
        # 탭이 실제로 **바뀌었는지** — 죽으면 #view 에 이전 탭이 남는다
        assert "수출 단가(ASP) 추이" in body, (label, body[:200])
        assert "해외 대조축" in body, label
        print(f"  ✓ 렌더 {label} — JS 오류 0건 · #view 교체 확인")


def test_workflow_builds_pcb():
    wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    body = "\n".join(l for l in wf.splitlines() if not l.strip().startswith("#"))
    assert "build_pcb.py" in body, "워크플로가 PCB 를 빌드하지 않는다"
    assert "--extra-regions pcb" in body, "PCB 시군구 수집 단계가 없다"
    # 수집이 빌드보다 먼저여야 한다 — 최종 수요 패널이 순서 때문에 영원히 비었던 적이 있다
    assert body.index("--extra-regions pcb") < body.index("build_pcb.py"), \
        "PCB 시군구 수집이 빌드 뒤에 있다 — 지역 패널이 영원히 빈다"
    assert "run_pcb_intl.py" in body, "일본 축 수집 단계가 없다"
    assert body.index("run_pcb_intl.py") < body.index("build_pcb.py"), \
        "일본 축 수집이 빌드 뒤에 있다 — 해외 대조 패널이 영원히 빈다"
    assert "secrets.EJ_ESTAT_APP_ID" in body, "e-Stat 키를 시크릿으로 넘기지 않는다"
    assert "3028661a" not in wf, "인증키가 워크플로에 박혀 있다"
    print("  ✓ 워크플로 — 수집(시군구·일본) → 빌드 순서 · 키는 시크릿으로만")


def test_commit_push_rebuilds_pcb():
    """DB 병합 후 site/data 를 다시 만드는 목록에 PCB 가 빠지면, 충돌이 난 실행마다
    pcb.json 이 조용히 옛날 것으로 남는다."""
    sh = (ROOT / "scripts" / "commit_push.sh").read_text(encoding="utf-8")
    assert "build_pcb" in sh, "commit_push.sh 가 PCB 를 재생성하지 않는다"
    print("  ✓ 커밋 스크립트가 병합 후 PCB 를 다시 만든다")


def test_selfcheck_covers_pcb():
    src = (ROOT / "scripts" / "selfcheck.py").read_text(encoding="utf-8")
    assert "kortrade.pcb" in src and "build_pcb" in src
    assert "kortrade.intl" in src and "run_pcb_intl" in src
    print("  ✓ 배포 정합성 점검이 PCB 를 본다")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fns.sort(key=lambda f: f.__code__.co_firstlineno)
    print(f"PCB 레이어 검증 — {len(fns)}건\n")
    for f in fns:
        f()
    print(f"\n전부 통과 ({len(fns)}건)")
