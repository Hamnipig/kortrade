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
