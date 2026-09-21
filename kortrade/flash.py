"""속보(旬 단위 잠정치) 레이어.

관세청은 같은 달 수출을 네 번 발표한다.

    01~10일 누계 → 11일경
    01~20일 누계 → 21일경
    01~말일 누계 → 익월 1일경      ← 월전체 '잠정치'
    월별 확정치  → 익월 15일경      ← 기존 월별 레이어

이 모듈은 앞의 세 개를 다룬다. **월전체 잠정치만으로도 확정치보다 15일 빠르고,
상순 누계까지 쓰면 35일 빠르다.** 분기 실적 발표는 75일 뒤에 나온다.

설계상 조심해야 할 것 세 가지 — 모두 실측으로 확인했다.

1. 값은 **1일부터의 누계**다. 11~20일만 보려면 (01~20) − (01~10) 을 직접 빼야 한다.
2. 단위는 **천 달러**. 품목별 API(달러)와 다르다. 저장 전에 달러로 맞춘다.
3. 누계 YoY 는 **조업일수 차이를 포함한다.** 이 API 는 조업일수를 주지 않고,
   공휴일은 해마다 날짜가 바뀐다. 우리가 정확히 셀 수 있는 것은 평일수뿐이다.
   → 조업일수에 중립인 축은 **점유율(share)** 이다. 같은 순(旬) 안에서는 모든
     품목이 같은 날짜를 공유하므로 비중과 그 변화(%p)는 일수의 영향을 받지 않는다.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "flash.yaml"

# 원본 단위(천 달러) → 달러
UNIT_USD = 1_000

SEQ_LABEL = {1: "1~10일", 2: "1~20일", 3: "월전체"}
SEQ_SPAN = {1: "상순", 2: "상·중순", 3: "월전체(잠정)"}


# ------------------------------------------------------------------ 기간 헬퍼

def seq_of(day_to: int) -> int:
    """마지막 날짜 → 순번. 2월은 '01~28', 4월은 '01~30' 이므로 날짜로 직접 비교하면 안 된다."""
    if day_to <= 10:
        return 1
    if day_to <= 20:
        return 2
    return 3


def parse_dt(raw: str) -> tuple[int, int]:
    """'01~10' -> (10, 1).  숫자가 아니면 (0, 0)."""
    s = (raw or "").strip()
    digits = [d for d in "".join(ch if ch.isdigit() else " " for ch in s).split() if d]
    if len(digits) < 2:
        return 0, 0
    day_to = int(digits[-1])
    return day_to, seq_of(day_to)


def weekdays(year: int, month: int, day_to: int) -> int:
    """해당 월 1일~day_to 사이의 평일(월~금) 수.

    ※ **공휴일은 반영하지 못한다.** 이 API 도, 우리가 쓰는 다른 관세청 API 도
      조업일수를 주지 않는다. 설·추석·대체공휴일은 해마다 날짜가 바뀌므로
      외부 공휴일표 없이는 정확히 셀 수 없고, 출처 없는 표를 코드에 박아 넣느니
      '평일 기준'이라고 명시하고 한계를 화면에 적는 편이 낫다.
      명절이 낀 순(旬)에서는 이 값으로 만든 일평균도 여전히 왜곡된다.
    """
    last = calendar.monthrange(year, month)[1]
    end = min(day_to, last)
    return sum(1 for d in range(1, end + 1)
               if date(year, month, d).weekday() < 5)


def shift(period: str, months: int) -> str:
    """'2026-09' + (-12) -> '2025-09'"""
    y, m = int(period[:4]), int(period[5:7])
    t = y * 12 + (m - 1) + months
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


# ------------------------------------------------------------------ 기간 검증
#
# ★ 회귀 방지 — 실측 사고(2026-09-21)
#   응답의 priodMon 에 달(月)이 아닌 값이 섞여 들어와 period='YYYY-20' 같은 행이
#   저장됐고, 빌드가 calendar.monthrange(2026, 20) 에서 죽었다.
#   교훈 둘:
#     (1) **달력에 없는 달은 애초에 저장하지 않는다.** 걸러내는 자리는 수집기다.
#     (2) 화면 빌드는 이상한 행 하나 때문에 통째로 죽으면 안 된다. 건너뛰고 알린다.

def parse_period(year, month) -> str | None:
    """('2026','08') -> '2026-08'. 달력에 없는 값이면 None."""
    try:
        y, m = int(str(year).strip()), int(str(month).strip())
    except (TypeError, ValueError):
        return None
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        return None
    return f"{y:04d}-{m:02d}"


def split_period(period: str) -> tuple[int, int] | None:
    """'2026-08' -> (2026, 8). 형식이 깨졌거나 달이 1~12 밖이면 None."""
    s = (period or "").strip()
    if len(s) < 7 or s[4] != "-":
        return None
    try:
        y, m = int(s[:4]), int(s[5:7])
    except ValueError:
        return None
    return (y, m) if 1 <= m <= 12 else None


# ------------------------------------------------------------------ 지표

def pct(now: float | None, prev: float | None, min_base: float = 0.0) -> float | None:
    """전년 대비 증감률(%). 기준이 너무 작으면 None — 작은 분모는 숫자를 만들어낸다."""
    if now is None or prev is None:
        return None
    if prev <= 0 or prev < min_base:
        return None
    return round((now / prev - 1) * 100, 1)


def share(part: float | None, total: float | None) -> float | None:
    if not total or part is None or total <= 0:
        return None
    return round(part / total * 100, 2)


def landing_simple(cum_now: float | None, cum_prev: float | None,
                   full_prev: float | None, min_base: float = 0.0) -> float | None:
    """월 착지 추정(단순) = 올해 누계 × (작년 월전체 ÷ 작년 같은 순 누계).

    ※ 이 방식의 착지 추정은 **전년 대비 증가율이 누계 YoY 와 정확히 같다.**
         est / full_prev − 1 = cum_now / cum_prev − 1
       금액 감각을 주는 데는 쓸모가 있지만, '누계 YoY 와 다른 두 번째 증가율'로
       내보내면 없는 정보를 있는 것처럼 보여주는 셈이 된다. 그래서 화면의
       기본 추정은 아래 landing_wd 를 쓴다.
    """
    if cum_now is None or not cum_prev or not full_prev:
        return None
    if cum_prev <= 0 or cum_prev < min_base:
        return None
    return round(cum_now * (full_prev / cum_prev))


def landing_wd(cum_now: float | None, cum_prev: float | None, full_prev: float | None,
               wd_now: int | None, wd_prev: int | None,
               wd_full_now: int | None, wd_full_prev: int | None,
               min_base: float = 0.0) -> float | None:
    """월 착지 추정(평일 보정). 남은 기간을 **올해의 평일 수**로 채운다.

        ρ   = (올해 누계 ÷ 올해 평일수) ÷ (작년 같은 순 누계 ÷ 작년 평일수)
              → 일당 판매속도의 전년비. 주말 배치 차이가 제거된 값이다.
        추정 = 올해 누계 + ρ × (작년 잔여기간 일당) × (올해 잔여 평일수)

    단순 비례식과 달리, 이번 달 하순에 평일이 몇 개 남았는지를 반영한다.
    그래서 이 추정의 YoY 는 누계 YoY 와 **다른 숫자**가 되고, 그 차이가 곧
    '조업일수 때문에 생긴 착시'의 크기다.

    한계는 그대로다 — 공휴일은 못 센다. 명절이 낀 달에는 여전히 흔들린다.
    """
    simple = landing_simple(cum_now, cum_prev, full_prev, min_base)
    if simple is None:
        return None
    if None in (wd_now, wd_prev, wd_full_now, wd_full_prev):
        return simple
    if not wd_now or not wd_prev:
        return simple
    rem_now = wd_full_now - wd_now
    rem_prev = wd_full_prev - wd_prev
    if rem_now <= 0:
        return round(cum_now)          # 월전체가 이미 나왔다 — 추정할 것이 없다
    if rem_prev <= 0 or full_prev <= cum_prev:
        return simple
    rho = (cum_now / wd_now) / (cum_prev / wd_prev)
    return round(cum_now + rho * ((full_prev - cum_prev) / rem_prev) * rem_now)


def shape_ratio(cum_prev: float | None, full_prev: float | None) -> float | None:
    """작년 같은 순 누계가 작년 월전체의 몇 %였나. 착지 추정의 배율이자 신뢰도 힌트."""
    if not cum_prev or not full_prev or full_prev <= 0:
        return None
    return round(cum_prev / full_prev * 100, 1)


# ------------------------------------------------------------------ 설정 로더

@dataclass
class Slot:
    slot: str
    label: str
    total: bool = False
    hs: str = ""
    tickers: list[str] = field(default_factory=list)
    note: str = ""
    watch: list[str] = field(default_factory=list)
    chain: str = ""
    stage: str = ""
    code: str = ""
    lumpy: bool = False


@dataclass
class SlotSet:
    kind: str                       # 'item' | 'country'
    verified: bool
    verified_note: str
    slots: list[Slot]

    @property
    def total_slot(self) -> str:
        for s in self.slots:
            if s.total:
                return s.slot
        return "00"

    def body(self) -> list[Slot]:
        return [s for s in self.slots if not s.total]

    def label(self, slot: str) -> str:
        for s in self.slots:
            if s.slot == slot:
                return s.label
        return slot


@dataclass
class Ratio:
    key: str
    name: str
    num: str
    den: str
    rising_means: str = ""
    note: str = ""


@dataclass
class FlashConfig:
    version: str
    min_base_usd: float
    reconcile_tolerance_pct: float
    item: SlotSet
    country: SlotSet
    ratios: list[Ratio]

    def sets(self) -> dict[str, SlotSet]:
        return {"item": self.item, "country": self.country}

    def validate(self) -> list[str]:
        errs: list[str] = []
        for kind, ss in self.sets().items():
            seen = [s.slot for s in ss.slots]
            if len(seen) != len(set(seen)):
                errs.append(f"{kind}: 슬롯 번호 중복 {sorted(seen)}")
            for s in ss.slots:
                if not (len(s.slot) == 2 and s.slot.isdigit()):
                    errs.append(f"{kind}: 슬롯 키 '{s.slot}' 는 '00'~'10' 두 자리여야 한다")
            if sum(1 for s in ss.slots if s.total) != 1:
                errs.append(f"{kind}: total 슬롯이 정확히 하나여야 한다")
        known = {s.slot for s in self.item.slots}
        for r in self.ratios:
            for side in (r.num, r.den):
                if side not in known:
                    errs.append(f"ratio '{r.key}': 알 수 없는 슬롯 '{side}'")
        return errs


def _slotset(kind: str, d: dict) -> SlotSet:
    slots = []
    for k, raw in sorted((d.get("slots") or {}).items()):
        kw = {kk: vv for kk, vv in dict(raw or {}).items() if kk in Slot.__annotations__}
        kw["slot"] = str(k)
        kw.setdefault("label", str(k))
        kw["note"] = " ".join(str(kw.get("note", "")).split())
        slots.append(Slot(**kw))
    return SlotSet(kind=kind, verified=bool(d.get("verified")),
                   verified_note=" ".join(str(d.get("verified_note", "")).split()),
                   slots=slots)


def load(path: Path | None = None) -> FlashConfig:
    cfg = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8"))
    return FlashConfig(
        version=str(cfg.get("version", "")),
        min_base_usd=float(cfg.get("min_base_usd", 0)),
        reconcile_tolerance_pct=float(cfg.get("reconcile_tolerance_pct", 5.0)),
        item=_slotset("item", cfg.get("item") or {}),
        country=_slotset("country", cfg.get("country") or {}),
        ratios=[Ratio(**{k: v for k, v in r.items() if k in Ratio.__annotations__})
                for r in (cfg.get("ratios") or [])],
    )
