"""섹터 설정 + 사이트 빌드 검증. (API 호출 없음)

실행:  python tests/test_sectors_site.py
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kortrade.sectors import get_sector, load_sectors, validate_all  # noqa: E402
from kortrade.store import Store  # noqa: E402


def test_sector_configs():
    """섹터 설정은 HS 6단위여야 하고, 미검증 섹터는 draft 로 잠겨 있어야 한다."""
    errs = validate_all()
    assert errs == [], errs
    secs = load_sectors()
    keys = {s.key for s in secs}
    assert {"cosmetics", "semiconductor", "battery"} <= keys, keys

    cos = get_sector("cosmetics")
    assert cos.active and cos.dominant == "330499"
    assert cos.label("330410") == "입술" and cos.group("330410") == "색조"
    assert all(len(h) == 6 and h.isdigit() for h in cos.codes)

    # 미검증 섹터가 실수로 active 가 되면 빈 데이터나 엉뚱한 품목이 조용히 쌓인다
    for k in ("semiconductor", "battery"):
        assert get_sector(k).status == "draft", f"{k} 는 코드 검증 전까지 draft 여야 한다"
    assert [s.key for s in load_sectors(include_draft=False)] == ["cosmetics"]
    print(f"  ✓ 섹터 설정 {len(secs)}개 / active {len(load_sectors(include_draft=False))}개")


def test_sector_validation_catches_bad_config():
    """잘못된 섹터 YAML 은 빌드 전에 걸려야 한다."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "bad.yaml").write_text(
            "key: bad\nname: 불량\ndominant: '999999'\n"
            "categories:\n  '3304991000': {label: 십단위}\n",
            encoding="utf-8")
        errs = validate_all(d)
        assert any("6단위" in e for e in errs), errs          # 10단위 코드
        assert any("dominant" in e for e in errs), errs       # categories 에 없는 dominant
    print("  ✓ 불량 섹터 설정 사전 차단")


def _seed(db: Path) -> None:
    st = Store(db)
    st.save_sido_codes({"11": "서울특별시", "51": "강원특별자치도", "41": "경기도"}, "1900-01")
    random.seed(11)
    codes = {"330499": 1000, "330410": 60, "330420": 25, "330430": 4,
             "330491": 7, "330510": 22, "330590": 35, "330300": 9}
    places = [("서울특별시", "강남구", .34), ("경기도", "화성시", .22),
              ("강원특별자치도", "강릉시", .14), ("서울특별시", "송파구", .30)]
    rows = []
    # 카테고리마다 성장 '속도'를 달리 준다. 배수만 다르고 증가율이 같으면 비중변화가 0이 된다.
    rate = {"330499": 0.020, "330410": -0.004, "330420": 0.004, "330430": 0.002,
            "330491": 0.001, "330510": 0.010, "330590": 0.012, "330300": 0.030}
    for i, p in enumerate(pd.period_range("2024-01", "2026-08", freq="M").astype(str)):
        for hs, base in codes.items():
            tr = max(0.05, 1 + rate[hs] * i)
            for sido, sgg, w in places:
                rows.append(dict(period=p, hs_code=hs, hs_name="x", sido_cd="11",
                                 sido_name=sido, sigungu_name=sgg, exp_cnt=30,
                                 exp_usd=int(base * 1e6 * w * tr * (1 + random.uniform(-.1, .1))),
                                 imp_cnt=0, imp_usd=0, bal_usd=0))
    st.upsert_region(rows)
    st.close()


def test_build_site_payload():
    """DB → 사이트 JSON. 카테고리 합계가 totals 와 맞아야 한다."""
    with tempfile.TemporaryDirectory() as td:
        db, out = Path(td) / "t.sqlite", Path(td) / "data"
        _seed(db)
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_site.py"),
             "--db", str(db), "--out", str(out)],
            capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stdout + r.stderr

        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        by = {s["key"]: s for s in man["sectors"]}
        assert by["cosmetics"]["ready"] is True
        # draft 섹터는 수집도 빌드도 되지 않아야 한다
        assert by["semiconductor"]["ready"] is False
        assert not (out / "semiconductor.json").exists()

        d = json.loads((out / "cosmetics.json").read_text(encoding="utf-8"))
        assert d["asOf"] == "2026-08" and len(d["months"]) == 32
        assert len(d["cats"]) == 8 and d["dominant"] == "330499"

        tot = round(sum(c["ytd"] for c in d["cats"]), 1)
        assert abs(tot - d["totals"]["ytd"]) <= 1.0, (tot, d["totals"]["ytd"])

        shares = sum(c["share"] for c in d["cats"])
        assert abs(shares - 100) < 0.5, shares

        dom = next(c for c in d["cats"] if c["hs"] == "330499")
        assert dom["share"] > 80 and d["split"] is not None
        assert len(d["split"]["domIdx"]) == 32 and d["split"]["domIdx"][0] == 100.0

        lip = next(c for c in d["cats"] if c["hs"] == "330410")
        assert lip["share_chg"] < dom["share_chg"], "축소 카테고리가 지배 카테고리보다 비중을 잃어야 한다"
        assert all(p["ytd"] >= 1.0 for c in d["cats"] for p in c["places"])
        print(f"  ✓ 사이트 빌드 — 카테고리 {len(d['cats'])}개, 합계 정합 "
              f"(${tot:,.0f}M), 비중 합 {shares:.1f}%")


def test_site_html_contract():
    """사이트 HTML 이 빌드 산출물과 같은 파일명을 보고 있는지."""
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "data/manifest.json" in html
    assert "data/${key}.json" in html
    assert "<!doctype html>" in html.lower()          # Pages 는 완전한 문서를 받는다
    assert 'name="viewport"' in html
    # 전 카테고리 비중변화가 0 근처일 때 축을 거기 맞추면 노이즈를 로테이션으로 오독한다
    assert "Y_FLOOR" in html
    for key in ("cosmetics", "semiconductor", "battery"):
        pass                                          # 탭은 manifest 에서 동적 생성
    wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    assert "secrets.DATA_GO_KR_SERVICE_KEY" in wf
    assert "DATA_GO_KR_SERVICE_KEY:" in wf and "3028661a" not in wf, "인증키가 하드코딩되면 안 된다"
    assert "--sectors all" in wf and "build_site.py" in wf
    print("  ✓ 사이트/워크플로 계약 (키 하드코딩 없음)")




def test_watchlist_config_and_build():
    """워치리스트 설정 검증 + P·Q 분해가 실제로 갈리는지."""
    import tempfile, random
    from pathlib import Path
    from kortrade import watchlist as W
    from kortrade.store import Store

    wl = W.load()
    errs = wl.validate()
    assert not errs, errs
    assert wl.active(), "active 품목이 하나도 없다"
    # 실측으로 확정한 코드들이 되돌아가지 않도록 못박는다 (전부 회귀 이력이 있다)
    fixed = {
        "cathode_ncm":  "2841909020",   # 9010(코발트산리튬 $7M) 아님 — NCM $2,650M
        "die_bonder":   "8486402010",   # 8486.20 아님 — 호 자체가 다름
        "frozen_snack": "1905901090",   # 1040(비스킷·쿠키) 아님 — 분석47260-0388
        "kpop":         "8523491040",   # 8523.49.1060 은 존재하지 않음
        "cell_ess":     "8507603000",   # EV용(2000)과 반드시 분리
        "bev":          "8703801000",   # 중고차(2000) 제외
    }
    for k, code in fixed.items():
        it = next((i for i in wl.items if i.key == k), None)
        assert it, f"품목 '{k}' 가 사라졌다"
        assert code in it.hsk, f"{k}: {code} 가 빠졌다 (현재 {it.hsk})"
    assert "2841909010" not in wl.items[0].hsk, "코발트산리튬으로 되돌아갔다"
    assert all("2841909010" not in i.hsk for i in wl.items), "코발트산리튬($7M)으로 되돌아갔다"

    # ── 합산 원칙: 사이클이 다른 코드를 한 항목에 합치지 않았는지 (2026-09-18 감사) ──
    # 실측 월별 증감률 상관이 0.4 미만이라 분리를 확정한 짝들. 되돌리면 큰 쪽이 작은 쪽을 덮는다.
    must_split = [
        ("2841909020", "2841909030", "양극재 NCM/NCA (r=-0.06)"),
        ("8710001000", "8710009000", "전차 본체/부분품 (r=0.13)"),
        ("8710001000", "8710002000", "전차/장갑차 (r=0.25)"),
        ("2103909090", "2103909030", "소스/혼합조미료 (r=0.02)"),
        ("8701922000", "8701942000", "트랙터 소형/대형 (r=0.31)"),
        ("8507602000", "8507603000", "2차전지 EV/ESS (r=-0.04)"),
        ("8507603000", "8507609000", "2차전지 ESS/기타"),
        ("8542323000", "8542324000", "MCP/MCO (r=0.52)"),
        ("9018908110", "9018909000", "미용기기 장비/부품"),
        ("3304991000", "3304999000", "기초화장품/잔여 (성장률 +29% vs +52%)"),
    ]
    for a, b, why in must_split:
        for it in wl.items:
            assert not (a in it.hsk and b in it.hsk), f"{it.key}: {why} 를 다시 합쳤다"

    # 감사에서 발견해 추가한 항목들이 사라지지 않았는지
    # 8523 호 안에서 SSD(8523.51)와 음반(8523.49)은 93배 차이나는 별개 산업이다
    ssd = next(i for i in wl.items if i.key == "ssd_nand")
    kp = next(i for i in wl.items if i.key == "kpop")
    assert ssd.hsk[0].startswith("852351"), ssd.hsk
    assert kp.hsk[0].startswith("852349"), kp.hsk
    assert not set(ssd.hsk) & set(kp.hsk), "SSD와 음반이 같은 코드를 쓴다"
    lp = next(i for i in wl.items if i.key == "laser_pcb")
    assert lp.hsk == ["8456111000"], lp.hsk
    assert "8456119000" not in lp.hsk, "방향이 반대인 8456119000을 합쳤다"

    for k in ("cell_other", "skincare_other", "aesthetic_parts", "armor_apc", "cathode_nca",
              "ssd_nand", "laser_pcb"):
        assert any(i.key == k and i.active for i in wl.items), f"감사 산출물 '{k}' 가 빠졌다"
    # 검증 안 된 코드가 조용히 active 로 올라가는 것을 막는다
    for it in wl.active():
        assert it.evidence.strip(), f"{it.key}: 근거 없이 active"
        assert all(len(c) == 10 for c in it.hsk), it.key

    # ---- P·Q 분해: 금액은 같은데 원인이 다른 두 계열을 구분해야 한다 ----
    # A: 물량 2배, 단가 절반   → 금액 동일
    # B: 물량 동일, 단가 2배   → 금액 2배
    sig_a = W.signals(cur_usd=100e6, prev_usd=100e6, cur_kg=200_000, prev_kg=100_000,
                      q3_usd=40e6, q3p_usd=40e6, q3_kg=80_000, q3p_kg=40_000)
    sig_b = W.signals(cur_usd=200e6, prev_usd=100e6, cur_kg=100_000, prev_kg=100_000,
                      q3_usd=80e6, q3p_usd=40e6, q3_kg=40_000, q3p_kg=40_000)
    assert sig_a["yoy"] == 0 and sig_a["qYoy"] == 100 and sig_a["pYoy"] == -50, sig_a
    assert sig_b["yoy"] == 100 and sig_b["qYoy"] == 0 and sig_b["pYoy"] == 100, sig_b

    # 기저가 거의 없는 계열은 % 를 만들지 않는다
    tiny = W.signals(50e6, 1e6, 200, 20, 20e6, 0.4e6, 80, 8)
    assert tiny["yoy"] is None, tiny          # 분모 $1M < MIN_BASE_USD
    assert tiny["p"] is None, tiny            # 중량 200kg < MIN_BASE_KG

    # 반대로 '$/kg 가 크다'는 것 자체는 오류가 아니다 — D램이 실제로 $67,700/kg 다.
    dram = W.signals(80699e6, 60000e6, 1192e3, 1000e3, 30000e6, 24000e6, 450e3, 400e3)
    assert dram["p"] and 60_000 < dram["p"] < 75_000, dram["p"]

    # 물량 턴어라운드 판정: 8M 은 마이너스, 최근 3M 은 플러스
    turn = W.signals(100e6, 110e6, 90_000, 100_000, 40e6, 36e6, 38_000, 34_000)
    assert turn["qTurn"] is True, turn

    # ---- 실제 빌더가 도는지 (합성 데이터) ----
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bw", Path(__file__).resolve().parent.parent / "scripts" / "build_watchlist.py")
    bw = importlib.util.module_from_spec(spec); spec.loader.exec_module(bw)

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "w.sqlite")
        periods = [f"{y}-{m:02d}" for y in (2024, 2025, 2026) for m in range(1, 13)][:32]
        recs = []
        rnd = random.Random(7)
        for it in wl.active():
            for i, p in enumerate(periods):
                for code in it.hsk:
                    usd = int(5e6 * (1 + 0.02 * i) * (1 + rnd.uniform(-.05, .05)))
                    kg = int(usd / (40 + i))          # 단가가 서서히 오르는 계열
                    for cc in ["ALL"] + it.countries[:3]:
                        f = 1.0 if cc == "ALL" else 0.25
                        recs.append(dict(period=p, hs_code=code, hs6=code[:6],
                                         hs_name=it.name, country_code=cc,
                                         country_name=cc, exp_usd=int(usd * f),
                                         exp_wgt=int(kg * f), imp_usd=0, imp_wgt=0,
                                         bal_usd=0))
        st.upsert_sector(recs)
        payload = bw.build(st, wl)
        st.close()

    assert payload, "빌더가 None 을 돌려줬다"
    ready = [r for r in payload["items"] if r.get("ready")]
    assert len(ready) == len(wl.active()), (len(ready), len(wl.active()))
    # draft 품목은 ready=False 로 남아 화면에 '미검증'으로 나와야 한다
    drafts = [r for r in payload["items"] if not r.get("ready")]
    assert {r["key"] for r in drafts} == {i.key for i in wl.items if not i.active}
    # 국가 합이 전국 합계를 넘으면 이중계상이다
    for r in ready:
        csum = sum(c["usd"] for c in r["countries"])
        assert csum <= r["usd"] * 1.001, (r["key"], csum, r["usd"])
    assert payload["highlights"]["accel"] is not None
    print(f"  ✓ 워치리스트 — active {len(ready)} / draft {len(drafts)}, P·Q 분해 검증")


def test_site_contract_scanner():
    """스캐너가 섹터와 **독립**으로 뜨는지, HSK 이스케이프가 깨지지 않는지."""
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    # manifest 실패가 스캐너까지 죽이면 안 된다
    assert "Promise.allSettled" in html, "manifest/워치리스트가 여전히 직렬로 묶여 있다"
    assert 'MANIFEST = mr.status === "fulfilled"' in html
    # HSK 는 esc() 로 감싼 뒤 <br> 로 이어야 한다 (반대로 하면 <br> 이 글자로 보인다)
    assert 'r.hsk.map(esc).join("<br>")' in html, "HSK 줄바꿈이 문자로 출력된다"
    assert 'esc(r.hsk.join("<br>"))' not in html
    # 워크플로가 워치리스트를 갱신하는지 — 안 그러면 첫 수집 이후 영원히 멈춘다
    wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    assert "run_watchlist.py" in wf and "build_watchlist.py" in wf, "자동 갱신에 워치리스트가 빠졌다"
    # 사분면 축 라벨이 실제로 있어야 한다 (P·Q 해석의 전부)
    for q in ["수요 확장", "점유율 경쟁", "믹스 개선", "위축"]:
        assert q in html, q
    print("  ✓ 스캐너 계약 — 독립 로딩 / HSK 렌더 / 자동 갱신 연결")


def test_base_effect_flag_and_sorting():
    """가속 착시 방어 — 기저지수 산출과 표 정렬 기능."""
    from kortrade import watchlist as W

    # 전년 동기 3개월이 평년의 1/4 로 꺼졌던 계열 → 기저지수가 낮게 나와야 한다
    normal = [100.0] * 12
    dipped = [100.0] * 9 + [25.0] * 3          # 마지막 3개월만 급감
    q3p, yr = [9, 10, 11], list(range(12))
    assert W.base_index(normal, q3p, yr) == 1.0
    bi = W.base_index(dipped, q3p, yr)
    assert bi is not None and bi < 0.75, bi     # 화면에서 '기저↓' 로 표시되는 구간
    assert W.base_index([1.0, 2.0], [0], [0, 1]) is None, "표본이 적으면 계산하지 않는다"

    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    # 머리글 클릭 정렬 (YoY 포함 모든 수치 열)
    assert 'th class="${c.l?' in html or "sortable" in html
    assert 'data-col="${c.k}"' in html, "정렬 가능한 머리글이 없다"
    assert '{k:"yoy"' in html, "YoY 열로 정렬할 수 없다"
    assert "uniDir = -uniDir" in html, "같은 열 재클릭 시 역순 전환이 없다"
    # 기저 경고 배지와 필터
    assert "기저↓" in html and "baseIdx" in html
    assert 'clean:' in html and "기저 정상만" in html
    print("  ✓ 기저효과 플래그 + 열 정렬 (가속 착시 방어)")


def test_universe_layer():
    """유니버스 레이어 — 4단위 집계, 워치리스트와 지표 정의 일치, 화면 연결."""
    import tempfile, random, importlib.util, yaml as _y
    from pathlib import Path
    from kortrade.store import Store
    from kortrade import watchlist as W

    chs = _y.safe_load((ROOT / "config" / "hs_chapters.yaml").read_text(encoding="utf-8"))["chapters"]
    chs = {str(k).zfill(2): v for k, v in chs.items()}
    assert len(chs) == 97, f"장이 97개가 아니다 ({len(chs)})"
    assert chs["85"] and chs["33"] and chs["87"], "핵심 장 이름 누락"

    spec = importlib.util.spec_from_file_location("bu", ROOT / "scripts" / "build_universe.py")
    bu = importlib.util.module_from_spec(spec); spec.loader.exec_module(bu)

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "u.sqlite")
        periods = [f"{y}-{m:02d}" for y in (2024, 2025, 2026) for m in range(1, 13)][:32]
        rnd = random.Random(2)
        recs = []
        for c in ["28", "33", "85", "87"]:
            for n in range(1, 4):
                h = f"{c}{n:02d}"
                for i, p_ in enumerate(periods):
                    kg = 4e5 * (1.01 ** i) * (1 + rnd.uniform(-.04, .04))
                    recs.append(dict(period=p_, hs4=h, hs2=c, top_name=f"품목{h}",
                                     exp_usd=int(kg * 30), exp_wgt=int(kg), imp_usd=0))
        st.upsert_universe(recs)
        pl = bu.build(st)
        st.close()

    assert pl, "빌더가 None"
    assert all(len(r["hs4"]) == 4 for r in pl["items"]), "4단위가 아닌 항이 섞였다"
    assert all(r["hs2"] == r["hs4"][:2] for r in pl["items"])
    # 장 롤업 합계가 항 합계를 넘으면 이중계상
    ci = sum(r["usd"] for r in pl["items"])
    cc = sum(r["usd"] for r in pl["chapters"])
    assert abs(ci - cc) / max(cc, 1) < 0.02, (ci, cc)
    # 지표 정의를 워치리스트와 공유해야 두 화면 숫자가 어긋나지 않는다
    src = (ROOT / "scripts" / "build_universe.py").read_text(encoding="utf-8")
    assert "W.signals(" in src, "유니버스가 별도 지표 계산을 쓰고 있다 — 정의가 갈린다"

    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "data/universe.json" in html and "renderUniverse" in html
    # 기본 화면은 가장 최신인 속보. 속보가 없으면 전체 유니버스로 떨어져야 한다
    # ("먼저 전산업을 보고 특정 산업으로" — 워치리스트로 먼저 떨어지면 안 된다).
    assert 'let scanPane = "flash"' in html, "기본 화면이 속보가 아니다"
    assert 'if (!FLASH) scanPane = UNI ? "uni"' in html, \
        "속보가 없을 때 전체 유니버스로 떨어지지 않는다"
    wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    assert "run_universe.py" in wf and "build_universe.py" in wf, "자동 갱신에 유니버스가 빠졌다"
    assert "DB 크기 점검" in wf, "100MB 한도 경고가 없다"
    print(f"  ✓ 유니버스 — 장 97개, 항 {len(pl['items'])}개, 롤업 정합, 지표 공유")


def test_value_chain_localization():
    """해외 현지생산 보정 — 실측 對미 2차전지 수치로 판정 로직을 고정한다.

    이 프로젝트의 가장 큰 오독 위험: 완제품 수출이 줄었다는 이유만으로
    수요 감소라 판단하는 것. 실제로는 미국 현지 조립 전환이었다.
    """
    import tempfile, importlib.util
    from pathlib import Path
    from kortrade import chains as C
    from kortrade.store import Store

    cs = C.load()
    errs = cs.validate()
    assert not errs, errs
    assert {c.key for c in cs.chains} >= {"battery", "auto"}

    # 장비는 수요 합계에서 빠져야 한다 (설비투자 흐름이라 섞으면 둘 다 흐려진다)
    assert "equipment" not in C.DEMAND_STAGES
    assert set(C.DEMAND_STAGES) == {"final", "component", "material"}

    # 판정 로직 — 완제품↓ + 체인↑ 이면 반드시 '현지화'
    assert C.verdict(-13.0, 12.9, 1.56, 0.70)["code"] == "localizing"
    assert C.verdict(-13.0, -8.0, 1.56, 0.70)["code"] == "mixed"      # 체인도 감소 + 현지화 급등
    assert C.verdict(-13.0, -8.0, 0.72, 0.70)["code"] == "contracting"  # 순수 위축
    assert C.verdict(20.0, 25.0, 0.72, 0.70)["code"] == "expanding"
    assert C.verdict(None, 5.0, 1.0, 1.0)["code"] == "unknown"
    assert C.localization(0, 100) is None

    # 실측 對미 월별을 그대로 넣어 결과를 고정한다 (2025-01~2026-08)
    CELL = {
        "8507603000": [151.4,149.4,154.3,101.8,122.9,183.2,165.9,148.3,156.3,127.5,261.2,245.1,
                       114,153.5,235.3,116.8,181.1,88,81.3,54.2],
        "8507602000": [16.7,20.6,21.9,128.4,20,17.5,24.4,26.3,33.5,21.1,20,15.6,
                       18,36.6,102.3,29.5,13.2,19,11.8,20.9],
        "8507609000": [9.3,22.5,13.7,23.8,30.1,29.6,29,19.1,20.1,10.7,9.6,22.3,
                       5.1,51.5,34.8,44.9,50.2,56.8,80.1,68.3]}
    UP = {
        "8507909000": [33.3,42.2,40.7,32,22.3,18.5,24.3,14.1,25.2,27.2,43.2,52.6,
                       52.7,45.3,64.8,73.8,83.1,111.2,104.2,127.3],
        "2841909020": [35.1,56.7,76,85.3,104.7,116.4,153.8,74.7,81.4,28.7,44.7,67.6,
                       19.2,24.8,52.3,64.2,64.7,87.1,120.9,113.1],
        "2841909030": [45.4,31.6,20.5,8.7,0,4.9,19.5,15.2,21.7,16.8,11.6,12.2,
                       1.4,20.2,27.8,27.8,23.3,18.6,37.1,17.6],
        "3801101000": [0.1,1.8,1.6,2.4,2.8,2.8,3.2,2.5,4,1.6,3.1,2.1,
                       2.3,1.6,2.5,3.5,3.8,3.8,4.8,2.9]}
    periods = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 9)]

    spec = importlib.util.spec_from_file_location("bc", ROOT / "scripts" / "build_chains.py")
    bc = importlib.util.module_from_spec(spec); spec.loader.exec_module(bc)

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "c.sqlite")
        rows = []
        for d in (CELL, UP):
            for code, vals in d.items():
                for p_, v in zip(periods, vals):
                    rows.append(dict(period=p_, hs_code=code, hs6=code[:6], hs_name="",
                                     country_code="US", country_name="US",
                                     exp_usd=int(v * 1e6), exp_wgt=int(v * 1e4),
                                     imp_usd=0, imp_wgt=0, bal_usd=0))
        st.upsert_sector(rows)
        pl = bc.build(st, cs)
        st.close()

    bat = next(c for c in pl["chains"] if c["key"] == "battery")
    us = next(v for v in bat["views"] if v["market"] == "US")
    # 완제품은 거의 제자리인데 상류가 크게 늘어 체인 전체는 두 자릿수 성장
    assert -1 < us["finalYoy"] < 6, us["finalYoy"]
    assert us["upstreamYoy"] > 20, us["upstreamYoy"]
    assert us["totalYoy"] > 10, us["totalYoy"]
    # 최근 3개월 현지화지수가 8개월 평균보다 전환을 훨씬 잘 잡는다
    assert us["locQ3"] > us["loc"], (us["locQ3"], us["loc"])
    assert us["locQ3"] / us["locQ3Prev"] >= C.LOCALIZATION_JUMP, (us["locQ3"], us["locQ3Prev"])
    # ★ 핵심: ESS셀은 −13% 지만 '수요 위축'이 아니라 '현지화'로 판정돼야 한다
    ess = next(f for f in us["finals"] if f["code"] == "8507603000")
    assert ess["yoy"] < -10, ess["yoy"]
    assert ess["verdict"]["code"] == "localizing", ess["verdict"]

    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "renderChains" in html and "data/chains.json" in html
    assert "localizationWarn" in html, "워치리스트에 현지화 경고가 없다"
    assert "v.finals" in html, "배지가 체인 합계가 아니라 품목별 판정을 써야 한다"
    assert "DART" in html, "통관 데이터의 한계 안내가 없다"
    wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    assert "build_chains.py" in wf, "자동 갱신에 체인이 빠졌다"
    print(f"  ✓ 밸류체인 — ESS셀 {ess['yoy']}% → [{ess['verdict']['label']}], "
          f"체인 {us['totalYoy']}%, 현지화 {us['locQ3Prev']}→{us['locQ3']}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\n섹터·사이트 검증 — {len(tests)}개\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"  ✗ {t.__name__}: {exc}")
            import traceback; traceback.print_exc()
    print(f"\n{'실패 ' + str(failed) if failed else '전부 통과'} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
