"""전산업 워치리스트 로더 + 지표 계산.

섹터 탭과 데이터 출처가 다르다:
  섹터 탭    시군구별 품목별 API — HS 6단위까지만, 대신 지역 분해
  워치리스트  품목별 국가별 API  — HS 10단위 그대로, 국가 분해 + **중량(kg)**

중량이 오기 때문에 여기서만 P·Q 분해가 가능하다:
    P(내재 단가) = 수출액 / 중량   ($/kg)
    Q(물량)      = 중량            (kg)
금액만 보면 단가 하락을 물량 증가로, 단가 상승을 수요 회복으로 오독한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"

# 증가율 분모 최소액. 기저가 이보다 작으면 % 를 계산하지 않는다.
MIN_BASE_USD = 3_000_000
# 단가를 계산할 최소 중량. 1kg 미만이면 $/kg 이 폭발한다.
MIN_BASE_KG = 1_000


@dataclass
class Item:
    key: str
    name: str
    group: str
    hs6: str
    hsk: list[str] = field(default_factory=list)
    hs6_extra: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    idea: str = ""
    countries: list[str] = field(default_factory=list)
    purity: str = "low"
    status: str = "active"
    evidence: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active" and bool(self.hsk)

    @property
    def parents(self) -> list[str]:
        """수집해야 할 6단위 코드 전부 (트랙터처럼 여러 6단위에 걸친 품목이 있다)."""
        return sorted({self.hs6, *self.hs6_extra})

    def validate(self) -> list[str]:
        errs = []
        for p in self.parents:
            if not (p.isdigit() and len(p) == 6):
                errs.append(f"{self.key}: hs6 '{p}' 는 6자리가 아니다")
        for c in self.hsk:
            if not (c.isdigit() and len(c) == 10):
                errs.append(f"{self.key}: hsk '{c}' 는 10자리가 아니다")
            if not any(c.startswith(p) for p in self.parents):
                errs.append(f"{self.key}: hsk '{c}' 가 hs6 {self.parents} 어디에도 속하지 않는다")
        if self.status == "active" and not self.hsk:
            errs.append(f"{self.key}: active 인데 hsk 가 비어 있다")
        if self.status == "active" and not self.evidence.strip():
            errs.append(f"{self.key}: active 인데 검증 근거(evidence)가 없다")
        if self.status not in ("active", "draft"):
            errs.append(f"{self.key}: status '{self.status}' 는 active/draft 만 허용")
        if self.purity not in ("high", "mid", "low"):
            errs.append(f"{self.key}: purity '{self.purity}' 는 high/mid/low 만 허용")
        # ★ YAML 1.1 은 NO/ON/OFF/YES/Y/N 을 불린으로 읽는다. 따옴표 없이 쓴
        #   countries: [PL, NO, ...] 의 'NO'(노르웨이)가 False 가 되어 조용히 사라진다.
        for c in self.countries:
            if not isinstance(c, str):
                errs.append(f"{self.key}: 국가코드 {c!r} 가 문자열이 아니다 "
                            "— YAML 이 NO/ON/OFF 를 불린으로 읽었다. 따옴표로 감쌀 것")
            elif not (len(c) == 2 and c.isalpha() and c.isupper()):
                errs.append(f"{self.key}: 국가코드 '{c}' 형식 오류 (대문자 2자리)")
        return errs


@dataclass
class Watchlist:
    version: str
    countries: dict[str, str]
    groups: dict[str, dict]
    items: list[Item]

    def active(self) -> list[Item]:
        return [i for i in self.items if i.active]

    def all_parents(self) -> list[str]:
        return sorted({p for i in self.active() for p in i.parents})

    def all_countries(self) -> list[str]:
        """실제로 쓰이는 국가코드만. config 에 없는 코드는 이름을 코드로 대체한다."""
        used = {c for i in self.active() for c in i.countries}
        return sorted(used)

    def validate(self) -> list[str]:
        errs = []
        keys = [i.key for i in self.items]
        for k in set(keys):
            if keys.count(k) > 1:
                errs.append(f"key '{k}' 가 중복됐다")
        for i in self.items:
            if i.group not in self.groups:
                errs.append(f"{i.key}: group '{i.group}' 가 groups 에 없다")
            errs.extend(i.validate())
        return errs


def load(path: Path | None = None) -> Watchlist:
    cfg = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8"))
    items = [Item(**{k: v for k, v in d.items() if k in Item.__annotations__})
             for d in cfg.get("items", [])]
    return Watchlist(
        version=str(cfg.get("version", "")),
        countries=cfg.get("countries", {}) or {},
        groups=cfg.get("groups", {}) or {},
        items=items,
    )


# ------------------------------------------------------------------ 지표

def pct(cur: float, prev: float, floor: float) -> float | None:
    """증가율. 분모가 floor 미만이면 계산하지 않는다(기저효과 차단)."""
    if prev is None or prev < floor or prev <= 0:
        return None
    return round((cur / prev - 1) * 100, 1)


def unit_price(usd: float, kg: float) -> float | None:
    """$/kg. 중량이 너무 작으면 의미 없는 값이 나오므로 버린다."""
    if kg is None or kg < MIN_BASE_KG:
        return None
    return usd / kg


def base_index(monthly: list[float], q3p_idx: list[int], yr_idx: list[int]) -> float | None:
    """전년 동기 3개월이 그 품목 기준으로 비정상적으로 낮았는지 본다.

    가속(=3M YoY − 8M YoY)은 **분자가 아니라 분모** 때문에 커질 수 있다.
    작년 그 3개월이 유난히 비어 있었으면 올해가 평범해도 가속이 크게 잡힌다.
    이 값이 1보다 한참 작으면 '기저가 낮아서 생긴 가속'을 의심해야 한다.

        base_index = (전년 동기 3개월 월평균) / (전년 12개월 월평균)

    ※ 가속 자체는 YoY 끼리의 차이라 **달력 계절성은 이미 상쇄돼 있다.**
      가속을 오염시키는 건 계절성이 아니라 이 기저효과와 선적 타이밍 쏠림이다.
    """
    q = [monthly[i] for i in q3p_idx if 0 <= i < len(monthly)]
    y = [monthly[i] for i in yr_idx if 0 <= i < len(monthly)]
    if len(q) < 2 or len(y) < 6:
        return None
    ya = sum(y) / len(y)
    if ya <= 0:
        return None
    return round((sum(q) / len(q)) / ya, 2)


def signals(cur_usd: float, prev_usd: float, cur_kg: float, prev_kg: float,
            q3_usd: float, q3p_usd: float, q3_kg: float, q3p_kg: float) -> dict:
    """사용자가 고른 세 신호를 한 번에 계산한다.

    가속 : 최근 3개월 YoY - 비교창 YoY  (추세가 꺾이거나 붙는 지점)
    P    : $/kg 의 YoY 변화            (단가 전환)
    Q    : 중량의 YoY 변화             (실물 물량 턴어라운드)
    """
    yoy = pct(cur_usd, prev_usd, MIN_BASE_USD)
    q3 = pct(q3_usd, q3p_usd, MIN_BASE_USD / 2)
    accel = round(q3 - yoy, 1) if (yoy is not None and q3 is not None) else None

    p_cur, p_prev = unit_price(cur_usd, cur_kg), unit_price(prev_usd, prev_kg)
    p_q3, p_q3p = unit_price(q3_usd, q3_kg), unit_price(q3p_usd, q3p_kg)
    p_yoy = round((p_cur / p_prev - 1) * 100, 1) if (p_cur and p_prev) else None
    p_q3_yoy = round((p_q3 / p_q3p - 1) * 100, 1) if (p_q3 and p_q3p) else None

    q_yoy = pct(cur_kg, prev_kg, MIN_BASE_KG)
    q_q3 = pct(q3_kg, q3p_kg, MIN_BASE_KG)

    return {
        "usd": round(cur_usd / 1e6, 1),
        "yoy": yoy, "q3": q3, "accel": accel,
        "p": round(p_cur, 2) if p_cur else None,
        "pYoy": p_yoy, "pQ3": p_q3_yoy,
        "kg": round(cur_kg / 1000, 1) if cur_kg else 0.0,   # 톤
        "qYoy": q_yoy, "qQ3": q_q3,
        # 물량 턴어라운드 = 비교창은 마이너스인데 최근 3개월이 플러스로 돌아선 것
        "qTurn": bool(q_yoy is not None and q_q3 is not None and q_yoy < 0 <= q_q3),
        # 단가 전환 = 같은 논리를 P 에 적용
        "pTurn": bool(p_yoy is not None and p_q3_yoy is not None and p_yoy < 0 <= p_q3_yoy),
    }
