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
