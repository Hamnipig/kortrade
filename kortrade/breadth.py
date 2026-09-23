"""확산도 — 성장이 **넓어지고 있는가, 좁아지고 있는가.**

왜 필요한가 (2026-09-23)
  화장품 수출의 국면은 이미 한 번 끝났다. 대중국 비중은 '21년 53.2% 에서
  '26년 상반기 14.4% 로 내려왔고(한국무역협회·식약처), 미국이 20.7% 로 1위다.
  그러므로 지금 관전 포인트는 "중국이 빠진다"가 아니라 **"미국 다음은 어디인가"** 다.

  집중은 피크의 전조다. 한 나라가 성장의 대부분을 만들고 있으면, 그 나라의
  재고 사이클 한 번에 섹터 전체가 꺾인다. 반대로 증가분이 여러 나라에 흩어져
  있으면 한 나라가 쉬어도 추세가 버틴다. 금액 합계에는 이 차이가 안 보인다.

★ 중계무역 허브 함정
  네덜란드 +220%, 에스토니아 +196% (식약처, '26 상반기 상위 20개국)를
  '유럽 개척'으로 읽으면 크게 틀린다. 로테르담·발트 3국은 **재수출 관문**이고,
  상당 부분이 러시아·CIS 로 넘어간다. 최종 소비지가 아니다.
  기초화장품 단가가 UAE 53.9 / 네덜란드 49.4 $/kg 로 미국(27.3)의 2배 가까이
  나오는 것도 허브 물량의 성격(고가·소량 혼재 재수출)과 무관하지 않다.

  → 허브를 빼고 계산한 확산도를 **함께** 낸다. 둘이 갈리면 그 자체가 신호다.
    어느 나라를 허브로 볼지는 코드가 아니라 **설정에 근거와 함께** 둔다
    (config/sectors/*.yaml 의 hub_countries). 섹터마다 다르고, 시간이 지나면
    바뀌기 때문이다.

★ 수집국 커버리지
  우리는 전 세계가 아니라 **설정에 올린 국가만** 수집한다. 그래서 비중을 낼 때
  분모가 세계 전체가 아니다. 이 사실을 숨기면 "상위 3국 45%" 같은 숫자가
  실제보다 작게 보인다. coverage 를 항상 같이 낸다 — 시군구 표에서
  placesCoverage 를 노출한 것과 같은 이유다.
"""

from __future__ import annotations

TOP_N = 3

# 증가분 기여도를 낼 때, 전체 증가분이 이보다 작으면 비율을 내지 않는다.
# 분모가 0 근처면 기여율이 ±수백 %로 발산한다.
MIN_DELTA_USD = 1_000_000

# 확산/집중 판정 문턱 (상위 N 국 비중의 전년 대비 변화, %p)
SPREAD_PP = 1.5


def concentration(values: dict[str, float], top_n: int = TOP_N) -> dict:
    """상위 N 국 비중과 HHI. values 는 {국가코드: 금액}."""
    tot = sum(v for v in values.values() if v and v > 0)
    if tot <= 0:
        return {"total": 0.0, "topShare": None, "hhi": None, "top": []}
    ranked = sorted(((k, v) for k, v in values.items() if v and v > 0),
                    key=lambda kv: -kv[1])
    top = ranked[:top_n]
    return {
        "total": tot,
        "topShare": round(sum(v for _, v in top) / tot * 100, 1),
        # HHI 는 0~10000. 1,500 미만 분산 / 2,500 초과 집중 (미 법무부 기준선 관용)
        "hhi": round(sum((v / tot * 100) ** 2 for _, v in ranked)),
        "top": [{"cc": k, "share": round(v / tot * 100, 1)} for k, v in top],
    }


def contribution(cur: dict[str, float], prev: dict[str, float],
                 top_n: int = TOP_N) -> dict:
    """증가분을 누가 만들었나.

    outsideShare = 전체 증가분 중 **상위 N 국 밖**이 만든 비중(%).
    이게 오르면 성장이 퍼지는 중이고, 내리면 소수 국가에 얹혀 있다는 뜻이다.
    """
    delta = {k: (cur.get(k) or 0.0) - (prev.get(k) or 0.0)
             for k in set(cur) | set(prev)}
    total_delta = sum(delta.values())
    ranked = sorted(((k, v) for k, v in cur.items() if v and v > 0), key=lambda kv: -kv[1])
    top_keys = {k for k, _ in ranked[:top_n]}
    outside = sum(v for k, v in delta.items() if k not in top_keys)

    movers = sorted(delta.items(), key=lambda kv: -kv[1])
    return {
        "totalDelta": total_delta,
        "outsideDelta": outside,
        "outsideShare": (round(outside / total_delta * 100, 1)
                         if abs(total_delta) >= MIN_DELTA_USD else None),
        "gainers": [{"cc": k, "delta": v} for k, v in movers[:5] if v > 0],
        "losers": [{"cc": k, "delta": v} for k, v in movers[-5:] if v < 0],
    }


def first_cross(series: dict[str, float], threshold: float) -> str | None:
    """월별 금액이 처음으로 threshold 를 넘긴 달. S커브 어느 단계인지의 단서다.

    ★ 한 번 튄 달을 '진입'으로 읽지 않기 위해 **연속 2개월**을 요구한다.
      단발 대량 선적(전시회·초도 물량)이 진입으로 잡히면 신규 시장 수가 부풀려진다.
    """
    months = sorted(series)
    for i in range(len(months) - 1):
        if (series.get(months[i]) or 0) >= threshold and \
           (series.get(months[i + 1]) or 0) >= threshold:
            return months[i]
    return None


def verdict(top_share_now: float | None, top_share_prev: float | None,
            spread_pp: float = SPREAD_PP) -> dict:
    """상위 N 국 비중의 방향으로 국면을 판정한다."""
    if top_share_now is None or top_share_prev is None:
        return {"code": "unknown", "label": "판정 불가",
                "note": "전년 동기 국가별 데이터가 부족합니다."}
    chg = top_share_now - top_share_prev
    if chg <= -spread_pp:
        return {"code": "spreading", "label": "확산", "chg": round(chg, 1),
                "note": "성장이 상위국 밖으로 퍼지고 있습니다. 한 나라의 재고 사이클에 "
                        "덜 휘둘리는 구간입니다."}
    if chg >= spread_pp:
        return {"code": "concentrating", "label": "집중", "chg": round(chg, 1),
                "note": "성장이 소수 국가로 몰리고 있습니다. 그 나라의 재고·채널 상황이 "
                        "곧 섹터 전체의 리스크입니다 — 피크 여부를 따로 확인하십시오."}
    return {"code": "stable", "label": "유지", "chg": round(chg, 1),
            "note": "국가 구성이 크게 바뀌지 않았습니다."}


def analyze(cur: dict[str, float], prev: dict[str, float],
            hubs: set[str] | None = None, top_n: int = TOP_N,
            world_total: float | None = None) -> dict:
    """확산도 한 묶음. 허브 포함/제외 두 벌을 낸다.

    world_total 을 주면 coverage(수집국 합 / 전세계 합)를 함께 낸다.
    분모가 세계 전체가 아니라는 사실을 숨기지 않기 위해서다.
    """
    hubs = hubs or set()
    ex_cur = {k: v for k, v in cur.items() if k not in hubs}
    ex_prev = {k: v for k, v in prev.items() if k not in hubs}

    c_all, p_all = concentration(cur, top_n), concentration(prev, top_n)
    c_ex, p_ex = concentration(ex_cur, top_n), concentration(ex_prev, top_n)
    hub_usd = sum(v for k, v in cur.items() if k in hubs and v)
    hub_prev = sum(v for k, v in prev.items() if k in hubs and v)

    return {
        "topN": top_n,
        "all": {"now": c_all, "prev": p_all,
                "verdict": verdict(c_all["topShare"], p_all["topShare"])},
        "exHub": {"now": c_ex, "prev": p_ex,
                  "verdict": verdict(c_ex["topShare"], p_ex["topShare"])},
        "contribution": contribution(cur, prev, top_n),
        "hub": {
            "codes": sorted(hubs),
            "usd": hub_usd,
            "share": round(hub_usd / c_all["total"] * 100, 1) if c_all["total"] else None,
            "yoy": (round((hub_usd / hub_prev - 1) * 100, 1)
                    if hub_prev and hub_prev > 0 else None),
        },
        "coverage": (round(min(c_all["total"] / world_total, 1.0) * 100, 1)
                     if world_total and world_total > 0 else None),
    }
