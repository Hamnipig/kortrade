"""PCB·기판 레이어.

iM증권 「PCB Tracker」(2026-09-28) 의 한국 축을 **원출처(관세청)** 로 재현하고,
리포트가 해외 데이터로 우회해서 보는 것을 한국 데이터로 어디까지 볼 수 있는지
명시적으로 가른다.

── 이 레이어의 중심 명제 ─────────────────────────────────────────────────

  한국 HS 8534 에는 층수 구분이 없다. 그래서 **고다층·패키지기판 전환은
  금액으로 보이지 않고 ASP($/kg)로만 보인다.**

  리포트 8월 실측: 수출액 +15% / 중량 −3% / ASP +18% ('22.10 이후 최고)
  금액만 보면 "+15% 성장"이다. 물량이 오히려 줄었다는 사실 — 즉 성장 전부가
  단가에서 나왔다는 사실 — 이 빠지면 AI 서버용 고다층 전환이라는 해석 자체가
  성립하지 않는다. 그래서 P/Q 분해(kortrade/pq.py)가 화면 맨 위에 온다.

  일본은 METI 가 品目別 생산액(일반/고다층/HDI/패키지기판)을 주므로 믹스를
  직접 본다. 한국에 그런 통계는 없다. ASP 가 대체재이고, 그 한계
  (구리 가격·환율·품목 믹스가 한 숫자에 섞인다)를 화면에 같이 적는다.

── 축 세 개 ──────────────────────────────────────────────────────────────

  1. 완제품(board)   HS 8534 — 수출액·중량·ASP
  2. 소재(material)  CCL·동박·드라이필름 — **완제품에 선행한다**
  3. 장비(equipment) 레이저 가공기·실장기 — CAPEX, 수요 합계에서 뺀다

  소재 수입 단가(CCL $/kg)는 원가, 완제품 수출 단가는 판가다. 둘의 시차 회귀
  잔차가 마진 프록시다 — 2차전지 레이어와 같은 구조이고, 공용 stats.best_lag 를
  쓴다(레이어가 레이어를 import 하지 않는다).

── status 의 뜻 ─────────────────────────────────────────────────────────

  probe  품명까지 확인했으나 OpenAPI 실측 전. 응답 hs_name 이 expect 와 맞으면
         집계에 들어가고, 아니면 검증 실패로 표시만 된다.
  active 실측·대조를 모두 통과해 승격된 것.
  draft  코드가 불확실하거나 규모가 묻힌다. 집계에 넣지 않고 측정만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 공용 통계만 가져온다. 다른 레이어(battery/flash)는 절대 import 하지 않는다.
from .stats import best_lag, corr, growth, ols, predict, unit_price  # noqa: F401

CONFIG = Path(__file__).resolve().parent.parent / "config" / "pcb.yaml"

GROUPS = ("board", "material", "equipment")
# 수요 합계에 넣는 축. 장비는 설비투자라 수요의 흐름이 아니다.
DEMAND_GROUPS = ("board", "material")

GROUP_LABEL = {"board": "완제품(인쇄회로)", "material": "소재(CCL·동박·감광재)",
               "equipment": "장비(CAPEX)"}

# 턴어라운드/가속 문턱 — battery 와 같은 기준을 쓴다(탭마다 다르면 비교가 안 된다)
TURN_RECENT_MIN = 5.0     # %
ACCEL_MIN = 10.0          # %p

# 두 코드를 합쳐도 되는지의 문턱. 프로젝트 합산 원칙(2026-09-18 감사).
MERGE_OK_R = 0.6
MERGE_NO_R = 0.4


@dataclass
class Code:
    code: str
    label: str
    group: str = ""
    status: str = "probe"          # probe | active | draft
    purity: str = ""               # high | medium | low | unknown
    expect: str = ""               # 응답 hs_name 에 이 문자열이 있어야 집계에 넣는다
    cost: bool = False             # 수입 단가를 원가 축으로 쓰는가
    note: str = ""

    @property
    def countable(self) -> bool:
        """집계 후보. draft 는 영원히 아니다 — 실측돼도 사람이 승격해야 한다."""
        return self.status in ("probe", "active")

    @property
    def hs6(self) -> str:
        return self.code[:6]


@dataclass
class Place:
    match: str
    why: str = ""


@dataclass
class Intl:
    key: str
    name: str
    who: str = ""
    need_key: str | None = None
    free: bool = True
    have: bool = False
    signup: str = ""
    lag: str = ""
    why: str = ""


@dataclass
class PcbConfig:
    version: str
    window: int = 8
    recent: int = 3
    hist: int = 30
    leadlag_max_months: int = 6
    max_lag_months: int = 4
    min_months: int = 12
    min_r2: float = 0.25
    markets: list[str] = field(default_factory=list)
    hubs: dict[str, str] = field(default_factory=dict)
    groups: dict[str, list[Code]] = field(default_factory=dict)
    region_codes: list[str] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    intl: list[Intl] = field(default_factory=list)

    # ------------------------------------------------------------ 코드 모음

    def all(self) -> list[Code]:
        return [c for g in GROUPS for c in self.groups.get(g, [])]

    def group_codes(self, group: str, countable_only: bool = False) -> list[str]:
        return [c.code for c in self.groups.get(group, [])
                if (c.countable or not countable_only)]

    def all_codes(self) -> list[str]:
        return sorted({c.code for c in self.all()})

    def all_parents(self) -> list[str]:
        """품목별 국가별 API 는 6단위로 요청하면 하위 10단위를 전부 준다."""
        return sorted({c.code[:6] for c in self.all()})

    def find(self, code: str) -> Code | None:
        return next((c for c in self.all() if c.code == code), None)

    def cost_codes(self) -> list[Code]:
        return [c for c in self.all() if c.cost]

    def drafts(self) -> list[Code]:
        return [c for c in self.all() if c.status == "draft"]

    # ------------------------------------------------------------ 검증

    def validate(self) -> list[str]:
        errs: list[str] = []
        seen: set[str] = set()
        for c in self.all():
            if not (c.code.isdigit() and len(c.code) == 10):
                errs.append(f"'{c.code}'({c.label}) 는 10자리 HSK 가 아니다")
            if c.status not in ("probe", "active", "draft"):
                errs.append(f"'{c.code}' status 는 probe|active|draft (현재 {c.status!r})")
            # probe 는 기계 대조가 전부다. expect 가 비면 대조가 통과로 위장된다.
            if c.status == "probe" and not c.expect:
                errs.append(f"'{c.code}'({c.label}) 는 probe 인데 expect 가 없다 "
                            "— 품명 대조 없이 집계에 들어가면 probe 의 의미가 없다")
            if c.code in seen:
                errs.append(f"'{c.code}' 가 두 번 나온다")
            seen.add(c.code)
        if not self.groups.get("board"):
            errs.append("board(완제품 HS 8534) 가 비어 있다 — 이 레이어의 축이다")
        for g in self.groups:
            if g not in GROUPS:
                errs.append(f"알 수 없는 그룹 '{g}' (허용: {GROUPS})")
        for m in self.markets:
            # YAML 1.1 이 NO/ON/OFF 를 불리언으로 읽는다. 따옴표가 빠지면 여기서 잡힌다.
            if not isinstance(m, str) or len(m) != 2 or not m.isupper():
                errs.append(f"시장코드 {m!r} 형식 오류 (따옴표 누락 의심)")
        for h in self.hubs:
            if not isinstance(h, str) or len(h) != 2:
                errs.append(f"허브코드 {h!r} 형식 오류 (따옴표 누락 의심)")
            elif h not in self.markets:
                errs.append(f"허브 '{h}' 가 markets 에 없다 — 수집하지 않는 국가를 "
                            "허브로 적으면 영원히 0 으로 잡힌다")
        for rc in self.region_codes:
            # 시군구 API 는 HS 6단위만 받는다. 10단위를 넣으면 조용히 빈 응답이 온다.
            if not (isinstance(rc, str) and rc.isdigit() and len(rc) == 6):
                errs.append(f"region_codes '{rc}' — 시군구 API 는 HS 6단위만 받는다")
        parents = set(self.all_parents())
        for rc in self.region_codes:
            if rc not in parents:
                errs.append(f"region_codes '{rc}' 가 어느 코드의 6단위 부모도 아니다")
        if not self.places:
            errs.append("places 가 비어 있다 — 지역 축이 통째로 사라진다")
        return errs


def _codes(raw, group: str) -> list[Code]:
    out = []
    for d in (raw or []):
        d = dict(d)
        d["code"] = str(d.get("code", ""))
        d["group"] = group
        d["note"] = " ".join(str(d.get("note", "")).split())
        d["expect"] = str(d.get("expect") or "")
        d["cost"] = bool(d.get("cost", False))
        out.append(Code(**{k: v for k, v in d.items() if k in Code.__annotations__}))
    return out


def load(path: Path | None = None) -> PcbConfig:
    cfg = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8")) or {}
    sp = cfg.get("spread") or {}
    return PcbConfig(
        version=str(cfg.get("version", "")),
        window=int(cfg.get("window", 8)),
        recent=int(cfg.get("recent", 3)),
        hist=int(cfg.get("hist", 30)),
        leadlag_max_months=int(cfg.get("leadlag_max_months", 6)),
        max_lag_months=int(sp.get("max_lag_months", 4)),
        min_months=int(sp.get("min_months", 12)),
        min_r2=float(sp.get("min_r2", 0.25)),
        markets=list(cfg.get("markets") or []),
        hubs=dict(cfg.get("hubs") or {}),
        groups={g: _codes(cfg.get(g), g) for g in GROUPS},
        region_codes=[str(x) for x in (cfg.get("region_codes") or [])],
        places=[Place(match=str(p.get("match", "")), why=str(p.get("why", "")))
                for p in (cfg.get("places") or [])],
        rejected=list(cfg.get("rejected") or []),
        intl=[Intl(**{k: v for k, v in dict(d).items() if k in Intl.__annotations__})
              for d in (cfg.get("intl") or [])],
    )


# ------------------------------------------------------------------ 판정


def verify_name(expect: str, hs_name: str | None) -> bool:
    """응답 품명이 기대 문자열을 담고 있는가.

    ★ expect 가 비면 **통과시키지 않는다.** 빈 문자열은 'in' 검사에서 항상
      True 라, 실수로 expect 를 빼먹으면 검증이 통과로 위장된다. validate() 가
      probe+expect없음을 이미 막지만, 여기서도 한 번 더 막는다.
    """
    if not expect:
        return False
    return expect in (hs_name or "")


def verdict(yoy: float | None, recent_yoy: float | None) -> dict:
    """성장/가속/턴어라운드. 기저가 낮으면 가속은 얼마든지 만들어지므로
    **부호 전환(턴어라운드)** 과 **속도(가속)** 를 따로 말한다."""
    if yoy is None or recent_yoy is None:
        return {"code": "unknown", "label": "판정 불가",
                "note": "전년 동기 데이터가 부족합니다."}
    accel = recent_yoy - yoy
    if yoy < 0 and recent_yoy >= TURN_RECENT_MIN:
        return {"code": "turnaround", "label": "턴어라운드",
                "note": f"기준창은 아직 {yoy:+.0f}% 지만 최근이 {recent_yoy:+.0f}% 로 "
                        f"부호가 바뀌었습니다."}
    if yoy < 0 and accel >= ACCEL_MIN:
        return {"code": "bottoming", "label": "감속 둔화",
                "note": f"여전히 마이너스({yoy:+.0f}%)지만 낙폭이 {accel:+.0f}%p "
                        f"줄었습니다."}
    if yoy < 0:
        return {"code": "contracting", "label": "위축",
                "note": f"기준창 {yoy:+.0f}% · 최근 {recent_yoy:+.0f}%."}
    if accel >= ACCEL_MIN:
        return {"code": "accelerating", "label": "가속",
                "note": f"최근이 {recent_yoy:+.0f}% 로 기준창({yoy:+.0f}%)보다 "
                        f"{accel:+.0f}%p 빠릅니다."}
    if accel <= -ACCEL_MIN:
        return {"code": "decelerating", "label": "감속",
                "note": f"성장 중이나 최근이 {accel:+.0f}%p 느려졌습니다."}
    return {"code": "steady", "label": "유지",
            "note": f"기준창 {yoy:+.0f}% · 최근 {recent_yoy:+.0f}%."}


def merge_verdict(r: float | None) -> dict:
    """두 코드를 한 줄로 합쳐도 되는가 (2026-09-18 감사에서 정립한 원칙).

    r >= 0.6 같은 사이클 → 합산 가능
    r <  0.4 다른 사이클 → 합치면 큰 쪽이 작은 쪽을 덮어쓴다
    """
    if r is None:
        return {"code": "unknown", "label": "표본 부족",
                "note": "월별 증감률 상관을 낼 만한 달이 부족합니다."}
    if r >= MERGE_OK_R:
        return {"code": "mergeable", "label": "합산 가능",
                "note": f"월별 증감률 상관 r={r:.2f} — 같은 사이클로 움직입니다."}
    if r < MERGE_NO_R:
        return {"code": "separate", "label": "분리 필요",
                "note": f"r={r:.2f} — 사이클이 다릅니다. 합계만 보면 큰 코드가 "
                        f"작은 코드를 덮어씁니다. 코드별로 읽으십시오."}
    return {"code": "caution", "label": "합산 주의",
            "note": f"r={r:.2f} — 경계 구간입니다. 합계와 코드별을 같이 보십시오."}


# 고부가로 보는 일본 品目. 한국에는 이 구분이 없다 — 그게 이 축을 두는 이유다.
HIGH_VALUE_JP = ("multilayer", "buildup")

# 일본 고부가 비중이 이만큼(%p) 움직여야 '믹스가 실제로 움직였다'고 본다.
MIX_PP = 1.5


def mix_cross(kr_asp_yoy: float | None, jp_mix_chg_pp: float | None,
              flat: float = 3.0, mix_pp: float = MIX_PP) -> dict:
    """한국 ASP 상승이 **믹스인가 가격인가** — 일본 品目別 비중으로 교차검증.

    한국 HS 8534 에는 층수 구분이 없어 믹스를 직접 볼 수 없다. ASP 에는 품목
    믹스·구리 가격·환율이 한꺼번에 섞인다. 일본 METI 는 品目別 생산액을 주므로
    고부가(다층+빌드업) 비중의 방향을 따로 볼 수 있다.

    ★ 두 나라 숫자를 더하지 않는다. 단위도 개념(생산 vs 수출)도 모집단도 다르다.
      **방향의 일치/불일치**만 읽는다. 일치하면 근거가 둘이 되고, 엇갈리면
      한국 ASP 를 믹스로 읽지 말라는 경고가 된다.
    """
    if kr_asp_yoy is None or jp_mix_chg_pp is None:
        return {"code": "unknown", "label": "대조 불가",
                "note": "한국 ASP 또는 일본 品目別 비중 중 한쪽이 없습니다."}
    up_kr = kr_asp_yoy > flat
    dn_kr = kr_asp_yoy < -flat
    up_jp = jp_mix_chg_pp > mix_pp
    dn_jp = jp_mix_chg_pp < -mix_pp
    if up_kr and up_jp:
        return {"code": "mix_confirmed", "label": "믹스 상승 확인",
                "note": f"한국 ASP {kr_asp_yoy:+.1f}% 와 일본 고부가 비중 "
                        f"{jp_mix_chg_pp:+.1f}%p 가 같은 방향입니다. 한국 단가 상승을 "
                        f"고다층·패키지기판 전환으로 읽을 근거가 둘이 됐습니다."}
    if up_kr and dn_jp:
        return {"code": "mix_contradicted", "label": "믹스로 읽기 어려움",
                "note": f"한국 ASP 는 {kr_asp_yoy:+.1f}% 인데 일본 고부가 비중은 "
                        f"{jp_mix_chg_pp:+.1f}%p 로 반대입니다. 한국 단가 상승은 구리 "
                        f"가격·환율·두께 쪽일 가능성이 큽니다 — 원가 대조 패널을 보십시오."}
    if up_kr:
        return {"code": "mix_unconfirmed", "label": "믹스 미확인",
                "note": f"한국 ASP 는 {kr_asp_yoy:+.1f}% 지만 일본 고부가 비중은 "
                        f"{jp_mix_chg_pp:+.1f}%p 로 거의 움직이지 않았습니다. "
                        f"믹스 전환의 외부 근거가 아직 없습니다."}
    if dn_kr and dn_jp:
        return {"code": "mix_down", "label": "믹스 후퇴",
                "note": f"한국 ASP {kr_asp_yoy:+.1f}% · 일본 고부가 비중 "
                        f"{jp_mix_chg_pp:+.1f}%p. 양쪽이 같이 빠집니다."}
    return {"code": "mixed", "label": "혼재",
            "note": f"한국 ASP {kr_asp_yoy:+.1f}% · 일본 고부가 비중 "
                    f"{jp_mix_chg_pp:+.1f}%p. 방향이 정리되지 않았습니다."}


def asp_caveat(cu_note: str = "") -> str:
    """ASP 를 믹스 지표로 읽을 때의 한계. 화면에 그대로 싣는다."""
    return ("ASP($/kg)는 한국 데이터로 고부가 전환을 볼 수 있는 유일한 창이지만, "
            "품목 믹스·구리 가격·환율·기판 두께가 한 숫자에 섞입니다. "
            "구리가 오르면 믹스가 그대로여도 ASP 가 오릅니다 — 그래서 "
            "**동박·CCL 수입 단가를 같이** 보고, 원가로 설명되지 않는 부분만 "
            "믹스로 읽어야 합니다." + (" " + cu_note if cu_note else ""))
