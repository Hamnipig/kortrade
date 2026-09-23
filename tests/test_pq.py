"""단가(P/Q) 분해와 확산도 검증. (API 호출 없음)

실행:  python tests/test_pq.py

이 레이어가 왜 있는지는 kortrade/pq.py 머리말에 있다. 한 줄로 줄이면:
**금액만 보면 '밀어내기'와 '프리미엄 확산'이 같은 칸에 들어간다.**
아래 테스트는 2026-08 관세청 실측치를 그대로 넣어 그 구분이 실제로 되는지를 고정한다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kortrade import breadth as BR  # noqa: E402
from kortrade import pq as PQ  # noqa: E402
from kortrade.store import Store  # noqa: E402

# ── 2026년 8월 실측 (출처: 뷰티경제 2026-09-21/22, 관세청 수출입무역통계 기준) ──
#   기초  3304991000  금액 +49.4%  물량 1만3528→1만5659톤(+15.7%)  단가 26.2→33.8 $/kg
#   메이크업 3304992000  6454→7095만달러(+9.9%)  1260.7→1333.3톤(+5.8%)  51.2→53.2 $/kg
#   국가별 기초 단가: 미국 28.2→27.3(하락) · 일본 20.9→29.9(+42.6%, 금액 +92.2%)
BASE = {"usd_prev": 354.8e6, "usd_now": 530.05e6, "wgt_prev": 13_528_000.0,
        "wgt_now": 15_659_000.0}
MAKEUP = {"usd_prev": 64.54e6, "usd_now": 70.95e6, "wgt_prev": 1_260_700.0,
          "wgt_now": 1_333_300.0}


def test_asp_guards():
    """중량이 없거나 너무 작으면 단가를 내지 않는다."""
    assert PQ.asp(1_000_000, 100_000) == 10.0
    assert PQ.asp(1_000_000, 500) is None, "소량 선적에서 단가가 튀는 것을 막아야 한다"
    assert PQ.asp(0, 100_000) is None and PQ.asp(None, 1) is None
    assert PQ.asp(1e6, 0) is None
    print("  ✓ 단가 가드 — 중량 <"
          f"{PQ.MIN_WGT_KG:,.0f}kg 은 계산하지 않음")


def test_decompose_matches_measured():
    """실측 기초화장품 수치를 그대로 재현하는지."""
    d = PQ.decompose(BASE["usd_now"], BASE["usd_prev"], BASE["wgt_now"], BASE["wgt_prev"])
    assert abs(d["valueYoy"] - 49.4) < 0.5, d["valueYoy"]
    assert abs(d["qtyYoy"] - 15.7) < 0.3, d["qtyYoy"]
    assert abs(d["priceYoy"] - 29.0) < 0.5, d["priceYoy"]
    assert abs(d["asp"] - 33.8) < 0.3 and abs(d["aspPrev"] - 26.2) < 0.3, d

    # 로그 분해라 기여도 합이 정확히 100 이어야 한다 (단순 %로 쪼개면 교차항이 남는다)
    assert abs(d["priceShare"] + d["qtyShare"] - 100) < 0.05, d
    # 성장의 과반이 단가에서 나왔다 — 이게 '프리미엄화'의 근거다
    assert d["priceShare"] > 55, d["priceShare"]
    assert d["verdict"]["code"] == "premium_expansion", d["verdict"]

    m = PQ.decompose(MAKEUP["usd_now"], MAKEUP["usd_prev"],
                     MAKEUP["wgt_now"], MAKEUP["wgt_prev"])
    assert abs(m["valueYoy"] - 9.9) < 0.3 and abs(m["qtyYoy"] - 5.8) < 0.3, m
    assert abs(m["priceYoy"] - 3.9) < 0.3, m["priceYoy"]
    # ★ 같은 HS 6단위(330499) 안인데 단가 레벨이 1.6배 다르다. 합치면 둘 다 안 보인다.
    assert m["asp"] / d["asp"] > 1.5, (m["asp"], d["asp"])
    print(f"  ✓ 실측 재현 — 기초 금액 {d['valueYoy']}% = 물량 {d['qtyYoy']}% + 단가 "
          f"{d['priceYoy']}% (단가 기여 {d['priceShare']}%) / 메이크업 단가 ${m['asp']}/kg")


def test_verdict_grid():
    """2x2 판정이 전부 채워져 있고, 보합 구간이 실제로 작동하는지."""
    seen = set()
    for q in (-20.0, 0.0, 20.0):
        for p in (-20.0, 0.0, 20.0):
            v = PQ.verdict(q, p)
            assert v["code"] != "unknown", (q, p)
            assert v["note"], v
            seen.add(v["code"])
    assert len(seen) == 9, seen

    # ★ 핵심 두 칸 — 이 구분이 이 레이어의 존재 이유다
    assert PQ.verdict(+26.2, -3.2)["code"] == "push", "물량↑ 단가↓ 가 밀어내기로 안 잡힌다"
    assert PQ.verdict(+34.8, +42.6)["code"] == "premium_expansion"
    assert PQ.verdict(-10.0, -10.0)["code"] == "contracting"
    assert PQ.verdict(-10.0, +10.0)["code"] == "mix_up"

    # 보합 구간: ±3% 이내는 부호가 있어도 '변화 없음'으로 본다.
    # 이게 없으면 단가 +0.4% 짜리 달에 국면이 뒤집혀 아무 말도 못 하게 된다.
    assert PQ.verdict(1.0, 1.0)["code"] == "flat"
    assert PQ.verdict(0.5, 20.0)["code"] == "price_led"
    assert PQ.verdict(None, 5.0)["code"] == "unknown"
    print(f"  ✓ 판정 격자 9칸 · 보합 구간 ±{PQ.FLAT_PCT}%")


def test_decompose_edge_cases():
    """분모가 없거나 변화가 미미할 때 조용히 틀린 숫자를 만들지 않는지."""
    d = PQ.decompose(100e6, 0, 1e6, 0)
    assert d["valueYoy"] is None and d["verdict"]["code"] == "unknown", d
    # 금액이 거의 안 변하면 기여도 분모가 0 에 가까워 발산한다 → 비운다
    flat = PQ.decompose(100e6, 100.2e6, 2e6, 2.01e6)
    assert flat["priceShare"] is None, flat
    # 중량이 없으면 단가가 없고, 따라서 판정 불가여야 한다 (금액만으로 우기지 않는다)
    now = PQ.decompose(100e6, 80e6, None, None)
    assert now["asp"] is None and now["verdict"]["code"] == "unknown", now
    print("  ✓ 경계 — 기저 없음 / 미미한 변화 / 중량 없음")


def test_breadth_concentration_and_hubs():
    """확산도 — 상위국 비중, 허브 분리, 증가분 기여."""
    # 단위는 **달러**다. breadth 는 MIN_DELTA_USD($1M) 미만 증가분에서는 기여율을
    # 내지 않으므로, 토이 숫자로 쓰면 outsideShare 가 None 이 되어 통과하지 않는다.
    M = 1e6
    prev = {k: v * M for k, v in
            {"CN": 500, "US": 300, "JP": 100, "NL": 20, "VN": 40, "GB": 10}.items()}
    cur = {k: v * M for k, v in
           {"CN": 300, "US": 420, "JP": 180, "NL": 90, "VN": 80, "GB": 45}.items()}
    hubs = {"NL", "HK", "AE", "SG", "PL", "KZ"}

    c = BR.concentration(cur)
    assert abs(c["total"] - 1115 * M) < 1
    assert {t["cc"] for t in c["top"]} == {"US", "CN", "JP"}
    assert 0 < c["topShare"] <= 100 and c["hhi"] > 0

    a = BR.analyze(cur, prev, hubs=hubs, world_total=1500 * M)
    # 대중국이 빠지고 나머지가 크면서 상위 3국 비중이 내려간다 → '확산'
    assert a["all"]["verdict"]["code"] == "spreading", a["all"]
    assert a["all"]["now"]["topShare"] < a["all"]["prev"]["topShare"]
    # 허브를 빼도 국가 목록에서 NL 이 사라져야 한다
    assert all(t["cc"] != "NL" for t in a["exHub"]["now"]["top"])
    assert a["hub"]["codes"] == sorted(hubs)
    assert abs(a["hub"]["usd"] - 90 * M) < 1           # 수집된 허브는 NL 뿐
    assert a["hub"]["yoy"] and a["hub"]["yoy"] > 300   # 20 -> 90
    # 분모가 세계 전체가 아니라는 사실을 숨기지 않는다
    assert a["coverage"] is not None and a["coverage"] < 100

    con = a["contribution"]
    assert abs(con["totalDelta"] - 145 * M) < 1, con["totalDelta"]    # 1115 - 970
    assert con["outsideShare"] is not None
    assert {g["cc"] for g in con["gainers"]} >= {"US", "JP", "NL"}
    assert any(l["cc"] == "CN" for l in con["losers"]), con["losers"]
    print(f"  ✓ 확산도 — 상위3국 {a['all']['prev']['topShare']}%→"
          f"{a['all']['now']['topShare']}% [{a['all']['verdict']['label']}], "
          f"허브 제외 {a['exHub']['now']['topShare']}%")


def test_first_cross_needs_two_months():
    """단발 대량 선적을 '신규 진입'으로 읽지 않는지."""
    spike = {"2026-01": 0, "2026-02": 50, "2026-03": 0, "2026-04": 0}
    real = {"2026-01": 0, "2026-02": 30, "2026-03": 40, "2026-04": 45}
    assert BR.first_cross(spike, 10) is None, "한 달 튄 것을 진입으로 읽으면 안 된다"
    assert BR.first_cross(real, 10) == "2026-02"
    print("  ✓ 신규 진입 — 연속 2개월 요건")


# ══════════════════════════════════════════════════════════════════════════════
# 엔드투엔드 — DB 에서 사이트 JSON 까지
# ══════════════════════════════════════════════════════════════════════════════

MONTHS = [f"{y}-{m:02d}" for y in (2024, 2025, 2026) for m in range(1, 13)][:32]  # ~2026-08

# 국가별 (금액배수, 단가배수). 물량배수는 금액/단가로 따라온다.
#   미국  금액 +22.2% · 단가 28.2→27.3 (-3.2%)  → 물량 +26.2% → **밀어내기**
#   일본  금액 +92.2% · 단가 +42.6%             → 물량 +34.8% → **프리미엄 확산**
CTY = {
    "US": (1.222, 0.9681), "JP": (1.922, 1.426), "CN": (0.90, 1.05),
    "VN": (1.35, 1.10), "GB": (2.51, 1.20), "FR": (1.30, 1.05),
    "HK": (1.15, 1.02), "SG": (1.20, 1.08), "AE": (1.45, 1.25),
    "NL": (3.20, 1.30), "PL": (1.73, 1.15), "KZ": (1.55, 1.12),
}
CTY_BASE = {"US": 900.0, "JP": 420.0, "CN": 760.0, "VN": 190.0, "GB": 70.0,
            "FR": 60.0, "HK": 300.0, "SG": 120.0, "AE": 95.0, "NL": 45.0,
            "PL": 80.0, "KZ": 55.0}

CUR, PREV = MONTHS[-8:], [f"2025-{m:02d}" for m in range(1, 9)]


def _seed_nation(st: Store) -> None:
    """실측 배수를 그대로 심는다. 비교창(2026-01~08) vs 전년 동기만 맞추면 된다."""
    rows = []

    def add(period, code, cc, usd, wgt):
        rows.append(dict(period=period, hs_code=code, hs6=code[:6],
                         hs_name={"3304991000": "기초화장용 제품류",
                                  "3304992000": "메이크업용 제품류"}.get(code, code),
                         country_code=cc, country_name=cc, exp_usd=int(usd),
                         exp_wgt=int(wgt), imp_usd=0, imp_wgt=0, bal_usd=0))

    for i, p in enumerate(MONTHS):
        cur_win = p in CUR
        prev_win = p in PREV
        # 전국 합계(ALL) — 카테고리/HSK10 분해가 이걸 쓴다
        for code, spec in (("3304991000", BASE), ("3304992000", MAKEUP),
                           ("3304999000", {"usd_prev": 20e6, "usd_now": 21e6,
                                           "wgt_prev": 600_000.0, "wgt_now": 640_000.0})):
            u = spec["usd_now"] if cur_win else spec["usd_prev"]
            w = spec["wgt_now"] if cur_win else spec["wgt_prev"]
            if not (cur_win or prev_win):        # 창 밖은 대충 채워 추이만 만든다
                u, w = spec["usd_prev"] * (0.9 + 0.01 * i), spec["wgt_prev"] * 0.95
            add(p, code, "ALL", u, w)
        # 다른 카테고리도 하나쯤 있어야 화면 표가 비지 않는다
        add(p, "330410", "ALL", 45e6 * (1.1 if cur_win else 1.0), 1_100_000.0)

        # 국가별
        for cc, base in CTY_BASE.items():
            fu, fp = CTY[cc]
            u = base * 1e6 * (fu if cur_win else 1.0)
            price = 30.0 * (fp if cur_win else 1.0)
            add(p, "3304991000", cc, u * 0.8, u * 0.8 / price)
            add(p, "3304992000", cc, u * 0.2, u * 0.2 / (price * 1.6))
    st.upsert_sector(rows)


def _seed_region(st: Store) -> None:
    """시군구 레이어 — build_sector 가 없으면 섹터 자체가 안 만들어진다."""
    st.save_sido_codes({"11": "서울특별시", "41": "경기도"}, "1900-01")
    rows = []
    for p in MONTHS:
        f = 1.3 if p in CUR else 1.0
        for hs in ("330499", "330410", "330420", "330430", "330491",
                   "330510", "330590", "330300"):
            base = 900e6 if hs == "330499" else 40e6
            for sido, sgg, w in (("서울특별시", "강남구", .6), ("경기도", "화성시", .4)):
                rows.append(dict(period=p, hs_code=hs, hs_name="x", sido_cd="11",
                                 sido_name=sido, sigungu_name=sgg, exp_cnt=100,
                                 exp_usd=int(base * w * f), imp_cnt=0, imp_usd=0,
                                 bal_usd=0))
    st.upsert_region(rows)


def test_build_site_nation_block():
    """빌드 산출물에 단가·확산도가 실려 나오는지, 그리고 판정이 실측과 맞는지."""
    with tempfile.TemporaryDirectory() as td:
        db, out = Path(td) / "n.sqlite", Path(td) / "data"
        st = Store(db)
        _seed_region(st)
        _seed_nation(st)
        st.close()
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_site.py"),
             "--db", str(db), "--out", str(out), "--sector", "cosmetics"],
            capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stdout + r.stderr
        d = json.loads((out / "cosmetics.json").read_text(encoding="utf-8"))

    N = d["nation"]
    assert N, "국가 축(단가·확산도)이 페이로드에 없다"
    assert N["asOf"] == "2026-08"

    # ── HSK 10단위 분해 ──────────────────────────────────────────────
    # 330499 안에 기초와 메이크업이 같이 들어 있고 단가가 1.6배 다르다.
    dom = next(c for c in N["cats"] if c["hs"] == "330499")
    kids = {k["code"]: k for k in dom["hs10"]}
    assert {"3304991000", "3304992000"} <= set(kids), kids.keys()
    base, mk = kids["3304991000"], kids["3304992000"]
    assert abs(base["valueYoy"] - 49.4) < 1.0, base["valueYoy"]
    assert abs(base["priceYoy"] - 29.0) < 1.0, base["priceYoy"]
    assert abs(mk["priceYoy"] - 3.9) < 1.0, mk["priceYoy"]
    assert mk["asp"] / base["asp"] > 1.5, "10단위 단가 차이가 뭉개졌다"
    assert base["verdict"]["code"] == "premium_expansion", base["verdict"]
    assert base["name"] and "제품류" in base["name"], base["name"]
    assert sum(k["share"] for k in dom["hs10"]) > 95, dom["hs10"]

    # ── 국가별 국면: 이 기능의 존재 이유 ──────────────────────────────
    cty = {c["cc"]: c for c in N["countries"]}
    assert cty["US"]["verdict"]["code"] == "push", \
        f"미국이 밀어내기로 안 잡힌다: {cty['US']}"
    assert cty["JP"]["verdict"]["code"] == "premium_expansion", cty["JP"]
    assert cty["US"]["priceYoy"] < 0 < cty["US"]["qtyYoy"], cty["US"]
    assert cty["JP"]["priceYoy"] > 30, cty["JP"]
    # 금액 증가율만 보면 둘 다 '성장'이라 같은 칸에 들어간다는 것을 못박는다
    assert cty["US"]["valueYoy"] > 0 and cty["JP"]["valueYoy"] > 0

    # ── 허브 ────────────────────────────────────────────────────────
    assert cty["NL"]["hub"] is True and cty["NL"]["why"], cty["NL"]
    assert cty["US"]["hub"] is False
    B = N["breadth"]
    assert set(B["hub"]["codes"]) == {"HK", "SG", "AE", "NL", "PL", "KZ"}
    assert B["all"]["now"]["topShare"] is not None
    assert B["exHub"]["now"]["topShare"] is not None
    # 허브를 빼면 상위 3국 구성이 달라질 수 있어야 한다 (안 갈리면 지표가 무의미)
    assert all(t["cc"] not in B["hub"]["codes"] for t in B["exHub"]["now"]["top"])
    assert B["coverage"] is not None, "수집국 커버리지를 안 내면 비중이 과대해 보인다"

    # ── 섹터 전체 ───────────────────────────────────────────────────
    t = N["total"]
    assert t["asp"] and t["priceShare"] is not None
    assert abs(t["priceShare"] + t["qtyShare"] - 100) < 0.05, t
    print(f"  ✓ 빌드 — 기초 단가 ${base['asp']}/kg({base['priceYoy']:+}%) vs "
          f"메이크업 ${mk['asp']}/kg({mk['priceYoy']:+}%) · "
          f"미국 [{cty['US']['verdict']['label']}] / 일본 [{cty['JP']['verdict']['label']}] · "
          f"상위3국 {B['all']['now']['topShare']}% (허브제외 {B['exHub']['now']['topShare']}%)")


def test_site_renders_nation():
    """화면이 이 블록을 실제로 그리는지 + 지역 귀속 설명이 정정됐는지."""
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "renderNation" in html and 'id="nation"' in html
    assert "drawNationQuad" in html and 'id="nquad"' in html
    for s in ["단가 국면 지도", "프리미엄 확산", "밀어내기", "카테고리 × HSK 10단위",
              "국가별 단가", "확산도", "중계"]:
        assert s in html, s
    # ★ 지역 귀속 기준 정정 (2026-09-23). 1차 출처는 관세청 API 명세와 K-stat 통계가이드.
    #   제도상 기준은 '제조자 사업장'이 맞고, 틀어지는 원인은 **자기신고 품질**이다.
    #   "신고인 소재지 기준"이라는 이전 설명은 기준 자체를 잘못 본 것이었다.
    assert "제조자 사업장" in html, "지역 귀속 기준이 정정되지 않았다"
    assert "수출자가 직접 적" in html, "자기신고 항목이라는 설명이 없다"
    assert "수출 주체(신고인) 소재지에 가깝게" not in html, \
        "틀린 설명(신고인 기준)이 남아 있다"
    # 단가는 국가 축에서만 나온다는 사실이 코드 주석에 남아 있어야 한다
    src = (ROOT / "kortrade" / "pq.py").read_text(encoding="utf-8")
    assert "중량 필드가 아예 없" in src
    print("  ✓ 화면 연결 + 귀속 기준 설명 정정")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\n단가(P/Q)·확산도 검증 — {len(tests)}개\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {exc}")
            import traceback
            traceback.print_exc()
    print(f"\n{'실패 ' + str(failed) if failed else '전부 통과'} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
