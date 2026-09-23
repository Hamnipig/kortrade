"""섹터 정의 로더.

config/sectors/*.yaml 하나가 섹터 하나다. 섹터를 늘리는 일이 코드 수정이 아니라
설정 추가가 되도록 분리했다. 반도체·2차전지를 붙이려면 YAML 하나만 더 쓰면 된다.

status
  active : 수집·대시보드 대상
  draft  : HS 코드가 아직 검증되지 않은 섹터. 수집에서 제외되고 대시보드에는
           "미수집" 탭으로만 나타난다. discover_hs.py 로 코드를 확인한 뒤 승격한다.
           (검증 안 된 코드로 수집하면 조용히 빈 데이터나 엉뚱한 품목이 쌓인다)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

SECTOR_DIR = Path(__file__).resolve().parent.parent / "config" / "sectors"


@dataclass
class Sector:
    key: str
    name: str
    path: Path
    subtitle: str = ""
    status: str = "active"
    order: int = 99
    dominant: str | None = None
    categories: dict[str, dict] = field(default_factory=dict)
    countries: list[str] = field(default_factory=list)
    # 순위와 무관하게 **항상 표시할** 시군구. ODM 생산 거점처럼
    # "여기가 안 보인다"는 사실 자체가 정보인 지역을 고정해 둔다.
    # 부분 문자열로 맞춘다 — '세종'이 '세종특별자치시'로 오든 뭐로 오든 잡히게.
    pinned_places: list[dict] = field(default_factory=list)
    # 중계무역 허브로 보는 국가. {코드: {why: 근거}}.
    # 확산도를 낼 때 **허브 포함/제외 두 벌**을 내기 위해 쓴다. 허브 물량은
    # 최종 소비지가 아니라서, 빼고 봐야 진짜 시장 개척인지 알 수 있다.
    # 코드가 아니라 설정에 두는 이유: 섹터마다 다르고 시간이 지나면 바뀐다.
    hub_countries: dict[str, dict] = field(default_factory=dict)
    notes: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def codes(self) -> list[str]:
        return list(self.categories)

    def label(self, hs: str) -> str:
        return (self.categories.get(hs) or {}).get("label", hs)

    def group(self, hs: str) -> str:
        return (self.categories.get(hs) or {}).get("group", "기타")

    def validate(self) -> list[str]:
        """설정이 런타임에 조용히 깨지는 경우를 미리 잡는다."""
        errs = []
        if not self.categories:
            errs.append(f"{self.key}: categories 가 비어 있음")
        for hs in self.categories:
            if not (hs.isdigit() and len(hs) == 6):
                errs.append(f"{self.key}: '{hs}' — 시군구 API 는 HS 6단위만 받는다")
        if self.dominant and self.dominant not in self.categories:
            errs.append(f"{self.key}: dominant '{self.dominant}' 가 categories 에 없음")
        if self.status not in ("active", "draft"):
            errs.append(f"{self.key}: status 는 active/draft 만 허용")
        for cc in self.hub_countries:
            # ★ YAML 1.1 함정: 따옴표 없는 NO(노르웨이)·ON·OFF 는 **불리언**으로 파싱된다.
            #   그러면 키가 False 가 되어 조용히 아무 국가와도 안 맞는다.
            if not isinstance(cc, str):
                errs.append(f"{self.key}: hub_countries 키 {cc!r} 가 문자열이 아님 "
                            "— YAML 에서 따옴표로 감싸세요 (NO/ON/OFF 는 불리언이 됩니다)")
            elif len(cc) != 2 or not cc.isalpha():
                errs.append(f"{self.key}: hub_countries '{cc}' — 국가코드는 2자리 영문")
            elif self.countries and cc not in self.countries:
                # 수집하지 않는 국가를 허브로 적어두면 영원히 0 으로 잡혀
                # "허브 비중이 낮다"는 틀린 결론이 나온다.
                errs.append(f"{self.key}: hub_countries '{cc}' 가 countries 에 없음 "
                            "— 수집하지 않는 국가는 허브로 세어지지 않습니다")
        return errs


def load_sectors(directory: Path | None = None, include_draft: bool = True) -> list[Sector]:
    d = directory or SECTOR_DIR
    out: list[Sector] = []
    for p in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        s = Sector(
            key=raw.get("key", p.stem), name=raw.get("name", p.stem), path=p,
            subtitle=raw.get("subtitle", ""), status=raw.get("status", "active"),
            pinned_places=list(raw.get("pinned_places") or []),
            hub_countries=dict(raw.get("hub_countries") or {}),
            order=int(raw.get("order", 99)), dominant=raw.get("dominant"),
            categories=raw.get("categories") or {},
            countries=raw.get("countries") or [], notes=raw.get("notes", ""),
        )
        if not include_draft and not s.active:
            continue
        out.append(s)
    return sorted(out, key=lambda s: (s.order, s.key))


def get_sector(key: str) -> Sector:
    for s in load_sectors():
        if s.key == key:
            return s
    raise KeyError(f"섹터 '{key}' 없음. 있는 것: {[s.key for s in load_sectors()]}")


def validate_all(directory: Path | None = None) -> list[str]:
    errs: list[str] = []
    keys: set[str] = set()
    for s in load_sectors(directory):
        errs += s.validate()
        if s.key in keys:
            errs.append(f"중복 key: {s.key}")
        keys.add(s.key)
    return errs
