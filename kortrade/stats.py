"""회귀·상관 — 레이어들이 공유하는 통계 도구.

★ 왜 따로 떼어 놓았나 (2026-09-23)
  처음엔 ols/predict 를 flash.py 에 두고 battery.py 가 거기서 import 했다.
  두 가지가 나빴다.
    1. 의미상 이상하다 — 배터리 레이어가 속보 레이어에 의존할 이유가 없다.
    2. 실제로 깨졌다 — battery.py 만 배포하고 flash.py 를 빼먹자
       `ImportError: cannot import name 'ols' from 'kortrade.flash'` 로
       수집이 통째로 멈췄다.
  공용 코드는 공용 자리에 둔다. 어느 레이어도 다른 레이어를 import 하지 않는다.
"""

from __future__ import annotations


def ols(xs: list[float], ys: list[float]) -> dict | None:
    """단순회귀 y = α + βx. 예측구간을 내는 데 필요한 값까지 함께 돌려준다."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    xb, yb = sum(xs) / n, sum(ys) / n
    sxx = sum((x - xb) ** 2 for x in xs)
    if sxx <= 0:
        return None
    beta = sum((x - xb) * (y - yb) for x, y in zip(xs, ys)) / sxx
    alpha = yb - beta * xb
    sse = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    sst = sum((y - yb) ** 2 for y in ys)
    return {"alpha": alpha, "beta": beta, "n": n,
            "r2": (1 - sse / sst) if sst > 0 else 0.0,
            "s": (sse / (n - 2)) ** 0.5, "xbar": xb, "sxx": sxx}


def predict(fit: dict, x: float) -> tuple[float, float]:
    """(예측값, 예측 표준오차). 관측 잡음과 계수 불확실성을 모두 넣는다."""
    yhat = fit["alpha"] + fit["beta"] * x
    se = fit["s"] * (1 + 1 / fit["n"] + (x - fit["xbar"]) ** 2 / fit["sxx"]) ** 0.5
    return yhat, se


def corr(a: list[float], b: list[float], min_n: int = 6) -> float | None:
    """피어슨 상관. 표본이 너무 적으면 내지 않는다 — 적은 표본의 상관은 숫자를 만들어낸다."""
    n = len(a)
    if n < min_n or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    if da == 0 or db == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db)


# ══════════════════════════════════════════════════════════════════════════
# 시계열 도구 — 원래 battery.py 에 있었다. PCB 레이어가 같은 것을 필요로 해서
# 공용 자리로 옮긴다. battery.py 는 여기서 다시 export 하므로 호출부는 그대로다.
#
# 레이어가 레이어를 import 하면 안 되는 이유는 이 파일 맨 위에 적어 두었다.
# pcb.py 가 battery.py 에서 best_lag 를 가져가는 순간 같은 사고가 반복된다.
# ══════════════════════════════════════════════════════════════════════════


def unit_price(usd: float | None, wgt: float | None,
               min_wgt: float = 1_000.0) -> float | None:
    """$/kg. 중량이 너무 작으면 단가가 폭주하므로 내지 않는다."""
    if not usd or not wgt or wgt < min_wgt:
        return None
    return usd / wgt


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
