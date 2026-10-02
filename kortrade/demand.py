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
    미국 수입   (US Census, 달러/월)     시장 규모 + **선적 원산지** 구성 → (c)

★ 미국 수입의 한국 비중은 **기업 점유율이 아니다.** 한국 기업이 미국에서 만들면
  그 물량은 수입에 아예 안 잡힌다. 그래서 비중 하락 하나로 '경쟁 패배'라고 말할 수
  없고, 현지화지수(부품·소재/완제품)를 두 번째 조건으로 반드시 함께 본다.
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
class Base:
    """한국 기업의 해외 생산기지 — 원산지 귀속 보정에 쓰는 한 줄.

    match 는 **Census 응답의 CTY_NAME** 과 맞춘다. 코드로 맞추면 오타가 조용히
    다른 나라를 센다. code 는 사람이 읽는 참고값일 뿐 매칭에 쓰지 않는다.
    """
    match: str
    label: str = ""
    code: str = ""
    who: str = ""
    kind: str = ""
    why: str = ""


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
    bases: list[Base] = field(default_factory=list)
    cap_enabled: bool = True
    routes: list[str] = field(default_factory=list)
    facet_candidates: list[str] = field(default_factory=list)
    match_any: list[str] = field(default_factory=lambda: ["batter", "storage"])
    match_codes: list[str] = field(default_factory=lambda: ["MWH"])
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
        for b in self.bases:
            # ★ 집계그룹(0으로 시작)을 기지로 적으면 전세계 합계와 이중계상된다
            if b.code and b.code.startswith("0"):
                errs.append(f"kr_bases '{b.match}': 0 으로 시작하는 코드는 "
                            f"국가가 아니라 집계그룹이다")
            if b.match == self.partner or b.match in ("KOREA", "KOREA SOUTH"):
                errs.append("kr_bases 에 한국을 넣으면 한국이 두 번 더해진다")
        if not (self.partner.isdigit() and len(self.partner) == 4):
            errs.append(f"partner '{self.partner}': Census CTY_CODE 는 4자리")
        if self.cap_enabled and not self.routes:
            errs.append("us_capacity.routes 가 비어 있음")
        if self.cap_enabled and not (self.match_any or self.match_codes):
            errs.append("us_capacity: 배터리를 찾을 단서(match_any/match_codes)가 없음")
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
        bases=[Base(match=str(b.get("match", "")).upper(),
                    label=b.get("label", ""), code=str(b.get("code", "")),
                    who=b.get("who", ""), kind=b.get("kind", ""),
                    why=b.get("why", ""))
               for b in (raw.get("kr_bases") or []) if b.get("match")],
        cap_enabled=bool(cap.get("enabled", True)),
        routes=[str(r) for r in (cap.get("routes") or [])],
        facet_candidates=[str(f) for f in (cap.get("facet_candidates") or [])],
        match_any=[str(x).lower() for x in (cap.get("match_any")
                                            or [cap.get("match", "batter")])],
        match_codes=[str(x).upper() for x in (cap.get("match_codes") or ["MWH"])],
        status_filter=[str(s) for s in (cap.get("status_filter") or [])],
        flat_pct=float(th.get("flat_pct", 5.0)),
        share_pp=float(th.get("share_pp", 3.0)),
        min_base_usd=float(th.get("min_base_usd", 20_000_000.0)),
    )


def is_base(cfg: DemandConfig, cty_code: str, cty_name: str = "") -> bool:
    """이 원산지가 한국 기업의 해외 생산기지인가.

    이름으로 먼저 맞추고(응답이 준 CTY_NAME), 이름이 비었을 때만 코드로 맞춘다.
    ★ 집계그룹(0으로 시작)은 어떤 경우에도 기지가 아니다 — 들어오면 전세계
      합계와 이중계상된다.
    """
    cc = str(cty_code or "")
    if cc.startswith("0"):
        return False
    nm = str(cty_name or "").upper()
    for b in cfg.bases:
        if nm and b.match in nm:
            return True
        if not nm and b.code and b.code == cc:
            return True
    return False


def base_of(cfg: DemandConfig, cty_code: str, cty_name: str = "") -> Base | None:
    """맞는 기지 정의를 돌려준다 (화면에 who·kind 를 같이 쓰기 위해)."""
    cc, nm = str(cty_code or ""), str(cty_name or "").upper()
    if cc.startswith("0"):
        return None
    for b in cfg.bases:
        if (nm and b.match in nm) or (not nm and b.code and b.code == cc):
            return b
    return None


# ── 판정 ─────────────────────────────────────────────────────────────────

def _sign(x: float | None, flat: float) -> int | None:
    if x is None:
        return None
    return +1 if x > flat else (-1 if x < -flat else 0)


LOC_JUMP = 1.2      # 현지화지수가 이 배수 이상 오르면 '생산지 이동 진행 중'으로 본다


def attribute(kr_yoy: float | None, imp_yoy: float | None,
              share_now: float | None, share_prev: float | None,
              cap_yoy: float | None, flat: float = 5.0,
              share_pp: float = 3.0,
              loc_now: float | None = None,
              loc_prev: float | None = None,
              wide_now: float | None = None,
              wide_prev: float | None = None) -> dict:
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

    # ★ 데이터는 있는데 **전년 동기가 없는** 경우를 '축 없음'이라고 말하면 안 된다.
    #   2026-09-28 실측: 미국 BESS 수입($9,846M)이 화면에 찍혀 있는데 판정은
    #   "미국 축 없음"이었다. 실제 상황은 "신설 통계품목이라 전년치가 없다"이고,
    #   그건 시간이 해결한다는 점에서 전혀 다른 이야기다.
    if i is None and c is None and share_now is not None:
        return {"code": "level_only", "label": "수준만 관측 (전년 없음)",
                "shareChg": d_share,
                "note": f"미국 수입은 관측되지만 **전년 동기 데이터가 없어** 변화율을 "
                        f"낼 수 없습니다 — 신설 통계품목일 수 있습니다. 현재 한국 비중은 "
                        f"{share_now}% 입니다. 12개월이 쌓이면 점유율 변화가 나옵니다."}

    # 미국 축이 하나도 없으면 기존 현지화 판정 이상을 말할 수 없다
    if i is None and c is None:
        return {"code": "partial", "label": "미국 축 없음", "shareChg": d_share,
                "note": "미국 수입·설치 데이터가 없어 '현지화'와 '점유율 상실'을 "
                        "구분할 수 없습니다. 현지화지수만으로 판단하십시오."}

    # ── 미국 수입 비중이 빠졌을 때 ─────────────────────────────────────
    # ★ 2026-09-29 정정. 전에는 이 한 조건만으로 '점유율 상실'이라고 했는데 **틀렸다.**
    #
    #   미국 수입의 한국 비중은 **선적 원산지 비중**이지 한국 기업의 점유율이 아니다.
    #   LG엔솔·삼성SDI·SK온이 미시간·조지아에서 만들어 팔면 그 물량은 미국 수입에
    #   **아예 잡히지 않는다.** 즉 현지화가 진행될수록 한국 비중은 반드시 떨어진다.
    #   같은 그림이 세 가지 원인에서 나온다:
    #     (a) 중국 등 경쟁사에 밀렸다                  → 경쟁 패배
    #     (b) 한국 기업이 미국에서 만든다              → 현지화 (실적은 오히려 좋다)
    #     (c) 폴란드·헝가리 공장에서 미국으로 보낸다   → 기지 재배치
    #
    #   그래서 **두 번째 조건 없이는 경쟁 패배라고 말하지 않는다.**
    #   두 번째 조건은 현지화지수((부품+소재)/완제품)다 — 한국 기업이 미국에서
    #   조립하고 있으면 부품·소재는 계속 한국에서 나가므로 이 지수가 오른다.
    #   (c)는 화면의 원산지 분해표에서 폴란드·헝가리가 오르는 것으로 보인다.
    loc_up = (loc_now is not None and loc_prev is not None and loc_prev > 0
              and loc_now / loc_prev >= LOC_JUMP)
    # 한국기업 귀속 비중(한국 + 해외기지) 의 변화. 한국發 비중과 **갈라지는지**가
    # 기지 이전과 경쟁 패배를 가르는 가장 직접적인 증거다.
    d_wide = (None if (wide_now is None or wide_prev is None)
              else round(wide_now - wide_prev, 1))
    if i is not None and i > 0 and d_share is not None and d_share <= -share_pp:
        # ★ 2026-10-02 추가. 한국發 비중은 빠졌는데 **한국기업 귀속 비중은
        #   지켜졌다면** 물건이 사라진 게 아니라 **선적지가 바뀐 것**이다.
        #   (말레이시아·폴란드·헝가리 공장에서 미국으로 직송)
        #   이 경우를 '점유율 상실'이라고 쓰면 멀쩡한 기업을 구조적 훼손으로
        #   오판한다. 기지 축이 있을 때는 현지화지수보다 이 증거가 더 직접적이다.
        if d_wide is not None and d_wide > -share_pp:
            return {"code": "base_shift", "label": "기지 이전 (선적지만 바뀜)",
                    "shareChg": d_share, "wideChg": d_wide,
                    "note": "한국發 비중은 떨어졌지만 **한국기업 귀속 비중(한국＋해외"
                            "기지)은 지켜졌습니다.** 물량이 사라진 것이 아니라 선적지가 "
                            "해외 기지로 옮겨간 쪽입니다 — 통관 수출은 줄지만 그 기업의 "
                            "실적은 유지되거나 늘 수 있습니다. 어느 기지가 받았는지는 "
                            "원산지 분해표에서 확인하십시오. ※ 귀속 비중은 **상한**입니다 "
                            "(그 나라發 전량이 한국 기업 물량이라는 보장은 없습니다)."}
        # ★ 순서가 중요하다. 2026-10-02 테스트가 잡은 실수:
        #   기지 관측이 없을 때 wide 는 한국發과 같은 값이 되므로 "둘 다 하락"이
        #   항상 참이 되어, 미국 **현지 조립**으로 설명되는 localizing 판정을
        #   통째로 덮어 버렸다. 미국 내 생산은 어느 나라 원산지에도 안 잡히므로
        #   귀속 비중이 떨어진다는 사실은 현지화를 **배제하지 않는다.**
        #   그래서 현지화지수 증거를 먼저 본다.
        if loc_up:
            return {"code": "localizing", "label": "현지화 (비중 하락은 생산지 이동)",
                    "shareChg": d_share, "wideChg": d_wide,
                    "note": "미국 수입에서 한국 비중은 떨어졌지만 **현지화지수가 함께 "
                            "올랐습니다.** 부품·소재는 계속 한국에서 나가고 있다는 뜻이므로, "
                            "경쟁 패배가 아니라 조립이 미국으로 옮겨간 쪽입니다. "
                            "현지법인 매출은 DART 부문정보로 확인하십시오."}
        if d_wide is not None and d_wide <= -share_pp:
            return {"code": "share_loss", "label": "점유율 상실 (기지 포함해도 하락)",
                    "shareChg": d_share, "wideChg": d_wide,
                    "note": "한국發 비중과 **해외 기지를 더한 귀속 비중이 함께 "
                            "떨어졌습니다.** 선적지 이동으로 설명되지 않습니다 — "
                            "기지 이전·현지화 가설이 모두 배제되므로 경쟁에서 밀린 "
                            "쪽입니다. 원산지 분해표에서 어느 나라가 그 자리를 "
                            "가져갔는지 확인하십시오."}
        if loc_up:
            return {"code": "localizing", "label": "현지화 (비중 하락은 생산지 이동)",
                    "shareChg": d_share,
                    "note": "미국 수입에서 한국 비중은 떨어졌지만 **현지화지수가 함께 "
                            "올랐습니다.** 부품·소재는 계속 한국에서 나가고 있다는 뜻이므로, "
                            "경쟁 패배가 아니라 조립이 미국으로 옮겨간 쪽입니다. "
                            "현지법인 매출은 DART 부문정보로 확인하십시오."}
        if loc_now is None or loc_prev is None:
            return {"code": "share_down_unknown", "label": "비중 하락 · 원인 미상",
                    "shareChg": d_share,
                    "note": "미국 수입 시장은 커지는데 한국 비중이 떨어졌습니다. "
                            "다만 이것만으로는 **경쟁 패배와 현지화를 구분할 수 없습니다** — "
                            "한국 기업이 미국에서 만들면 수입 통계에서 아예 사라지기 "
                            "때문입니다. 현지화지수와 원산지 분해표를 함께 보십시오."}
        return {"code": "share_loss", "label": "점유율 상실 의심", "shareChg": d_share,
                "note": "미국 수입 시장은 커지는데 한국 비중이 떨어졌고, **현지화지수도 "
                        "오르지 않았습니다.** 생산지 이동으로 설명되지 않으므로 경쟁에서 "
                        "밀렸을 가능성이 큽니다. 원산지 분해표에서 어느 나라가 그 자리를 "
                        "가져갔는지 확인하십시오 — 폴란드·헝가리면 한국 기업의 유럽 기지일 "
                        "수 있어 해석이 또 달라집니다."}

    # ── 설치는 느는데 수입이 줄면 미국 내 생산이 대체한 것이다
    if c is not None and c > 0 and i is not None and i < 0:
        return {"code": "onshoring", "label": "현지 생산 대체", "shareChg": d_share,
                "note": "미국 설치는 느는데 수입은 줄었습니다. 미국 내 생산이 수입을 "
                        "대체하는 국면입니다. 한국 기업의 현지 공장 가동 여부는 "
                        "통관 데이터에 없으므로 공시로 확인해야 합니다."}

    # ── 설치 축만 있는 경우 — 말할 수 있는 데까지만 말한다
    # (Census 키는 2026-05-12 부터 필수가 됐고 발급 메일이 늦는 일이 있다.
    #  EIA 키만 먼저 들어와도 '수요 위축은 아니다'까지는 확정할 수 있다.)
    if i is None and c is not None and c > 0:
        return {"code": "demand_ok_partial", "label": "수요 견조 · 원인 구분 불가",
                "shareChg": d_share,
                "note": "미국 설치는 늘고 있습니다 — **수요 위축은 아닙니다.** 다만 한국 수출 "
                        "감소가 현지 생산 전환인지 점유율 상실인지는 미국 수입 데이터 없이 "
                        "구분할 수 없습니다. CENSUS_API_KEY 를 넣으면 갈라집니다."}

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
    """첫 유효값 = 100 으로 지수화. 단일 계열용.

    ★ 0 은 None 으로 둔다. 0 으로 찍으면 선이 바닥에 깔려 **'그 달 수입이 0이었다'**
      로 읽히는데, 실제로는 대개 '그 품목이 아직 없었다'이다.
    """
    base = next((v for v in values if v not in (None, 0)), None)
    if not base:
        return None
    return [None if not v else round(v / base * 100, 1) for v in values]


def common_index(series: dict[str, list[float | None]]) -> dict:
    """여러 계열을 **같은 기준월**로 지수화한다.

    ★ 이게 왜 중요한가 (2026-09-28 실측 사고)
      계열마다 제 첫 유효값을 100으로 잡으면, 시작점이 다른 계열끼리는 비교가
      **무의미해진다.** 실제로 한국 수출은 2024-03부터, 미국 BESS 수입은 2026-01부터
      데이터가 있었는데(통계품목 신설로 추정), 각자 100에서 출발시키니 화면에서는
      "미국 수입이 폭증했다"로 보였다. 폭증한 것이 아니라 **없던 계열이 생긴 것**이다.

      그래서 모든 계열이 값을 갖는 **첫 달**을 공통 기준으로 잡는다.
      겹치는 달이 없으면 지수를 만들지 않는다 — 비교할 수 없다고 말하는 편이
      틀린 그림을 그리는 것보다 낫다.

    반환: {"base": 인덱스 | None, "idx": {키: 계열} | None,
           "starts": {키: 첫 유효 인덱스}, "note": 설명}
    """
    live = {k: v for k, v in series.items() if v and any(x for x in v)}
    if not live:
        return {"base": None, "idx": None, "starts": {}, "note": "데이터가 없습니다."}

    n = max(len(v) for v in live.values())
    starts = {k: next((i for i, x in enumerate(v) if x), None) for k, v in live.items()}

    base = None
    for i in range(n):
        if all(i < len(v) and v[i] for v in live.values()):
            base = i
            break
    if base is None:
        return {"base": None, "idx": None, "starts": starts,
                "note": "계열들의 관측 구간이 겹치지 않아 지수 비교를 할 수 없습니다. "
                        "각 계열의 시작 시점이 다릅니다 — 신설 통계품목일 수 있습니다."}

    out = {}
    for k, v in live.items():
        b = v[base]
        out[k] = [None if not x else round(x / b * 100, 1) for x in v]
    return {"base": base, "idx": out, "starts": starts, "note": ""}
