"""P/Q 분해 — 금액 증가를 **단가**와 **물량**으로 쪼갠다.

왜 필요한가 (2026-09-23)
  이 프로젝트는 지금까지 **금액만** 봤다. 그런데 같은 '성장'이 전혀 다른 것일 수 있다.

    2026-08 기초화장품(3304991000)  금액 +49.4%  물량 +15.7%  단가 26.2→33.8$/kg (+29.0%)
    2026-08 메이크업 (3304992000)  금액 + 9.9%  물량 + 5.8%  단가 51.2→53.2$/kg (+ 3.9%)
      (출처: 뷰티경제 2026-09-21/22, 관세청 수출입무역통계 기준)

  금액만 보면 "둘 다 성장"이다. 단가를 넣으면 기초는 성장의 2/3 가 **단가**에서 나온
  프리미엄화이고 메이크업은 사실상 물량이다. 국가별로는 더 갈린다 —
  같은 기초화장품인데 **미국은 금액 +22.2% 인데 단가가 28.2→27.3 으로 하락**했고,
  **일본은 +92.2% 에 단가 +42.6%** 다. 미국은 밀어내기, 일본은 프리미엄 침투다.
  금액 증가율만 보는 대시보드에서는 이 둘이 같은 칸에 들어간다.

  ★ 투자 판단이 갈리는 지점이 정확히 여기다. 밀어내기는 다음 분기 마진과 재고로 돌아온다.

데이터 근거
  단가는 **국가 축(sector_trade)에서만** 나온다. 시군구 API(sigunguperprlstperacrs)는
  중량 필드가 아예 없고 HS 6단위만 받는다. 국가별 API(nitemtrade)는 expWgt 를 주고,
  우리는 이미 그걸 수집해 DB 에 넣고 있다 — **추가 API 호출 0건.**

  (참고: 2026-09-01 관세청 지침으로 시군구×HSK10 전면 비공개, 시군구 중량 비공개.
   우리는 둘 다 쓰지 않던 것이라 직접 타격은 없다. 다만 정책 방향이 축소 쪽이므로
   시군구 축에 무게를 더 싣지 않는다.)

분해 방식 — 왜 로그인가
    V = P x Q      (금액 = 단가 x 물량)
    ln(V1/V0) = ln(P1/P0) + ln(Q1/Q0)

  로그로 가야 **기여도가 정확히 가법적**이다. 단순 %로 쪼개면
  (1+v) = (1+p)(1+q) 의 교차항 p·q 가 남아 기여도 합이 100%가 안 된다.
  기초화장품처럼 +49% 짜리 큰 변화에서는 교차항이 4%p 를 넘어 무시할 수 없다.
"""

from __future__ import annotations

import math

# 중량이 이보다 작으면 단가를 계산하지 않는다. 샘플·소량 선적에서
# $/kg 가 수백 달러로 튀어 국면 판정을 통째로 뒤집는다.
MIN_WGT_KG = 1_000.0

# ±이 값 이내의 변화는 '보합'으로 본다. 부호만 보면 노이즈가 국면이 된다.
# 3%는 월별 단가의 통상 변동폭을 기준으로 잡았다 — 메이크업 8월 단가 +3.9%가
# '상승'으로 잡히되 '강한 상승'은 아닌 수준.
FLAT_PCT = 3.0

# 금액 변화가 이보다 작으면 기여도(share) 를 계산하지 않는다.
# 분모 ln(V1/V0) 가 0 에 가까우면 기여도가 ±수천 %로 발산한다.
MIN_VALUE_MOVE_PCT = 1.0

_LABELS = {
    (+1, +1): ("premium_expansion", "프리미엄 확산",
               "물량과 단가가 함께 올랐습니다. 가장 좋은 조합입니다 — "
               "가격을 올리고도 더 팔렸다는 뜻이므로 브랜드력·믹스 개선을 의심할 근거가 됩니다."),
    (+1, 0): ("volume_growth", "물량 성장",
              "단가는 그대로고 물량이 늘었습니다. 침투가 진행 중이지만 "
              "가격 결정력은 아직 확인되지 않습니다."),
    (+1, -1): ("push", "밀어내기",
               "물량은 늘었는데 단가가 떨어졌습니다. **마진 경고** — 채널 확대나 "
               "프로모션으로 매출을 만든 국면일 수 있습니다. 다음 분기 매출총이익률과 "
               "재고자산회전율을 함께 보십시오."),
    (0, +1): ("price_led", "단가 주도",
              "물량은 제자리인데 단가가 올랐습니다. 믹스 개선이거나 판가 인상입니다 — "
              "어느 쪽인지는 품목 구성으로 갈라야 합니다."),
    (0, 0): ("flat", "보합", "물량·단가 모두 큰 변화가 없습니다."),
    (0, -1): ("price_erosion", "단가 잠식",
              "물량은 제자리인데 단가가 내렸습니다. 경쟁 심화나 저가 믹스 확대를 의심합니다."),
    (-1, +1): ("mix_up", "물량 감소·단가 상승",
               "물량이 줄면서 단가가 올랐습니다. 저가 물량을 정리하고 있거나 "
               "채널을 정돈하는 국면일 수 있습니다 — 금액이 유지되는지가 갈림길입니다."),
    (-1, 0): ("volume_decline", "물량 감소",
              "단가는 버티는데 물량이 줄었습니다. 수요 둔화 쪽을 먼저 봅니다."),
    (-1, -1): ("contracting", "수축",
               "물량과 단가가 함께 빠졌습니다. 가장 나쁜 조합입니다 — "
               "가격을 낮췄는데도 덜 팔렸다는 뜻입니다."),
}


def asp(usd: float | None, wgt: float | None, min_wgt: float = MIN_WGT_KG) -> float | None:
    """평균 수출단가 ($/kg). 중량이 없거나 너무 작으면 None."""
    if not usd or not wgt or wgt < min_wgt:
        return None
    return usd / wgt


def _pct(now: float | None, prev: float | None) -> float | None:
    if now is None or prev is None or prev <= 0:
        return None
    return (now / prev - 1) * 100


def _sign(x: float | None, flat: float) -> int | None:
    if x is None:
        return None
    return +1 if x > flat else (-1 if x < -flat else 0)


def verdict(qty_yoy: float | None, price_yoy: float | None,
            flat: float = FLAT_PCT) -> dict:
    """물량·단가 증감률 → 국면 판정.

    ★ 부호만 보지 않고 **보합 구간(±flat)** 을 둔다. 단가 +0.4% 를 '상승'으로
      읽으면 매달 국면이 뒤집혀 아무 말도 못 하게 된다.
    """
    q, p = _sign(qty_yoy, flat), _sign(price_yoy, flat)
    if q is None or p is None:
        return {"code": "unknown", "label": "판정 불가", "q": q, "p": p,
                "note": "중량 또는 전년 동기 데이터가 없어 단가를 계산할 수 없습니다."}
    code, label, note = _LABELS[(q, p)]
    return {"code": code, "label": label, "q": q, "p": p, "note": note}


def decompose(usd_now: float | None, usd_prev: float | None,
              wgt_now: float | None, wgt_prev: float | None,
              min_wgt: float = MIN_WGT_KG, flat: float = FLAT_PCT) -> dict:
    """금액/물량/단가 증감률과 **기여도**를 한 번에 낸다.

    priceShare = 금액 변화 중 단가가 설명하는 비중(%). qtyShare 와 합이 100 이다
    (로그 분해라 교차항이 남지 않는다). 금액 변화가 거의 없으면 None 으로 비운다.
    """
    p_now, p_prev = asp(usd_now, wgt_now, min_wgt), asp(usd_prev, wgt_prev, min_wgt)
    out = {
        "usd": usd_now, "usdPrev": usd_prev,
        "wgt": wgt_now, "wgtPrev": wgt_prev,
        "asp": round(p_now, 2) if p_now else None,
        "aspPrev": round(p_prev, 2) if p_prev else None,
        "valueYoy": _r(_pct(usd_now, usd_prev)),
        "qtyYoy": _r(_pct(wgt_now, wgt_prev)),
        "priceYoy": _r(_pct(p_now, p_prev)),
        "priceShare": None, "qtyShare": None,
    }
    v = out["valueYoy"]
    if (v is not None and abs(v) >= MIN_VALUE_MOVE_PCT
            and usd_now and usd_prev and wgt_now and wgt_prev
            and p_now and p_prev):
        lv = math.log(usd_now / usd_prev)
        lp = math.log(p_now / p_prev)
        if lv != 0:
            out["priceShare"] = round(lp / lv * 100, 1)
            out["qtyShare"] = round(100 - out["priceShare"], 1)
    out["verdict"] = verdict(out["qtyYoy"], out["priceYoy"], flat)
    return out


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)
