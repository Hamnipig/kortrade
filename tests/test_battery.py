"""2차전지·ESS 심화 레이어 검증. (API 호출 없음)

이 레이어에서 조용히 틀릴 수 있는 지점을 고정한다.

  1. 원가 자리에 수출 코드가 들어가는 사고 — 제시받은 목록의 실제 오류였다.
     2841.90.9020 은 '전구체(수입)'가 아니라 **양극재(수출) $2,650M** 이다.
     원가 변수 자리에 판가를 넣으면 스프레드 부호가 뒤집힌다.
  2. 시차를 **수준**으로 찾는 사고 — 수준끼리는 둘 다 추세를 갖고 있어
     어느 시차나 R² 가 높다. 반드시 차분으로 찾아야 한다.
  3. 미검증 코드가 합계에 섞이는 것 — draft 는 측정만 하고 합계 밖에 둔다.
  4. 리튬 원단위를 상수로 가정하는 것 — 출처 없는 상수가 결과를 만든다.

실행:  python tests/test_battery.py
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kortrade import battery as B        # noqa: E402
from kortrade import flash as F          # noqa: E402
from kortrade.store import Store         # noqa: E402


def _fixture(db: Path, true_lag: int = 2, true_beta: float = 1.6, n: int = 30):
    """리튬(C) → 양극재 판가(P) 관계를 **알고 있는 값으로** 심어 둔다.

    C 는 톱니(sawtooth)로 만든다. 매끄러운 곡선으로 만들면 어느 시차나 맞아서
    시차 식별 자체를 검증할 수 없다 — 실제로 U자 곡선에서 그 함정을 밟았다.
    """
    months = []
    y, m = 2024, 3
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    C = [12.0 + 6.0 * ((i * 7) % 5) / 4.0 for i in range(n)]      # 톱니
    P = [20.0 + true_beta * C[max(0, i - true_lag)] for i in range(n)]

    rows = []
    def add(p, code, cty, eu=0.0, ew=0.0, iu=0.0, iw=0.0):
        rows.append({"period": p, "hs_code": code, "hs6": code[:6], "hs_name": code,
                     "country_code": cty, "country_name": cty,
                     "exp_usd": int(eu), "exp_wgt": int(ew),
                     "imp_usd": int(iu), "imp_wgt": int(iw), "bal_usd": 0})

    for i, p in enumerate(months):
        w = 8_000_000.0
        # 수산화리튬만 P 와 정확히 연동시키고, 나머지 둘은 잡음을 섞어
        # **어느 원가 코드가 뽑히는지**까지 검증한다. 셋이 모두 완전 비례면
        # R² 가 동률이라 순서 운으로 뽑히고, 그러면 β 기대값도 달라진다.
        add(p, "2825200000", "ALL", iu=C[i] * w, iw=w)            # 수산화리튬(수입)
        noise = 3.0 * math.sin(i * 1.7)
        add(p, "2836910000", "ALL", iu=(C[i] * 0.8 + noise) * w, iw=w)
        add(p, "2825400000", "ALL", iu=(C[i] * 0.9 - noise) * w, iw=w)
        pw = 20_000_000.0
        add(p, "2841909020", "ALL", eu=P[i] * pw * 0.7, ew=pw * 0.7)
        add(p, "2841909030", "ALL", eu=P[i] * pw * 0.3, ew=pw * 0.3)
        for code, base in (("8507602000", 55e6), ("8507603000", 130e6),
                           ("8507609000", 100e6)):
            add(p, code, "ALL", eu=base * (1 + 0.01 * i), ew=base / 30)
            add(p, code, "US", eu=base * 0.4, ew=base / 30 * 0.4)
        add(p, "8507909000", "ALL", eu=90e6 * (1 + 0.02 * i), ew=3.6e6)
        add(p, "8507909000", "US", eu=50e6 * (1 + 0.03 * i), ew=2e6)
        add(p, "3801101000", "ALL", eu=5e6, ew=6e5)
        add(p, "8479899050", "ALL", eu=18e6 * (1 + 0.04 * i), ew=3.6e5)
        for code, base in (("7410110000", 70e6), ("7607119000", 120e6),
                           ("3824999053", 4e6), ("3824999038", 0.2e6),
                           ("8479899099", 900e6), ("8543709090", 600e6),
                           ("8428909000", 40e6)):
            add(p, code, "ALL", eu=base, ew=base / 10)
    with Store(db) as s:
        s.upsert_sector(rows)
    return months


def test_config_audit_pinned():
    """제시받은 목록의 감사 결과를 설정에 고정한다.

    이 프로젝트는 앞서 제시받은 HSK 20개 중 12개가 틀린 적이 있다.
    같은 실수가 다시 들어오지 않도록 **왜 뺐는지**까지 파일에 남긴다.
    """
    cfg = B.load()
    assert cfg.validate() == [], cfg.validate()

    cost = {c.code for c in cfg.cost}
    price = {c.code for c in cfg.price}
    mat = set(cfg.stage_codes("material"))

    # ★ 핵심 — 2841909020 은 양극재(수출)다. 원가 쪽에 있으면 안 된다.
    assert "2841909020" not in cost, \
        "2841.90.9020 이 원가(수입)에 들어갔다 — 이 코드는 양극재 수출($2,650M)이다"
    assert "2841909020" in price and "2841909020" in mat
    assert "2841909030" in price

    # 리튬 2종은 원가에 있어야 한다
    assert {"2836910000", "2825200000"} <= cost, cost
    # 실측으로 뺀 코드는 rejected 에 근거와 함께 남아 있어야 한다
    rej = {r["code"]: r for r in cfg.rejected}
    assert "2804690000" in rej, "금속규소(2804.69)를 실리콘 음극재로 쓰는 건 제외해야 한다"
    assert "금속규소" in rej["2804690000"]["reason"]

    # 이 프로젝트가 이미 실측한 코드들이 빠지지 않았는지
    assert "8507909000" in cfg.stage_codes("component"), "전지 부분품($881M, 對미 +191%) 누락"
    assert "8479899050" in cfg.stage_codes("equipment"), "코팅머신($157M) 누락"
    assert "3801101000" in mat, "음극재 인조흑연 누락"

    # 제시받은 미검증 코드는 draft 로 들어와 있어야 한다 (버리지도, 믿지도 않는다)
    drafts = {d.code for d in cfg.drafts()}
    for c in ("7410110000", "7607119000", "8479899099", "8543709090",
              "8428909000", "3824999038"):
        assert c in drafts, f"{c} 가 draft 로 들어와 있지 않다"
    print(f"  ✓ 코드 감사 고정 — 원가 {len(cost)} / 판가 {len(price)} / "
          f"draft {len(drafts)} / 제외 {len(rej)}")


def test_cost_price_overlap_is_rejected():
    """원가와 판가에 같은 코드가 들어가면 설정 검증이 막아야 한다."""
    cfg = B.load()
    cfg.cost.append(B.Code(code="2841909020", label="일부러 넣은 오류"))
    errs = cfg.validate()
    assert any("부호가 뒤집힌다" in e for e in errs), errs
    print("  ✓ 원가·판가 코드 중복 차단 (스프레드 부호 역전 방지)")


def test_lag_found_on_differences_not_levels():
    """시차는 **차분**으로 찾아야 한다.

    수준끼리는 둘 다 추세를 갖고 있어 어느 시차나 R² 가 높게 나온다.
    실제로 매끄러운 U자 원가 시계열에서 수준 회귀는 진짜 시차 2를 놓치고 0을 골랐다.
    """
    months = []
    y, m = 2024, 3
    for _ in range(30):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    C = {p: 12.0 + 6.0 * ((i * 7) % 5) / 4.0 for i, p in enumerate(months)}
    P = {p: 20.0 + 1.6 * C[months[max(0, i - 2)]] for i, p in enumerate(months)}

    got = B.best_lag(P, C, F.shift, 4, 12)
    assert got is not None
    assert got["lag"] == 2, f"진짜 시차 2를 못 찾았다: {got['lag']} (곡선 {got['curve']})"
    assert abs(got["fit"]["beta"] - 1.6) < 0.2, got["fit"]["beta"]
    assert got["identified"] is True, "톱니 입력이면 시차가 식별돼야 한다"

    src = (ROOT / "kortrade" / "battery.py").read_text(encoding="utf-8")
    assert "_diff(" in src and "차분" in src, "시차를 차분으로 찾지 않는다"
    # 리튬 원단위를 상수로 박아두지 않았는지
    assert "0.44" not in src and "원단위" in src, \
        "리튬 원단위를 상수로 가정하면 그 가정이 결과를 만든다"
    print(f"  ✓ 시차 식별 — 차분 회귀로 L={got['lag']} β={round(got['fit']['beta'],3)} "
          f"(식별됨), 원단위 상수 없음")


def test_flat_curve_reports_unidentified():
    """원가가 매끄러우면 시차가 식별되지 않는다 — 그 사실을 말해야 한다."""
    months = []
    y, m = 2024, 3
    for _ in range(30):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    # 매끄러운 U자 — 어느 시차나 비슷하게 맞는다
    C = {p: max(8.0, 22.0 - 0.9 * i + 0.018 * i * i) for i, p in enumerate(months)}
    P = {p: 12.0 + 1.6 * C[months[max(0, i - 2)]] for i, p in enumerate(months)}
    got = B.best_lag(P, C, F.shift, 4, 12)
    assert got is not None and got["identified"] is False, \
        "평탄한 곡선인데 '식별됨'이라고 말하고 있다"
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "시차가 식별되지 않았습니다" in html, "화면에 미식별 경고가 없다"
    print("  ✓ 평탄 곡선 — '시차 미식별'로 정직하게 보고")


def test_verdict_separates_turnaround_from_acceleration():
    """'가속'과 '턴어라운드'는 다른 말이다. 섞으면 기저효과를 성장으로 읽는다."""
    assert B.verdict(-20.0, 12.0)["code"] == "turnaround"   # 부호가 바뀜
    assert B.verdict(-20.0, -5.0)["code"] == "bottoming"    # 낙폭 축소
    assert B.verdict(-20.0, -19.0)["code"] == "contracting"
    assert B.verdict(5.0, 30.0)["code"] == "accelerating"
    assert B.verdict(30.0, 5.0)["code"] == "decelerating"
    assert B.verdict(10.0, 12.0)["code"] == "steady"
    assert B.verdict(None, 5.0)["code"] == "unknown"
    print("  ✓ 판정 — 턴어라운드/감속둔화/가속/감속 분리")


def test_end_to_end_build():
    """수집 → 빌드가 실제로 돌고, draft 가 합계 밖에 있는지 확인한다."""
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "t.sqlite"
    _fixture(db)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_battery.py"),
                        "--db", str(db), "--out", str(tmp / "d")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    p = json.loads((tmp / "d" / "battery.json").read_text(encoding="utf-8"))

    sp = p["spread"]
    assert sp["linked"] is True, sp
    assert sp["costCode"] == "2825200000", \
        f"P 와 정확히 연동된 수산화리튬 대신 {sp['costCode']} 를 골랐다"
    assert sp["lag"] == 2, f"심어 둔 시차 2를 못 찾았다: {sp['lag']}"
    assert abs(sp["beta"] - 1.6) < 0.2, sp["beta"]
    assert sp["r2"] > 0.9, sp["r2"]

    stages = {s["stage"]: s for s in p["stages"]}
    assert set(stages) == {"final", "component", "material", "equipment"}, stages.keys()
    # 합계에서 장비는 빠져야 한다 (설비투자는 수요의 흐름이 아니다)
    demand = sum(stages[s]["usd"] for s in ("final", "component", "material"))
    assert abs(p["total"]["usd"] - demand) < 1.0, (p["total"]["usd"], demand)
    assert p["total"]["usd"] < demand + stages["equipment"]["usd"], "장비가 합계에 섞였다"

    # draft 는 측정만 하고 단계 합계 밖에 있어야 한다
    draft_codes = {d["code"] for d in p["drafts"]}
    for s in p["stages"]:
        for it in s["items"]:
            assert it["code"] not in draft_codes, f"{it['code']} 가 단계 합계에 들어갔다"
    assert "7410110000" in draft_codes and "8479899099" in draft_codes
    # 수입 코드는 '수입'으로 표기돼야 한다
    li = next(d for d in p["drafts"] if d["code"] == "2825200000")
    assert li["side"] == "수입", li
    assert li["unitPrice"] and li["unitPrice"] > 0

    assert p["leadlag"] and all("bestK" in l for l in p["leadlag"])
    assert p["rejected"], "제외 근거가 화면 데이터에 없다"
    print(f"  ✓ 빌드 — 시차 {sp['lag']}M β={sp['beta']} R²={sp['r2']}, "
          f"단계 {len(p['stages'])}개, draft {len(p['drafts'])}개 분리")


def test_no_cross_layer_imports():
    """레이어는 다른 레이어를 import 하지 않는다.

    회귀: battery.py 가 flash.py 에서 ols 를 가져오던 구조라, battery.py 만
    배포하고 flash.py 를 빼먹자 수집이 통째로 멈췄다
    (`ImportError: cannot import name 'ols' from 'kortrade.flash'`).
    공용 코드는 kortrade/stats.py 한 자리에 둔다.
    """
    from kortrade import stats as S
    layers = ("flash", "battery", "chains", "watchlist")
    for a_ in layers:
        src = (ROOT / "kortrade" / f"{a_}.py").read_text(encoding="utf-8")
        for b_ in layers:
            if a_ == b_:
                continue
            assert f"from .{b_} import" not in src, \
                f"{a_}.py 가 {b_}.py 를 import 한다 — 배포 누락 사고의 원인이다"
    # 공용 통계는 한 자리에만 있어야 한다
    assert B.ols is S.ols and B.predict is S.predict and B.corr is S.corr
    assert (ROOT / "kortrade" / "stats.py").exists()
    bsrc = (ROOT / "kortrade" / "battery.py").read_text(encoding="utf-8")
    assert "def ols(" not in bsrc and "def corr(" not in bsrc, \
        "battery.py 가 통계 함수를 다시 정의하고 있다 — 정의가 갈린다"

    # 배포 정합성 점검이 있고, 수집 전에 돌아야 한다
    sc = ROOT / "scripts" / "selfcheck.py"
    assert sc.exists(), "배포 정합성 점검 스크립트가 없다"
    import yaml
    for wf in (".github/workflows/update.yml", ".github/workflows/flash.yml"):
        d = yaml.safe_load((ROOT / wf).read_text(encoding="utf-8"))
        steps = list(d["jobs"].values())[0]["steps"]
        idx = [i for i, st in enumerate(steps) if "selfcheck.py" in str(st.get("run", ""))]
        assert idx, f"{wf} 에 정합성 점검 단계가 없다"
        first_api = min((i for i, st in enumerate(steps)
                         if "DATA_GO_KR_SERVICE_KEY" in str(st.get("env", {}))),
                        default=len(steps))
        assert idx[0] < first_api, \
            f"{wf} 의 정합성 점검이 API 호출 단계보다 뒤에 있다 — 먼저 걸러야 한다"
    print("  ✓ 레이어 간 import 없음 · 공용 통계 단일 출처 · 수집 전 정합성 점검")


def test_wired_into_site_and_automation():
    """자동 갱신과 화면에 붙어 있어야 한다."""
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    for token in ("renderBattery", "data/battery.json", "BAT_KEY", "drawSpread"):
        assert token in html, f"화면에 {token} 가 없다"
    assert "셀 마진을 추정하지 않습니다" in html, \
        "양극재/셀의 기제 차이 경고가 화면에 없다"
    for wf in (".github/workflows/update.yml", ".github/workflows/flash.yml"):
        s = (ROOT / wf).read_text(encoding="utf-8")
        assert "build_battery.py" in s, f"{wf} 에 배터리 빌드가 없다"
        assert "3028661a" not in s
    rw = (ROOT / "scripts" / "run_watchlist.py").read_text(encoding="utf-8")
    assert "BAT.load()" in rw, "수집기가 배터리 코드를 받지 않는다"
    assert "bt.all_parents()" in rw, "배터리 HS6 가 전국 합계 수집에 안 들어갔다"
    print("  ✓ 자동화 — 수집·빌드·화면 연결")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\n2차전지·ESS 레이어 검증 — {len(tests)}개\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"  ✗ {t.__name__}: {exc}")
            import traceback
            traceback.print_exc()
    print(f"\n{'실패 ' + str(failed) if failed else '전부 통과'} / {len(tests)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
