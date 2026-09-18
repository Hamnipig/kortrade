"""밸류체인 로더 + 현지화 지표.

수출 데이터의 구조적 한계 하나를 보정하기 위한 레이어다:
**해외 현지생산이 늘면 국내 완제품 수출은 줄지만 기업 실적은 나빠지지 않는다.**

완제품이 현지로 가도 부품·소재는 한국에서 나가므로, 단계를 묶어서 보면
"수요가 줄었나"와 "생산지가 옮겨갔나"를 갈라낼 수 있다.

    chain_total  = final + component + material
    localization = (component + material) / final
    equipment    = 장비 (현지 증설의 선행 지표. total 에는 넣지 않는다)

장비를 합계에서 빼는 이유: 장비는 매출 계상 시점이 완전히 다르고, 한 번 팔리면
끝이라 수요의 흐름이 아니라 설비투자의 흐름이다. 섞으면 둘 다 흐려진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "chains.yaml"

STAGES = ("final", "component", "material", "equipment")
# 합계(chain_total)에 들어가는 단계. 장비는 뺀다.
DEMAND_STAGES = ("final", "component", "material")

# 현지화지수가 이 배수 이상으로 뛰면 '현지 전환' 신호로 본다.
LOCALIZATION_JUMP = 1.4


@dataclass
class Chain:
    key: str
    name: str
    order: int = 99
    markets: list[str] = field(default_factory=list)
    thesis: str = ""
    stages: dict[str, list[dict]] = field(default_factory=dict)
    evidence: str = ""

    def codes(self, stage: str | None = None) -> list[str]:
        st = [stage] if stage else STAGES
        return [c["code"] for s in st for c in (self.stages.get(s) or [])]

    def label(self, code: str) -> str:
        for s in STAGES:
            for c in self.stages.get(s) or []:
                if c["code"] == code:
                    return c.get("label", code)
        return code

    def stage_of(self, code: str) -> str | None:
        for s in STAGES:
            if any(c["code"] == code for c in (self.stages.get(s) or [])):
                return s
        return None

    @property
    def parents(self) -> list[str]:
        return sorted({c[:6] for c in self.codes()})

    def validate(self) -> list[str]:
        errs = []
        for s in self.stages:
            if s not in STAGES:
                errs.append(f"{self.key}: 알 수 없는 단계 '{s}' (허용: {STAGES})")
        for c in self.codes():
            if not (c.isdigit() and len(c) == 10):
                errs.append(f"{self.key}: '{c}' 는 10자리 HSK 가 아니다")
        if not self.codes("final"):
            errs.append(f"{self.key}: final(완제품) 단계가 비어 있다 — 현지화지수를 못 만든다")
        if not (self.codes("component") or self.codes("material")):
            errs.append(f"{self.key}: 상류(component/material)가 비어 있다 — 체인의 의미가 없다")
        dup = [c for c in self.codes() if self.codes().count(c) > 1]
        if dup:
            errs.append(f"{self.key}: 코드 중복 {sorted(set(dup))} — 한 코드는 한 단계에만")
        for m in self.markets:
            if not isinstance(m, str) or len(m) != 2 or not m.isupper():
                errs.append(f"{self.key}: 시장코드 {m!r} 형식 오류 (YAML 이 NO 를 false 로 읽는다)")
        if not self.evidence.strip():
            errs.append(f"{self.key}: 검증 근거(evidence)가 없다")
        return errs


@dataclass
class Chains:
    version: str
    chains: list[Chain]

    def all_codes(self) -> list[str]:
        return sorted({c for ch in self.chains for c in ch.codes()})

    def all_parents(self) -> list[str]:
        return sorted({p for ch in self.chains for p in ch.parents})

    def validate(self) -> list[str]:
        errs = []
        keys = [c.key for c in self.chains]
        for k in set(keys):
            if keys.count(k) > 1:
                errs.append(f"체인 key '{k}' 중복")
        for c in self.chains:
            errs.extend(c.validate())
        return errs


def load(path: Path | None = None) -> Chains:
    cfg = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8"))
    out = []
    for d in cfg.get("chains", []):
        out.append(Chain(**{k: v for k, v in d.items() if k in Chain.__annotations__}))
    out.sort(key=lambda c: c.order)
    return Chains(version=str(cfg.get("version", "")), chains=out)


# ------------------------------------------------------------------ 지표

def localization(final_usd: float, upstream_usd: float) -> float | None:
    """(부품+소재) / 완제품. 완제품이 너무 작으면 의미가 없다."""
    if not final_usd or final_usd <= 0:
        return None
    return round(upstream_usd / final_usd, 2)


def verdict(final_yoy: float | None, total_yoy: float | None,
            loc_now: float | None, loc_prev: float | None) -> dict:
    """완제품 감소를 수요 감소로 읽어도 되는지 판정한다.

    화면의 '현지화' 배지가 이 판정을 그대로 쓴다. 목적은 하나다 —
    **완제품 수출이 줄었다는 이유만으로 투자 판단을 내리지 않게 막는 것.**

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
