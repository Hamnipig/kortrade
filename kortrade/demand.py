"""최종 수요 축 — 한국 수출 감소의 **원인**을 가른다.

이 프로젝트가 반복해서 부딪힌 벽
    對미 ESS 셀 수출 −13%. 셋 중 무엇인가?
      (a) 미국 ESS 수요 자체가 줄었다
      (b) 한국 기업이 미국에서 만들기 시작했다 (통관에서 사라진다)
      (c) 시장은 크는데 한국이 점유율을 뺏겼다

    현지화지수((부품+소재)/완제품)는 (a)와 (b)를 부분적으로 가른다. 상류가 계속
    나가면 생산지 이동이고, 같이 죽으면 수요 위축이라는 논리다.

    **그런데 (c)는 한국 수출 데이터만으로는 원리적으로 볼 수 없다.** 한국 수출만
    보고 있으면 미국 시장이 두 배가 됐는지 반토막 났는지 알 방법이 자체에 없다.
    그리고 (b)와 (c)는 투자 판단이 정반대다 — (b)면 그 기업 실적은 오히려 좋고,
    (c)면 구조적 훼손이다.

해법 — 축을 두 개 더 세우고 **합치지 않는다**
    한국 수출   (관세청, 달러/월)        이미 있다
    미국 수입   (US Census, 달러/월)     시장 규모 + 한국 점유율  → (c)
    미국 설치   (EIA, GW/월)             최종 수요                → (a)

★ 세 축은 단위도 출처도 갱신주기도 다르다. 하나의 숫자로 합치면 근거 없는
  '글로벌 공급량'이 된다. 증감률과 방향만 나란히 놓고 대조한다.

★ 중량 → GWh 환산도 하지 않는다. HS 8507.60 은 셀·모듈·시스템이 섞여 있어
  kg 당 에너지밀도가 제품마다 다르다. 환산하면 그 가정이 결과를 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "demand.yaml"


@dataclass
class ImportCode:
    code: str
    label: str
    status: str = "draft"
    expect: str = ""
    why: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass
class DemandConfig:
    version: str = ""
    window: int = 8
    recent: int = 3
    hist: int = 40
    partner: str = "5800"
    dataset: str = "timeseries/intltrade/imports/hs"
    imports_enabled: bool = True
    codes: list[ImportCode] = field(default_factory=list)
    cap_enabled: bool = True
    routes: list[str] = field(default_factory=list)
    facet_candidates: list[str] = field(default_factory=list)
    match: str = "batter"
    status_filter: list[str] = field(default_factory=list)
    flat_pct: float = 5.0
    share_pp: float = 3.0
    min_base_usd: float = 20_000_000.0

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.codes and self.imports_enabled:
            errs.append("us_imports.codes 가 비어 있음")
        for c in self.codes:
            if not (c.code.isdigit() and len(c.code) == 10):
                errs.append(f"{c.code}: 미국 HTS 통계품목은 10자리여야 한다")
            if c.status not in ("active", "draft"):
                errs.append(f"{c.code}: status 는 active/draft 만 허용")
            # draft 를 승격할 근거가 코드 안에 있어야 자동 검증이 가능하다
            if c.status == "draft" and not c.expect:
                errs.append(f"{c.code}: draft 인데 expect(설명 대조 문자열)가 없다")
        if not (self.partner.isdigit() and len(self.partner) == 4):
            errs.append(f"partner '{self.partner}': Census CTY_CODE 는 4자리")
        if self.cap_enabled and not self.routes:
            errs.append("us_capacity.routes 가 비어 있음")
        return errs


def load(path: Path | None = None) -> DemandConfig:
    raw = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8")) or {}
    imp = raw.get("us_imports") or {}
    cap = raw.get("us_capacity") or {}
    th = raw.get("thresholds") or {}
    return DemandConfig(
        version=str(raw.get("version", "")),
        window=int(raw.get("window", 8)), recent=int(raw.get("recent", 3)),
        hist=int(raw.get("hist", 40)),
        partner=str(imp.get("partner", "5800")),
        dataset=str(imp.get("dataset", "timeseries/intltrade/imports/hs")),
        imports_enabled=bool(imp.get("enabled", True)),
        codes=[ImportCode(code=str(c.get("code", "")), label=c.get("label", ""),
                          status=c.get("status", "draft"), expect=c.get("expect", ""),
                          why=c.get("why", ""))
               for c in (imp.get("codes") or [])],
        cap_enabled=bool(cap.get("enabled", True)),
        routes=[str(r) for r in (cap.get("routes") or [])],
        facet_candidates=[str(f) for f in (cap.get("facet_candidates") or [])],
        match=str(cap.get("match", "batter")),
        status_filter=[str(s) for s in (cap.get("status_filter") or [])],
        flat_pct=float(th.get("flat_pct", 5.0)),
        share_pp=float(th.get("share_pp", 3.0)),
        min_base_usd=float(th.get("min_base_usd", 20_000_000.0)),
    )


# ── 판정 ─────────────────────────────────────────────────────────────────

def _sign(x: float | None, flat: float) -> int | None:
    if x is None:
        return None
    return +1 if x > flat else (-1 if x < -flat else 0)


def attribute(kr_yoy: float | None, imp_yoy: float | None,
              share_now: float | None, share_prev: float | None,
              cap_yoy: float | None, flat: float = 5.0,
              share_pp: float = 3.0) -> dict:
    """한국 수출 감소가 무엇 때문인지 가른다.

    ★ 이 판정의 목적은 하나다 — **'현지화'와 '점유율 상실'을 혼동하지 않는 것.**
      둘 다 한국 수출을 줄이지만 투자 판단은 정반대다. 현지화면 그 기업 실적은
      오히려 좋을 수 있고, 점유율 상실이면 구조적 훼손이다.

    미국 수입이 없으면 (c)를 판정할 수 없으므로 솔직하게 'partial' 로 내려온다.
    """
    k = _sign(kr_yoy, flat)
    i = _sign(imp_yoy, flat)
    c = _sign(cap_yoy, flat)
    d_share = (None if (share_now is None or share_prev is None)
               else round(share_now - share_prev, 1))

    if k is None:
        return {"code": "unknown", "label": "판정 불가", "shareChg": d_share,
                "note": "한국 수출의 전년 동기 데이터가 부족합니다."}

    # 미국 축이 하나도 없으면 기존 현지화 판정 이상을 말할 수 없다
    if i is None and c is None:
        return {"code": "partial", "label": "미국 축 없음", "shareChg": d_share,
                "note": "미국 수입·설치 데이터가 없어 '현지화'와 '점유율 상실'을 "
                        "구분할 수 없습니다. 현지화지수만으로 판단하십시오."}

    # ── 점유율 상실이 가장 먼저다. 시장이 크는데 우리 몫이 줄면 그게 결론이다.
    if i is not None and i > 0 and d_share is not None and d_share <= -share_pp:
        return {"code": "share_loss", "label": "점유율 상실", "shareChg": d_share,
                "note": "미국 수입 시장은 커지는데 한국 비중이 떨어졌습니다. "
                        "현지화가 아니라 경쟁에서 밀린 것일 수 있습니다 — "
                        "이 경우 부품·소재가 버텨도 구조적 훼손입니다."}

    # ── 설치는 느는데 수입이 줄면 미국 내 생산이 대체한 것이다
    if c is not None and c > 0 and i is not None and i < 0:
        return {"code": "onshoring", "label": "현지 생산 대체", "shareChg": d_share,
                "note": "미국 설치는 느는데 수입은 줄었습니다. 미국 내 생산이 수입을 "
                        "대체하는 국면입니다. 한국 기업의 현지 공장 가동 여부는 "
                        "통관 데이터에 없으므로 공시로 확인해야 합니다."}

    # ── 수요 자체가 식은 경우
    if (c is not None and c < 0) and (i is None or i <= 0):
        return {"code": "demand_down", "label": "수요 위축", "shareChg": d_share,
                "note": "미국 설치와 수입이 함께 줄었습니다. 생산지 이동으로 "
                        "설명되지 않습니다."}

    if i is not None and i < 0 and k < 0 and (c is None or c == 0):
        return {"code": "demand_down", "label": "수요 위축", "shareChg": d_share,
                "note": "미국 수입이 한국 수출과 함께 줄었습니다. 설치 축이 없거나 "
                        "보합이라 확정은 아니지만 수요 둔화 쪽입니다."}

    # ── 시장도 크고 우리 몫도 지키는 경우
    if i is not None and i > 0:
        if d_share is not None and d_share >= share_pp:
            return {"code": "share_gain", "label": "점유율 확대", "shareChg": d_share,
                    "note": "미국 수입이 늘면서 한국 비중도 올랐습니다."}
        return {"code": "expanding", "label": "동반 확장", "shareChg": d_share,
                "note": "미국 수입 시장이 커지고 한국 비중은 유지됩니다."}

    return {"code": "mixed", "label": "혼재", "shareChg": d_share,
            "note": "미국 축들이 서로 다른 방향을 가리킵니다. 단월 변동일 수 있으니 "
                    "다음 달 갱신을 기다리십시오."}


def share(part: float | None, whole: float | None,
          min_base: float = 20_000_000.0) -> float | None:
    """한국이 미국 수입에서 차지하는 비중(%). 기저가 작으면 내지 않는다."""
    if not whole or whole < min_base or part is None:
        return None
    return round(part / whole * 100, 1)


def index(values: list[float | None]) -> list[float | None] | None:
    """첫 유효값 = 100 으로 지수화. 단위가 다른 계열을 한 축에 올리기 위한 것이다.

    ★ 지수는 **비교용**이지 합산용이 아니다. 달러와 GW 를 같은 그림에 올려도
      되는 이유는 각자의 출발점 대비 배수만 보기 때문이다.
    """
    base = next((v for v in values if v not in (None, 0)), None)
    if not base:
        return None
    return [None if v is None else round(v / base * 100, 1) for v in values]
