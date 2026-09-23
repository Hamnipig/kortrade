"""해외 현지생산 판정 — 레이어들이 공유하는 도메인 로직.

수출 데이터의 가장 큰 구멍은 **해외 현지생산**이다. 기업이 미국 공장에서 셀을
조립하기 시작하면 국내 셀 수출은 줄지만 그 기업 실적은 오히려 좋아질 수 있다.
완제품 수출 감소를 수요 감소로 읽으면 정반대 결론이 나온다.

완제품이 현지로 가도 **부품·소재는 여전히 한국에서 나간다.** 그래서 품목 하나가
아니라 단계를 묶어서 본다.

    chain_total  = 완제품 + 부품 + 소재        진짜 수요. 이게 늘면 수요는 살아있다
    localization = (부품 + 소재) / 완제품      오르면 현지생산 전환 진행 중

실증 (2026-09-18, 對미 2차전지)
    완제품 셀  $1,630M → $1,667M  (+2%)
    부품·소재  $1,093M → $1,407M  (+29%)
    체인 전체  $2,723M → $3,075M  (+13%)
    현지화지수 2025 내내 0.35~0.92 → 2026-06 이후 1.35 / 1.54 / 1.83
    ESS셀 −13% 만 보고 수요 위축으로 판단하면 틀린다.

★ 왜 별도 모듈인가 (2026-09-23)
  chains.py 에 있던 것을 battery.py 도 쓰게 되면서 공용 자리로 옮겼다.
  레이어가 다른 레이어를 import 하면 배포 누락 한 번에 수집이 멈춘다
  (실제로 battery→flash import 로 그런 사고가 났다). 공용 코드는 공용 자리에 둔다.
"""

from __future__ import annotations

# 현지화지수가 이 배수 이상으로 뛰면 '현지 전환' 신호로 본다.
LOCALIZATION_JUMP = 1.4


def localization(final_usd: float | None, upstream_usd: float | None) -> float | None:
    """(부품+소재) / 완제품. 완제품이 너무 작으면 의미가 없다."""
    if not final_usd or final_usd <= 0:
        return None
    return round(upstream_usd / final_usd, 2)


def verdict(final_yoy: float | None, total_yoy: float | None,
            loc_now: float | None, loc_prev: float | None) -> dict:
    """완제품 감소를 수요 감소로 읽어도 되는지 판정한다.

    목적은 하나다 — **완제품 수출이 줄었다는 이유만으로 투자 판단을 내리지 않게 막는 것.**

    ※ loc_now/loc_prev 는 **최근 3개월 기준**을 넣는 것이 낫다. 8개월 평균은
      전환 초기를 뭉갠다 — 실측 사례에서 8개월 기준은 0.67→0.84(1.25배)로
      문턱을 못 넘었지만, 3개월 기준은 0.70→1.56(2.2배)로 명확히 잡혔다.
    """
    if final_yoy is None or total_yoy is None:
        return {"code": "unknown", "label": "판정 불가",
                "note": "전년 동기 데이터가 부족합니다."}

    loc_up = (loc_now is not None and loc_prev is not None
              and loc_prev > 0 and loc_now / loc_prev >= LOCALIZATION_JUMP)

    if final_yoy < 0 and total_yoy > 0:
        return {"code": "localizing", "label": "현지화",
                "note": "완제품 수출은 줄었지만 부품·소재를 더하면 체인 전체는 늘었습니다. "
                        "수요 감소가 아니라 생산지 이동일 가능성이 큽니다 — "
                        "현지법인 매출(DART 부문정보)로 확인하세요."}
    if final_yoy < 0 and total_yoy <= 0 and loc_up:
        return {"code": "mixed", "label": "혼재",
                "note": "체인 전체도 줄었지만 현지화지수는 뚜렷이 올랐습니다. "
                        "수요 위축과 생산지 이동이 겹쳐 있을 수 있습니다."}
    if final_yoy < 0 and total_yoy <= 0:
        return {"code": "contracting", "label": "수요 위축",
                "note": "완제품·부품·소재가 함께 줄었습니다. 현지화로 설명되지 않습니다."}
    if loc_up:
        return {"code": "expanding_local", "label": "성장+현지화",
                "note": "체인이 커지면서 현지 조립 비중도 오르는 중입니다."}
    return {"code": "expanding", "label": "성장",
            "note": "완제품과 체인이 함께 늘고 있습니다."}
