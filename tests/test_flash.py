"""속보(10일 단위 잠정치) 레이어 검증. (API 호출 없음)

이 레이어가 조용히 틀릴 수 있는 지점만 골라서 고정한다.

  1. 슬롯 번호 ↔ 품목 대응 — 응답에 품목명이 없다. 한 칸 밀리면 산업이 통째로 바뀐다.
  2. 누계/구간 — 값은 1일부터의 누계다. 구간값으로 오해하면 전부 틀린다.
  3. 단위 — 원본 천 달러. 달러로 환산하지 않으면 다른 표와 1000배 어긋난다.
  4. 국가 슬롯 개방 조건 — 설정 파일을 손으로 고쳐서 열 수 없어야 한다.
  5. 조업일수 보정 — 착지 추정이 누계 YoY 와 같은 숫자면 새 정보가 없는 것이다.

실행:  python tests/test_flash.py
"""
from __future__ import annotations

import calendar
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kortrade import flash as F          # noqa: E402
from kortrade.client import parse_response  # noqa: E402
from kortrade.store import Store         # noqa: E402

# 2026-08 월전체 잠정 실측 ($) — 슬롯 규모 고정용
AUG26 = {"00": 98.28e9, "01": 46.83e9, "02": 4.04e9, "03": 3.64e9, "04": 6.88e9,
         "05": 1.65e9, "06": 1.63e9, "07": 1.42e9, "08": 6.41e9, "09": 0.86e9,
         "10": 0.51e9}


def test_slot_mapping_pinned():
    """슬롯 순서를 고정한다. 이 순서가 바뀌면 화면 전체가 다른 산업을 말하게 된다."""
    cfg = F.load()
    assert cfg.validate() == [], cfg.validate()
    got = [(s.slot, s.label) for s in cfg.item.slots]
    want = [("00", "전체 수출"), ("01", "반도체"), ("02", "철강제품"), ("03", "승용차"),
            ("04", "석유제품"), ("05", "무선통신기기"), ("06", "선박"),
            ("07", "자동차부품"), ("08", "컴퓨터 주변기기"), ("09", "정밀기기"),
            ("10", "가전제품")]
    assert got == want, f"슬롯 순서가 바뀌었다:\n  got  {got}\n  want {want}"
    # slot08 은 SSD 실측과 대조해 확정한 유일한 앵커다. 연결이 끊기면 근거가 사라진다.
    s08 = next(s for s in cfg.item.slots if s.slot == "08")
    assert "8523511000" in s08.watch, "slot08 과 SSD(8523511000) 의 연결이 끊겼다"
    print(f"  ✓ 품목 슬롯 11개 고정 — 앵커 slot08 ↔ SSD {s08.watch}")


def test_country_slots_locked_until_verified():
    """국가 슬롯은 **기계 검증 없이** 열리면 안 된다.

    config 의 verified 를 사람이 true 로 바꿔도 화면이 열리지 않아야 한다 —
    개방권은 data/flash_verify.json(검증 결과)에만 있다.
    """
    cfg = F.load()
    assert cfg.country.verified is False, \
        "config/flash.yaml 의 country.verified 가 true 다. 개방은 verify_flash.py 결과로만 한다."
    build = (ROOT / "scripts" / "build_flash.py").read_text(encoding="utf-8")
    assert 'v.get("country")' in build and '"ok"' in build, \
        "build_flash.py 가 검증 결과 파일을 보지 않는다"
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "FLASH.countryVerified" in html, "화면이 검증 여부와 무관하게 국가 표를 그린다"
    print("  ✓ 국가 슬롯 잠금 — 검증 결과 파일로만 개방")


def test_parse_dt_and_seq():
    """'01~28'(2월)도 월전체다. 날짜 숫자로 직접 비교하면 2월을 중순으로 읽는다."""
    assert F.parse_dt("01~10") == (10, 1)
    assert F.parse_dt("01~20") == (20, 2)
    for last in (28, 29, 30, 31):
        assert F.parse_dt(f"01~{last}") == (last, 3), last
    assert F.parse_dt("") == (0, 0)
    assert F.parse_dt("총계") == (0, 0)
    print("  ✓ 순(旬) 판별 — 2월(01~28)도 월전체로 인식")


def test_unit_conversion_is_thousand_dollars():
    """원본 천 달러 → 달러. 이 환산이 빠지면 정합성 점검이 1000배로 어긋난다."""
    assert F.UNIT_USD == 1_000
    xml = """<response><header><resultCode>00</resultCode></header><body><items>
      <item><priodYear>2026</priodYear><priodMon>08</priodMon><priodDt>01~10</priodDt>
      <itemUsdAmt00> 21,263,370</itemUsdAmt00><itemUsdAmt01> 9,900,000</itemUsdAmt01>
      </item></items></body></response>"""
    fields = {"priodYear": "year", "priodMon": "month", "priodDt": "dt",
              "itemUsdAmt00": "v00", "itemUsdAmt01": "v01"}
    recs = parse_response(xml, fields)
    assert len(recs) == 1, recs
    # 쉼표와 앞쪽 공백이 붙어서 온다 — 그대로 int() 하면 터진다
    assert recs[0]["v00"] == "21,263,370"
    from kortrade.collect import _scale
    assert _scale(recs[0]["v00"], F.UNIT_USD) == "21263370000"
    print("  ✓ 단위 — 천달러 21,263,370 → $21.26B")


def test_cumulative_not_incremental():
    """누계에서 구간값을 빼낸다. 누계를 구간으로 오해하면 중순·하순이 3배로 부풀어 오른다."""
    db = Path(tempfile.mkdtemp()) / "t.sqlite"
    rows = []
    for seq, dayto, v in ((1, 10, 30e9), (2, 20, 65e9), (3, 31, 100e9)):
        rows.append({"period": "2026-08", "seq": seq, "kind": "item", "slot": "00",
                     "dt": f"01~{dayto}", "day_to": dayto, "exp_usd": int(v)})
    with Store(db) as s:
        s.upsert_flash(rows)
        got = {r["seq"]: r["exp_usd"] for r in
               s.conn.execute("SELECT seq, exp_usd FROM flash_trade")}
    assert got[2] - got[1] == 35e9, "중순 구간 = (01~20) − (01~10)"
    assert got[3] - got[2] == 35e9, "하순 구간 = (01~말) − (01~20)"
    build = (ROOT / "scripts" / "build_flash.py").read_text(encoding="utf-8")
    assert "c2 - c1" in build and "c3 - c2" in build, \
        "build_flash.py 가 누계를 구간으로 바꾸지 않는다"
    print("  ✓ 누계→구간 — 상순 30 / 중순 35 / 하순 35 ($B)")


def test_bad_month_never_enters_and_never_crashes():
    """회귀: period='2026-20' 한 행이 빌드를 통째로 죽였다 (IllegalMonthError).

    방어선 세 겹을 전부 확인한다.
      1. 수집기가 달력에 없는 달을 애초에 저장하지 않는다
      2. Store 가 이미 들어간 불량 행을 지운다 (DB 를 레포에 커밋하는 구조라
         한 번 들어간 행은 계속 따라다닌다)
      3. 빌드가 남은 불량 행을 만나도 죽지 않고 건너뛴다
    """
    # 1) 수집 단계 — parse_period 가 막는다
    assert F.parse_period("2026", "08") == "2026-08"
    for y, m in (("2026", "20"), ("2026", "0"), ("2026", "13"), ("2026", None),
                 ("2026", "총계"), ("1999", "08")):
        assert F.parse_period(y, m) is None, (y, m)
    assert F.split_period("2026-20") is None
    assert F.split_period("2026-08") == (2026, 8)
    assert F.split_period("") is None
    src = (ROOT / "kortrade" / "collect.py").read_text(encoding="utf-8")
    assert "parse_period(r.get(\"year\"), r.get(\"month\"))" in src, \
        "수집기가 기간을 검증하지 않는다"
    assert "버린 행 원문" in src, \
        "버린 행을 원문으로 찍어야 응답의 실제 모양을 알 수 있다"

    # 2) Store 단계 — 이미 들어간 행 청소
    db = Path(tempfile.mkdtemp()) / "t.sqlite"
    good = {"period": "2026-08", "seq": 3, "kind": "item", "slot": "00",
            "dt": "01~31", "day_to": 31, "exp_usd": 100}
    with Store(db) as s:
        s.upsert_flash([good,
                        {**good, "period": "2026-20", "day_to": 20},
                        {**good, "period": "2026-00"},
                        {**good, "period": "abcd-ef"}])
        assert s.purge_bad_flash() == ["2026-00", "2026-20", "abcd-ef"]
        left = [r["period"] for r in s.conn.execute("SELECT period FROM flash_trade")]
        assert left == ["2026-08"], left
        assert s.purge_bad_flash() == []          # 멱등

    # 3) 빌드 단계 — 불량 행이 남아 있어도 살아남는다
    build = (ROOT / "scripts" / "build_flash.py").read_text(encoding="utf-8")
    assert "F.split_period(r[\"period\"])" in build, "빌드가 기간을 거르지 않는다"
    assert "purge_bad_flash" in build, "빌드가 DB 청소를 하지 않는다"
    assert "int(latest[:4])" not in build and "int(py[:4])" not in build, \
        "기간을 다시 맨손으로 파싱하고 있다 — split_period 를 쓸 것"
    print("  ✓ 불량 period 방어 3겹 — 수집 차단 / DB 청소 / 빌드 스킵")


def test_landing_adds_information():
    """착지 추정이 누계 YoY 와 같은 숫자면 아무것도 더하지 않은 것이다.

    단순 비례식은 정의상 estYoY == 누계 YoY 가 된다. 평일 보정을 넣어야
    '조업일수 때문에 생긴 착시'가 분리된다.
    """
    cum_now = cum_prev = 100.0
    full_prev = 300.0
    simple = F.landing_simple(cum_now, cum_prev, full_prev)
    assert abs(simple / full_prev - cum_now / cum_prev) < 1e-9, \
        "단순 비례식의 전년비는 누계 YoY 와 같아야 한다(그래서 쓰지 않는다)"

    # 2026-05 상순은 평일 6일, 2025-05 상순은 7일 — 누계가 같아도 속도는 다르다
    w_now, w_prev = F.weekdays(2026, 5, 10), F.weekdays(2025, 5, 10)
    assert (w_now, w_prev) == (6, 7), (w_now, w_prev)
    wd = F.landing_wd(cum_now, cum_prev, full_prev, w_now, w_prev,
                      F.weekdays(2026, 5, 31), F.weekdays(2025, 5, 31))
    assert wd > simple * 1.05, (wd, simple)
    print(f"  ✓ 착지 추정 — 누계 YoY 0% 인데 평일 보정 착지 "
          f"{round(wd / full_prev * 100 - 100, 1)}% (평일 {w_prev}→{w_now}일)")


def test_weekdays_exact():
    """평일수는 정확해야 한다. 단, 공휴일은 못 센다 — 그 한계가 문서에 있어야 한다."""
    assert F.weekdays(2026, 9, 10) == 8          # 9/1(화)~9/10(목)
    assert F.weekdays(2026, 2, 31) == F.weekdays(2026, 2, 28)   # 2월은 28일까지만
    assert F.weekdays(2024, 2, 31) == F.weekdays(2024, 2, 29)   # 윤년
    assert calendar.monthrange(2024, 2)[1] == 29
    src = (ROOT / "kortrade" / "flash.py").read_text(encoding="utf-8")
    assert "공휴일" in src, "공휴일을 못 센다는 한계가 코드에 적혀 있어야 한다"
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "공휴일은 반영하지 못합니다" in html, "화면에 한계 안내가 없다"
    print("  ✓ 평일수 계산 — 월말/윤년 처리, 공휴일 한계 명시")


def test_share_is_the_headline():
    """점유율 변화가 기본 정렬이어야 한다. 누계 YoY 를 헤드라인으로 두면 조업일수 잡음이 앞선다."""
    build = (ROOT / "scripts" / "build_flash.py").read_text(encoding="utf-8")
    assert 'r["shareChgPp"]' in build, "빌드가 점유율 변화로 정렬하지 않는다"
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert 'flSort = "shareChgPp"' in html, "화면 기본 정렬이 점유율 변화가 아니다"
    assert "hicol" in html, "점유율 변화 열 강조가 없다"
    print("  ✓ 기본 축 — 점유율 변화(조업일수 중립)")


def test_end_to_end_build():
    """수집→빌드가 실제로 돌고, 정합성 점검과 파생 비율이 나오는지 확인한다."""
    tmp = Path(tempfile.mkdtemp())
    db, out = tmp / "t.sqlite", tmp / "data"
    rows, uni = [], []
    for y in (2025, 2026):
        for m in range(1, 13):
            if (y, m) > (2026, 9):
                continue
            last = calendar.monthrange(y, m)[1]
            for slot, base in AUG26.items():
                full = base * (1.3 if y == 2026 else 1.0)
                for seq, dayto, frac in ((1, 10, .33), (2, 20, .65), (3, last, 1.0)):
                    if (y, m) == (2026, 9) and seq > 1:
                        continue
                    rows.append({"period": f"{y}-{m:02d}", "seq": seq, "kind": "item",
                                 "slot": slot, "dt": f"01~{dayto:02d}", "day_to": dayto,
                                 "exp_usd": int(full * frac)})
                    rows.append({**rows[-1], "kind": "country"})
                if slot == "00":
                    uni.append({"period": f"{y}-{m:02d}", "hs4": "8542", "hs2": "85",
                                "top_name": "x", "exp_usd": int(full * 0.99),
                                "exp_wgt": 1, "imp_usd": 1})
    with Store(db) as s:
        s.upsert_flash(rows)
        s.upsert_universe(uni)

    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_flash.py"),
                        "--db", str(db), "--out", str(out),
                        "--verify", str(tmp / "none.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    p = json.loads((out / "flash.json").read_text(encoding="utf-8"))

    assert p["asOf"]["period"] == "2026-09" and p["asOf"]["seq"] == 1
    assert p["leadDays"] == 35, "상순 기준이면 확정치보다 35일 앞선다"
    assert len(p["items"]) == 10, [i["label"] for i in p["items"]]
    assert p["countries"] == [], "국가 슬롯이 미검증인데 화면 데이터에 들어갔다"
    assert p["countryCollected"] is True, "수집은 해 두어야 한다"
    assert p["reconcile"] and p["reconcile"]["ok"], p["reconcile"]
    assert abs(p["reconcile"]["gapPct"] - 1.01) < 0.2, p["reconcile"]
    keys = {x["key"] for x in p["ratios"]}
    assert {"auto_loc", "mem_ssd"} <= keys, keys
    # 점유율 변화 내림차순
    sc = [i["shareChgPp"] for i in p["items"] if i["shareChgPp"] is not None]
    assert sc == sorted(sc, reverse=True), sc
    print(f"  ✓ 빌드 — {p['asOf']['period']} {p['asOf']['label']}, 품목 {len(p['items'])}개, "
          f"정합성 {p['reconcile']['gapPct']:+.2f}%")


def test_every_step_is_time_boxed():
    """단계마다 타임아웃이 있어야 한다.

    회귀 방지: 첫 실행이 잡 한도 25분을 넘겨 **통째로 취소**됐고, 그러면 어느
    단계가 시간을 먹었는지 로그에 남지 않는다. 단계별로 끊어 두면 범인이
    이름으로 찍히고 뒤 단계는 계속 돈다.
    """
    import yaml
    d = yaml.safe_load((ROOT / ".github" / "workflows" / "flash.yml").read_text(encoding="utf-8"))
    job = d["jobs"]["flash"]
    steps = [s for s in job["steps"] if "run" in s]
    missing = [s.get("name", "?") for s in steps if not s.get("timeout-minutes")]
    assert not missing, f"타임아웃 없는 실행 단계: {missing}"
    total = sum(s["timeout-minutes"] for s in steps)
    cap = job["timeout-minutes"]
    assert total < cap, f"단계 합계 {total}분이 잡 한도 {cap}분 이상 — 잡이 먼저 죽는다"

    # 국가 검증은 응답 크기가 예측되지 않는 유일한 호출이다. 예약 실행에서 기본 off.
    # YAML 1.1 함정 재등장 — `on:` 키가 boolean True 로 파싱된다.
    # (config/chains.yaml 의 국가코드 NO→false 와 같은 뿌리다)
    on = d.get("on", d.get(True))
    inp = on["workflow_dispatch"]["inputs"]["verify_countries"]
    assert inp["default"] is False, "국가 검증이 기본 on 이면 매 예약 실행이 위험해진다"

    up = yaml.safe_load((ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    fl = next(s for s in up["jobs"]["collect"]["steps"]
              if "속보" in (s.get("name") or ""))
    assert fl.get("continue-on-error") is True, \
        "월별 확정 파이프라인이 속보 실패로 죽으면 안 된다"
    print(f"  ✓ 단계 시간 상자 — 합계 {total}분 < 잡 {cap}분, 국가검증 기본 off, 월별과 격리")


def test_client_honors_time_budget():
    """예산을 넘기면 스스로 멈춰야 한다. 안 멈추면 CI 잡이 대신 죽는다."""
    from kortrade.client import CustomsClient, CustomsAPIError
    c = CustomsClient(service_key="x")
    assert c.budget_left() is None and not c.out_of_budget()
    c.set_budget(0)
    assert c.out_of_budget()
    try:
        c.call("flash_item", strtYymm="202601", endYymm="202601")
    except CustomsAPIError as exc:
        assert "예산" in str(exc) and exc.permanent, exc
    else:
        raise AssertionError("예산이 소진됐는데 호출을 시도했다")
    assert c.calls_made == 0, "예산 소진 상태에서 네트워크를 건드렸다"

    run = (ROOT / "scripts" / "run_flash.py").read_text(encoding="utf-8")
    assert "프로브" in run, "본 수집 전에 1개월 프로브로 응답 속도를 재야 한다"
    assert "--budget-seconds" in run and "--window-months" in run
    print("  ✓ 시간 예산 — 소진 시 호출 없이 중단, 프로브로 응답 속도 선측정")


def test_wired_into_automation():
    """자동 갱신에 붙어 있어야 한다. 붙지 않은 레이어는 다음 달이면 죽은 데이터가 된다."""
    fl = (ROOT / ".github" / "workflows" / "flash.yml").read_text(encoding="utf-8")
    assert "1,11,21" in fl, "1·11·21일 예약이 없다 — 속보 공표 주기와 맞지 않는다"
    assert "run_flash.py" in fl and "build_flash.py" in fl
    assert "group: trade-update" in fl, \
        "월별 워크플로와 같은 concurrency 그룹이어야 한다(같은 DB·같은 브랜치)"
    up = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    assert "build_flash.py" in up, "월별 실행에서도 속보를 맞춰야 한다"
    for wf in (fl, up):
        assert "3028661a" not in wf, "인증키가 워크플로에 박혀 있다"
        assert "secrets.DATA_GO_KR_SERVICE_KEY" in wf
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert "data/flash.json" in html and "renderFlash" in html
    print("  ✓ 자동화 — 1·11·21일 예약, 같은 concurrency 그룹, 키는 시크릿")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\n속보 레이어 검증 — {len(tests)}개\n")
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
