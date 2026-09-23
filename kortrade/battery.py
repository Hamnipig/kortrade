"""2차전지·ESS 심화 레이어.

밸류체인 레이어가 답하지 못하는 두 가지를 다룬다.

1. **마진이 어디로 가는가** — 양극재 판가(P)는 리튬 가격(C)에 시차를 두고 연동된다.
   원가는 수출이 아니라 **수입** 데이터에 있고, 우리가 쓰는 품목별 API 는
   impDlr/impWgt 를 같이 준다. 그동안 한 번도 쓰지 않았던 절반이다.

       P(t) = α + β·C(t−L) + e(t)

   L 은 0~4개월을 훑어 R² 가 가장 높은 값으로 **측정**한다. 리튬 원단위(kg/kg)를
   상수로 박지 않는 이유가 여기 있다 — 출처 없는 상수를 넣으면 그 가정이 결과를
   만든다. β 는 실측 전가율이고, 잔차 e(t) 가 '원가로 설명되지 않는 판가',
   즉 **마진 프록시**다. e 가 오르면 마진이 벌어지는 중이다.

2. **지금 사이클을 끌고 있는 단계가 어디인가** — 장비(CAPEX) → 소재 → 완제품
   순서로 물결이 지나간다. 단계 간 시차 상관을 재면 현재 선행 단계가 보인다.

두 가지 모두 '금액이 늘었다/줄었다'보다 한 겹 아래를 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .flash import ols, predict          # 회귀는 속보 레이어와 같은 구현을 쓴다

CONFIG = Path(__file__).resolve().parent.parent / "config" / "battery.yaml"

STAGES = ("final", "component", "material", "equipment")
# 수요 합계에 넣는 단계. 장비는 설비투자라 수요의 흐름이 아니다 — 섞으면 둘 다 흐려진다.
DEMAND_STAGES = ("final", "component", "material")

# 턴어라운드 판정 문턱 — 최근 3개월이 이만큼 살아나야 '전환'으로 본다
TURN_RECENT_MIN = 5.0     # %
# 가속 판정 문턱
ACCEL_MIN = 10.0          # %p


@dataclass
class Code:
    code: str
    label: str
    status: str = "draft"          # active | draft
    purity: str = ""               # high | medium | low | unknown
    note: str = ""
    evidence: str = ""
    for_: str = ""                 # cost 전용: 어느 조성용인가

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def hs6(self) -> str:
        return self.code[:6]


@dataclass
class BatteryConfig:
    version: str
    window: int = 8
    recent: int = 3
    hist: int = 30
    max_lag_months: int = 4
    min_months: int = 12
    min_r2: float = 0.25
    leadlag_max_months: int = 6
    markets: list[str] = field(default_factory=list)
    cost: list[Code] = field(default_factory=list)
    price: list[Code] = field(default_factory=list)
    stages: dict[str, list[Code]] = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------ 코드 모음

    def stage_codes(self, stage: str, active_only: bool = False) -> list[str]:
        return [c.code for c in self.stages.get(stage, [])
                if (c.active or not active_only)]

    def all_codes(self) -> list[str]:
        out = [c.code for c in self.cost] + [c.code for c in self.price]
        for s in STAGES:
            out += [c.code for c in self.stages.get(s, [])]
        return sorted(set(out))

    def all_parents(self) -> list[str]:
        return sorted({c[:6] for c in self.all_codes()})

    def label(self, code: str) -> str:
        for c in self.cost + self.price + [x for s in STAGES for x in self.stages.get(s, [])]:
            if c.code == code:
                return c.label
        return code

    def find(self, code: str) -> Code | None:
        for c in self.cost + self.price + [x for s in STAGES for x in self.stages.get(s, [])]:
            if c.code == code:
                return c
        return None

    def drafts(self) -> list[Code]:
        seen, out = set(), []
        for c in self.cost + self.price + [x for s in STAGES for x in self.stages.get(s, [])]:
            if not c.active and c.code not in seen:
                seen.add(c.code)
                out.append(c)
        return out

    # ------------------------------------------------------------ 검증

    def validate(self) -> list[str]:
        errs: list[str] = []
        for c in self.cost + self.price + [x for s in STAGES for x in self.stages.get(s, [])]:
            if not (c.code.isdigit() and len(c.code) == 10):
                errs.append(f"'{c.code}'({c.label}) 는 10자리 HSK 가 아니다")
            if c.status not in ("active", "draft"):
                errs.append(f"'{c.code}' status 는 active|draft 여야 한다 (현재 {c.status!r})")
        for s in self.stages:
            if s not in STAGES:
                errs.append(f"알 수 없는 단계 '{s}' (허용: {STAGES})")
        if not self.cost:
            errs.append("cost(수입) 코드가 없다 — 마진 프록시를 만들 수 없다")
        if not self.price:
            errs.append("price(양극재 수출) 코드가 없다")
        if not self.stage_codes("final"):
            errs.append("final(완제품) 단계가 비어 있다")
        for m in self.markets:
            # YAML 1.1 이 NO/ON/OFF 를 불리언으로 읽는다. 따옴표가 빠지면 여기서 잡힌다.
            if not isinstance(m, str) or len(m) != 2 or not m.isupper():
                errs.append(f"시장코드 {m!r} 형식 오류 (따옴표 누락 의심)")
        # 원가 자리에 수출 코드를 넣는 사고를 막는다 (사용자 제시 목록의 실제 오류)
        price_codes = {c.code for c in self.price}
        for c in self.cost:
            if c.code in price_codes:
                errs.append(f"'{c.code}' 가 cost 와 price 양쪽에 있다 — "
                            f"원가 자리에 판가를 넣으면 스프레드 부호가 뒤집힌다")
        return errs


def _codes(raw) -> list[Code]:
    out = []
    for d in (raw or []):
        d = dict(d)
        d["for_"] = d.pop("for", "")
        d["code"] = str(d.get("code", ""))
        for k in ("note", "evidence"):
            d[k] = " ".join(str(d.get(k, "")).split())
        out.append(Code(**{k: v for k, v in d.items() if k in Code.__annotations__}))
    return out


def load(path: Path | None = None) -> BatteryConfig:
    cfg = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8"))
    sp = cfg.get("spread") or {}
    return BatteryConfig(
        version=str(cfg.get("version", "")),
        window=int(cfg.get("window", 8)),
        recent=int(cfg.get("recent", 3)),
        hist=int(cfg.get("hist", 30)),
        max_lag_months=int(sp.get("max_lag_months", 4)),
        min_months=int(sp.get("min_months", 12)),
        min_r2=float(sp.get("min_r2", 0.25)),
        leadlag_max_months=int(cfg.get("leadlag_max_months", 6)),
        markets=list(cfg.get("markets") or []),
        cost=_codes(cfg.get("cost")),
        price=_codes(cfg.get("price")),
        stages={s: _codes((cfg.get("stages") or {}).get(s)) for s in STAGES},
        rejected=list(cfg.get("rejected") or []),
    )


# ------------------------------------------------------------------ 지표

def unit_price(usd: float | None, wgt: float | None,
               min_wgt: float = 1_000.0) -> float | None:
    """$/kg. 중량이 너무 작으면 단가가 폭주하므로 내지 않는다."""
    if not usd or not wgt or wgt < min_wgt:
        return None
    return usd / wgt


def corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 6 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    if da == 0 or db == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db)


def growth(series: list[float | None]) -> list[float | None]:
    """전월비. None 이 섞여도 자리를 유지한다 (시차 정렬이 어긋나면 안 된다)."""
    out: list[float | None] = [None]
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        out.append((b / a - 1) if (a and b and a > 0) else None)
    return out


def _diff(series: dict[str, float], shift_fn) -> dict[str, float]:
    """전월 대비 변화량. 시차 식별은 **반드시 차분으로** 해야 한다."""
    out = {}
    for p in sorted(series):
        q = shift_fn(p, -1)
        if q in series:
            out[p] = series[p] - series[q]
    return out


# 최적 시차의 차분 R² 가 차순위보다 이만큼은 높아야 '시차가 식별됐다'고 본다.
LAG_MARGIN = 0.05


def best_lag(y: dict[str, float], x: dict[str, float], shift_fn,
             max_lag: int, min_months: int) -> dict | None:
    """y(t) ~ x(t−L) 의 최적 시차 L 을 찾고, 그 L 로 **수준(level)** 회귀를 돌린다.

    ★ 시차는 수준이 아니라 **차분**으로 찾는다.
      수준끼리는 둘 다 추세를 갖고 있어 어느 시차를 넣어도 R² 가 비슷하게 높다.
      그러면 '시차 0개월'이라는 결론이 발견이 아니라 계산의 부산물이 된다.
      (실측 확인: 리튬 단가가 매끄러운 U자일 때 수준 회귀는 진짜 시차 2를 놓치고
       0을 골랐다.) 차분은 추세를 제거하므로 시차 정보만 남는다.

    수준 회귀를 따로 돌리는 이유는 잔차를 $/kg 단위로 읽어야 하기 때문이다 —
    마진 프록시는 '판가가 원가 대비 얼마나 벌어졌나'를 금액으로 말해야 쓸모가 있다.

    반환값에 모든 L 의 차분 R² 곡선(curve)과 identified 플래그를 담는다.
    곡선이 평탄하면 시차를 못 찾은 것이고, 그 사실을 화면에 적어야 한다.
    """
    dy, dx = _diff(y, shift_fn), _diff(x, shift_fn)
    curve, best_l, best_dr2 = [], None, None
    for lag in range(max_lag + 1):
        pairs = [(dx[shift_fn(p, -lag)], dy[p]) for p in sorted(dy)
                 if shift_fn(p, -lag) in dx]
        fit = ols([p[0] for p in pairs], [p[1] for p in pairs]) \
            if len(pairs) >= min_months else None
        r2 = None if not fit else fit["r2"]
        curve.append({"lag": lag, "n": len(pairs),
                      "r2": None if r2 is None else round(r2, 3)})
        if r2 is not None and (best_dr2 is None or r2 > best_dr2):
            best_l, best_dr2 = lag, r2
    if best_l is None:
        return None

    others = [c["r2"] for c in curve if c["r2"] is not None and c["lag"] != best_l]
    identified = bool(others) and (best_dr2 - max(others)) >= LAG_MARGIN

    # 고른 시차로 수준 회귀
    pairs = [(x[shift_fn(p, -best_l)], y[p]) for p in sorted(y)
             if shift_fn(p, -best_l) in x and x[shift_fn(p, -best_l)] > 0 and y[p] > 0]
    if len(pairs) < min_months:
        return None
    fit = ols([p[0] for p in pairs], [p[1] for p in pairs])
    if not fit:
        return None
    return {"lag": best_l, "fit": fit, "pairs": pairs, "curve": curve,
            "diffR2": round(best_dr2, 3), "identified": identified}


def verdict(yoy: float | None, recent_yoy: float | None) -> dict:
    """성장/가속/턴어라운드 판정.

    '가속'만으로 판단하지 않는다. 기저가 낮으면 가속은 얼마든지 만들어진다.
    **부호가 바뀌었는지**(턴어라운드)와 **속도가 붙었는지**(가속)를 따로 말한다.
    """
    if yoy is None or recent_yoy is None:
        return {"code": "unknown", "label": "판정 불가",
                "note": "전년 동기 데이터가 부족합니다."}
    accel = recent_yoy - yoy
    if yoy < 0 and recent_yoy >= TURN_RECENT_MIN:
        return {"code": "turnaround", "label": "턴어라운드",
                "note": f"8개월 기준은 아직 {yoy:+.0f}% 지만 최근 3개월이 "
                        f"{recent_yoy:+.0f}% 로 부호가 바뀌었습니다."}
    if yoy < 0 and accel >= ACCEL_MIN:
        return {"code": "bottoming", "label": "감속 둔화",
                "note": f"여전히 마이너스({yoy:+.0f}%)지만 낙폭이 "
                        f"{accel:+.0f}%p 줄었습니다. 바닥 통과 가능성."}
    if yoy < 0:
        return {"code": "contracting", "label": "위축",
                "note": f"8개월 {yoy:+.0f}% · 최근 3개월 {recent_yoy:+.0f}%."}
    if accel >= ACCEL_MIN:
        return {"code": "accelerating", "label": "가속",
                "note": f"최근 3개월 {recent_yoy:+.0f}% 로 8개월 기준({yoy:+.0f}%)보다 "
                        f"{accel:+.0f}%p 빠릅니다."}
    if accel <= -ACCEL_MIN:
        return {"code": "decelerating", "label": "감속",
                "note": f"성장 중이나 최근 3개월이 {accel:+.0f}%p 느려졌습니다."}
    return {"code": "steady", "label": "유지",
            "note": f"8개월 {yoy:+.0f}% · 최근 3개월 {recent_yoy:+.0f}%."}


def margin_verdict(e_now: float | None, e_3m_ago: float | None,
                   c_mom3: float | None) -> dict:
    """마진 프록시(회귀 잔차)의 방향 + 리튬 단가 모멘텀.

    양극재는 판가 연동 + 재고평가 구조라, **리튬이 오르면 마진이 좋아진다.**
    (셀 메이커는 기제가 반대일 수 있어 이 판정은 양극재에만 쓴다.)
    """
    if e_now is None or e_3m_ago is None:
        return {"code": "unknown", "label": "판정 불가",
                "note": "원가-판가 연동이 확인되지 않아 마진 방향을 내지 않습니다."}
    d = e_now - e_3m_ago
    li = "" if c_mom3 is None else (
        f" 리튬 수입단가는 3개월간 {c_mom3:+.1f}%." )
    if d > 0:
        return {"code": "expanding", "label": "마진 개선",
                "note": f"원가로 설명되지 않는 판가(스프레드)가 3개월간 "
                        f"{d:+.2f}$/kg 벌어졌습니다.{li}"}
    if d < 0:
        return {"code": "compressing", "label": "마진 압박",
                "note": f"스프레드가 3개월간 {d:+.2f}$/kg 좁혀졌습니다.{li}"}
    return {"code": "flat", "label": "보합", "note": f"스프레드 변화가 미미합니다.{li}"}
