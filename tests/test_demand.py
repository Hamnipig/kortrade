"""최종 수요 축 검증 — '현지화'와 '점유율 상실'을 가르는가. (API 호출 없음)

실행:  python tests/test_demand.py

이 레이어의 존재 이유 한 줄:
**(b)현지화와 (c)점유율 상실은 한국 수출을 똑같이 줄이지만 투자 판단이 정반대다.**
한국 수출 데이터만으로는 (c)를 원리적으로 볼 수 없다 — 미국 시장이 두 배가 됐는지
반토막 났는지 알 방법이 자체에 없기 때문이다.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from kortrade import battery as B  # noqa: E402
from kortrade import demand as D  # noqa: E402
from kortrade.store import Store  # noqa: E402


def test_config():
    c = D.load()
    assert c.validate() == [], c.validate()
    codes = {x.code: x for x in c.codes}
    # 미국 HTS 통계품목 — 한국 HSK 에 없는 **시스템 단위** 분류가 이 축의 핵심이다
    assert "8507600030" in codes, "컨테이너형 BESS 코드가 빠졌다"
    assert "8507600010" in codes and "8507600090" in codes
    # ★ USITC 원문을 직접 확인하지 못했다(JS 렌더). 기계가 확인하기 전에는 열지 않는다.
    assert all(x.status == "draft" for x in c.codes), \
        "미검증 HTS 코드가 active 로 올라갔다 — run_demand.py 의 설명 대조를 거쳐야 한다"
    assert all(x.expect for x in c.codes), "설명 대조 문자열이 없으면 자동 검증이 불가능하다"
    assert c.partner == "5800", "Census 한국 CTY_CODE"
    print(f"  ✓ 설정 — HTS {len(c.codes)}개(전부 draft) · 한국 CTY {c.partner}")


def test_attribution_separates_onshoring_from_share_loss():
    """이 레이어 전체가 이 한 가지를 위해 있다."""
    # 시장은 크는데(수입 +25%) 한국 비중이 30%→18% → **점유율 상실**
    v = D.attribute(kr_yoy=-13.0, imp_yoy=25.0, share_now=18.0, share_prev=30.0,
                    cap_yoy=40.0)
    assert v["code"] == "share_loss", v
    assert v["shareChg"] == -12.0, v

    # 설치는 느는데(+40%) 수입은 줄고(−20%) 비중은 유지 → **현지 생산 대체**
    v = D.attribute(-13.0, -20.0, 30.0, 31.0, 40.0)
    assert v["code"] == "onshoring", v

    # 설치도 수입도 함께 감소 → **수요 위축**
    v = D.attribute(-13.0, -20.0, 30.0, 31.0, -15.0)
    assert v["code"] == "demand_down", v

    # 시장도 크고 우리 몫도 지킴
    assert D.attribute(20.0, 25.0, 31.0, 30.0, 30.0)["code"] == "expanding"
    assert D.attribute(20.0, 25.0, 36.0, 30.0, 30.0)["code"] == "share_gain"

    # ★ 한 축만 들어와도 말할 수 있는 데까지는 말한다.
    #   Census 키는 2026-05-12 부터 필수가 됐고 발급 메일이 늦는 일이 있어서,
    #   EIA 키만 먼저 들어오는 상황이 실제로 생긴다.
    only_eia = D.attribute(-13.0, None, None, None, 35.0)
    assert only_eia["code"] == "demand_ok_partial", only_eia
    assert "수요 위축은 아닙니다" in only_eia["note"]
    assert "구분할 수 없습니다" in only_eia["note"], "구분 불가를 숨기면 안 된다"
    assert D.attribute(-13.0, None, None, None, -15.0)["code"] == "demand_down"
    # Census 만 있어도 점유율 판정은 된다 (이게 이 축의 핵심 기여다)
    assert D.attribute(-13.0, 25.0, 18.0, 30.0, None)["code"] == "share_loss"

    # ★ 미국 축이 없으면 **솔직하게 판정 불가**를 내려야 한다. 우기면 안 된다.
    p = D.attribute(-13.0, None, None, None, None)
    assert p["code"] == "partial", p
    assert "구분할 수 없" in p["note"]
    assert D.attribute(None, 10.0, 20.0, 20.0, 10.0)["code"] == "unknown"

    # 보합 구간 — 부호만 보면 매달 결론이 뒤집힌다
    assert D.attribute(-13.0, 2.0, 30.0, 31.0, 1.0)["code"] not in ("share_loss",)
    print("  ✓ 판정 — 현지화 / 점유율 상실 / 수요 위축 분리, 축 없으면 판정 불가")


def test_share_and_index_guards():
    assert D.share(30e6, 100e6) == 30.0
    assert D.share(3e6, 10e6) is None, "기저가 작으면 비중을 내지 않는다"
    assert D.share(None, 100e6) is None
    # 지수는 **비교용**이다. 단위가 다른 계열을 한 축에 올리기 위한 것.
    # 기준월은 **첫 유효값**(0 이 아닌)이다. 0 으로 나누면 계열이 통째로 사라진다.
    assert D.index([None, 0, 50.0, 75.0]) == [None, 0.0, 100.0, 150.0]
    assert D.index([None, 0, 0]) is None
    print("  ✓ 가드 — 작은 기저 / 0 기준월")


def _bb():
    spec = importlib.util.spec_from_file_location("bb", ROOT / "scripts" / "build_battery.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_build_attaches_demand():
    """빌드 산출물에 최종 수요 축이 실리고, 판정이 실제로 갈리는지."""
    import test_battery as T
    bb, cfg = _bb(), B.load()
    td = Path(tempfile.mkdtemp())
    db = td / "d.sqlite"
    months = T._fixture(db)

    # 미국 수입: 전세계는 커지는데(+60%) 한국분은 줄어든다 → 점유율 상실이어야 한다
    rows = []
    n = len(months)
    for i, p in enumerate(months):
        grow = 1 + 0.03 * i
        rows.append({"source": "census", "series": "8507600030:ALL", "period": p,
                     "value": 400e6 * grow, "unit": "USD"})
        rows.append({"source": "census", "series": "8507600030:KR", "period": p,
                     "value": 160e6 * max(0.2, 1 - 0.025 * i), "unit": "USD"})
        rows.append({"source": "eia", "series": "capacity", "period": p,
                     "value": 20000 * grow, "unit": "MW"})
    with Store(db) as st:
        st.upsert_demand(rows)
        payload = bb.build(st, cfg)

    assert payload, "배터리 payload 없음"
    dm = payload.get("demand")
    assert dm and dm["ok"], dm
    assert dm["months"] == payload["months"], "차트 축이 배터리 본체와 어긋난다"
    assert dm["picked"] == "8507600030", dm["picked"]
    assert dm["imp"]["yoy"] and dm["imp"]["yoy"] > 20, dm["imp"]
    assert dm["imp"]["share"] is not None and dm["imp"]["sharePrev"] is not None
    assert dm["imp"]["share"] < dm["imp"]["sharePrev"], "점유율이 떨어져야 하는 시드다"
    assert dm["verdict"]["code"] == "share_loss", dm["verdict"]
    # 지수는 세 축 모두 100 에서 출발해야 한다 (합산이 아니라 비교라는 표시)
    for k in ("kr", "imp", "cap"):
        ser = dm["idx"].get(k)
        if ser:
            assert next(v for v in ser if v is not None) == 100.0, (k, ser[:3])
    # 갱신주기가 다르다는 사실이 페이로드에 남아 있어야 한다
    assert len(dm["sources"]) == 3 and all(x["freq"] for x in dm["sources"])
    print(f"  ✓ 빌드 — 미국 수입 +{dm['imp']['yoy']}% · 한국 점유율 "
          f"{dm['imp']['sharePrev']}% → {dm['imp']['share']}% "
          f"[{dm['verdict']['label']}]")


def test_build_without_demand_data_is_graceful():
    """키가 없으면 이 축만 비고 나머지는 그대로 돌아야 한다."""
    import test_battery as T
    bb, cfg = _bb(), B.load()
    db = Path(tempfile.mkdtemp()) / "e.sqlite"
    T._fixture(db)
    with Store(db) as st:
        payload = bb.build(st, cfg)
    assert payload and payload["stages"], "수요 축이 없다고 배터리 빌드가 죽으면 안 된다"
    assert payload["demand"]["ok"] is False
    assert payload["demand"]["note"], "왜 비었는지 화면에 말할 근거가 없다"
    assert payload["demand"]["verdict"]["code"] in ("partial", "unknown")
    # ★ 원인을 뭉뚱그리면 안 된다. 실제로 원인이 **워크플로 단계 순서**였는데
    #   "키를 못 찾았거나 아직 실행되지 않았습니다" 로만 떠서 화면만 보고는
    #   알 수 없었다 (2026-09-28). 축별 상태를 따로 낸다.
    ax = payload["demand"]["axes"]
    assert set(ax) == {"census", "eia"} and all(v["note"] for v in ax.values()), ax
    assert ax["census"]["ok"] is False and ax["eia"]["ok"] is False
    print("  ✓ 키 없음 — 이 축만 비고 파이프라인은 계속 돈다 (원인·축별 상태 표시)")


def test_collection_runs_before_build():
    """★ 수집이 빌드보다 **앞**에 있어야 한다.

    2026-09-28 실측 버그: `수집 — 최종 수요` 단계가 `사이트 데이터 생성` **뒤**에
    있었다. build_battery.py 는 DB 의 demand_series 와 data/demand_verify.json 을
    읽으므로, 순서가 뒤집히면 **키를 넣어도 패널이 영원히 빈다.** 그리고 화면에는
    "키를 못 찾았거나 아직 실행되지 않았습니다" 로만 떠서 원인을 알 수 없다.
    """
    import yaml
    d = yaml.safe_load((ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    names = [s.get("name", "") for s in d["jobs"]["collect"]["steps"]]
    i_dem = next(i for i, n in enumerate(names) if "최종 수요" in n)
    i_bld = names.index("사이트 데이터 생성")
    assert i_dem < i_bld, (
        f"최종 수요 수집({i_dem + 1})이 사이트 빌드({i_bld + 1})보다 뒤에 있다 — "
        "키가 있어도 패널이 영원히 빈다")
    # 다른 수집 단계들도 같은 함정에 빠지지 않았는지 함께 본다
    for kw in ("섹터 국가별", "워치리스트", "유니버스", "속보"):
        i = next((i for i, n in enumerate(names) if kw in n), None)
        assert i is None or i < i_bld, f"'{kw}' 수집이 빌드 뒤에 있다"
    print(f"  ✓ 단계 순서 — 수집({i_dem + 1}) < 빌드({i_bld + 1})")


def test_collector_never_leaks_key_and_exits_clean():
    src = (ROOT / "scripts" / "run_demand.py").read_text(encoding="utf-8")
    # ★ URL 에 키가 들어간다. 로그에 URL 을 통째로 찍으면 공개 레포에 키가 남는다.
    assert 'url.split("?")[0]' in src, "요청 실패 로그가 URL 전체를 찍는다 — 키가 샌다"
    assert "print(url" not in src and "log.info(url" not in src
    assert 'os.environ.get("CENSUS_API_KEY"' in src and 'os.environ.get("EIA_API_KEY"' in src
    # 키가 없을 때 **0 으로 끝나야** 워크플로가 죽지 않는다
    assert "return 0" in src.split("없어 최종 수요 축을 건너뜁니다")[1][:500]
    # 연료코드를 추측해 박지 않고 API 에게 묻는다
    assert "def discover_eia" in src and "facet/" in src
    # EIA v2 는 숫자를 문자열로 준다 — float() 없이 더하면 문자열 연결이 된다
    assert "float(r.get(col))" in src
    print("  ✓ 수집기 — 키 미노출 · 키 없으면 정상 종료 · 코드 자동 발견")


def test_wired_into_site_and_workflow():
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    for t in ("renderDemand", 'id="bdem"', "DVCLS", "최종 수요 대조"):
        assert t in html, f"화면에 {t} 가 없다"
    # 합산 금지 경고는 이 패널의 핵심이다 — 지우면 안 된다
    assert "세 축을 더하지 마십시오" in html
    assert "GWh 로 환산하지 않습니다" in html
    assert "수요이지 한국의 공급이 아닙니다" in html
    assert "CENSUS_API_KEY" in html and "EIA_API_KEY" in html, \
        "키가 없을 때 무엇을 하면 되는지 안내가 없다"
    wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    assert "run_demand.py" in wf, "자동 갱신에 최종 수요 수집이 빠졌다"
    assert "continue-on-error: true" in wf
    assert "3028661a" not in wf
    print("  ✓ 화면·자동화 연결 (합산 금지 경고 포함)")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\n최종 수요 축 검증 — {len(tests)}개\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:                          # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {exc}")
            import traceback; traceback.print_exc()
    print(f"\n{'실패 ' + str(failed) if failed else '전부 통과'} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
