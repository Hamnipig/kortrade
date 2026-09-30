"""PCB 해외 대조축 — 설정과 **순수 파싱 로직**.

네트워크는 scripts/run_pcb_intl.py 가 맡고, 이 모듈은 응답을 해석하는 규칙만
갖는다. 그렇게 나눠야 키 없이도 테스트가 돌고, 화면이 비었을 때 '못 받은 것'과
'받았는데 못 읽은 것'이 구분된다.

── 왜 statsDataId 를 박아 두지 않는가 ────────────────────────────────────

  2차전지 최종 수요 축에서 EIA 라우트를 상수로 박았다가, 실제 라우트가 다르고
  facet URL 에 슬래시가 빠져 있어 '배터리 축 못 찾음'만 반복한 적이 있다.
  ID·코드는 개정되고 재편된다. 그래서 여기서는

    · 통계표는 getStatsList 로 **매번 찾고** (점수로 고른다)
    · 品目은 코드가 아니라 **이름(CLASS @name)으로 맞추고**
    · 고른 것과 후보 전체를 verify 파일에 남긴다

  화면이 비면 추측할 필요 없이 그 파일이 이유를 말한다.

── 시간 코드 ─────────────────────────────────────────────────────────────

  e-Stat 의 @time 은 '2026000808'(2026년 8월) 같은 코드다. 앞 4자리가 연도,
  뒤 네 자리가 시작월·종료월이다. 다만 통계표마다 편차가 있어서 **메타의
  이름('2026年8月')을 먼저 읽고**, 그게 없을 때만 코드 패턴으로 떨어진다.
  이름이 더 안정적이다 — 코드 체계는 개정되지만 사람이 읽는 이름은 남는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "pcb_intl.yaml"

SOURCE = "estat"          # demand_series.source 값


@dataclass
class Item:
    key: str
    label: str
    match: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class Measure:
    key: str
    label: str
    match: list[str] = field(default_factory=list)


@dataclass
class Pending:
    key: str
    name: str
    who: str = ""
    need_key: str | None = None
    have: bool = False
    lag: str = ""
    why: str = ""


@dataclass
class IntlConfig:
    version: str
    base: str
    env_key: str
    stats_code: str
    search_words: list[str] = field(default_factory=list)
    title_bonus: list[str] = field(default_factory=list)
    cycle_prefer: list[str] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    start: str = "2019-01"
    credit: str = ""
    pending: list[Pending] = field(default_factory=list)

    def item(self, key: str) -> Item | None:
        return next((i for i in self.items if i.key == key), None)

    def measure(self, key: str) -> Measure | None:
        return next((m for m in self.measures if m.key == key), None)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.base.startswith("https://"):
            errs.append("estat.base 는 https 여야 한다")
        if not (self.stats_code.isdigit() and len(self.stats_code) == 8):
            errs.append(f"stats_code '{self.stats_code}' — 8자리 숫자여야 한다")
        if not self.search_words:
            errs.append("search_words 가 비어 있다 — 통계표를 찾을 방법이 없다")
        if not self.items:
            errs.append("items 가 비어 있다")
        if not self.measures:
            errs.append("measures 가 비어 있다")
        seen: set[str] = set()
        for i in self.items:
            if not i.match:
                errs.append(f"item '{i.key}' 에 match 가 없다 — 빈 매칭은 전부를 잡는다")
            if i.key in seen:
                errs.append(f"item key '{i.key}' 중복")
            seen.add(i.key)
        for m in self.measures:
            if not m.match:
                errs.append(f"measure '{m.key}' 에 match 가 없다")
        if not parse_period(self.start + "-01") and not re.fullmatch(r"\d{4}-\d{2}", self.start):
            errs.append(f"start '{self.start}' 는 YYYY-MM 이어야 한다")
        if "e-Stat" not in self.credit:
            errs.append("credit 문구가 없다 — 이용규약 제7조가 출처 표시를 의무로 둔다")
        return errs


def _list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def load(path: Path | None = None) -> IntlConfig:
    raw = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8")) or {}
    es = raw.get("estat") or {}
    return IntlConfig(
        version=str(raw.get("version", "")),
        base=str(es.get("base", "")),
        env_key=str(es.get("env_key", "EJ_ESTAT_APP_ID")),
        stats_code=str(es.get("stats_code", "")),
        search_words=_list(es.get("search_words")),
        title_bonus=_list(es.get("title_bonus")),
        cycle_prefer=_list(es.get("cycle_prefer")),
        measures=[Measure(key=str(d.get("key", "")), label=str(d.get("label", "")),
                          match=_list(d.get("match"))) for d in (es.get("measures") or [])],
        items=[Item(key=str(d.get("key", "")), label=str(d.get("label", "")),
                    match=_list(d.get("match")), exclude=_list(d.get("exclude")),
                    why=" ".join(str(d.get("why", "")).split()))
               for d in (es.get("items") or [])],
        start=str(es.get("start", "2019-01")),
        credit=" ".join(str(es.get("credit", "")).split()),
        pending=[Pending(key=str(d.get("key", "")), name=str(d.get("name", "")),
                         who=str(d.get("who", "")), need_key=d.get("need_key"),
                         have=bool(d.get("have", False)), lag=str(d.get("lag", "")),
                         why=" ".join(str(d.get("why", "")).split()))
                 for d in (raw.get("pending") or [])],
    )


# ------------------------------------------------------------------ 파싱

_KANJI_YM = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_CODE_YM = re.compile(r"^(\d{4})\d{2}(\d{2})(\d{2})$")


def parse_period(name: str | None, code: str | None = None) -> str | None:
    """e-Stat 시간 항목 → 'YYYY-MM'. 월차가 아니면 None.

    이름('2026年8月')을 먼저 본다. 코드 체계는 개정되지만 이름은 남는다.
    코드로 떨어질 때는 시작월 == 종료월인 것만 받는다 — '2026000406'(4~6월기)을
    월차로 읽으면 분기값이 월값 자리에 들어가 전부 3배로 보인다.
    """
    if name:
        m = _KANJI_YM.search(str(name))
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}"
    if code:
        m = _CODE_YM.match(str(code).strip())
        if m:
            y, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a == b and 1 <= a <= 12:
                return f"{y:04d}-{a:02d}"
    return None


def matches(name: str, want: list[str], avoid: list[str] | None = None) -> bool:
    """이름 매칭. **빈 want 는 절대 통과시키지 않는다.**

    빈 리스트로 any() 를 돌리면 False 라 괜찮아 보이지만, 빈 문자열이 하나라도
    섞이면 `"" in name` 이 항상 True 가 되어 전부를 잡는다. 그러면 品目 하나가
    통계표 전체를 삼킨다.
    """
    want = [w for w in (want or []) if w]
    if not want or not name:
        return False
    if any(a for a in (avoid or []) if a and a in name):
        return False
    return any(w in name for w in want)


def pick_item(name: str, items: list[Item]) -> Item | None:
    """品目 하나를 고른다. **가장 구체적인 것이 이긴다.**

    '多層プリント配線板' 은 pcb_total('電子回路基板')에는 안 걸리지만,
    'ビルドアップ多層配線板' 은 multilayer('多層')에도 걸린다. 매칭 문자열이
    긴 쪽 = 더 구체적인 쪽을 고르고, exclude 로 한 번 더 막는다.
    """
    best, best_len = None, -1
    for it in items:
        hits = [w for w in it.match if w and w in name]
        if not hits:
            continue
        if any(a for a in it.exclude if a and a in name):
            continue
        n = max(len(w) for w in hits)
        if n > best_len:
            best, best_len = it, n
    return best


def series_key(item_key: str, measure_key: str) -> str:
    return f"jp:{item_key}:{measure_key}"


def split_series(key: str) -> tuple[str, str] | None:
    p = key.split(":")
    if len(p) != 3 or p[0] != "jp":
        return None
    return p[1], p[2]


def status_of(payload: dict, root: str) -> tuple[int | None, str]:
    """e-Stat 응답의 RESULT.STATUS. 0 이 정상이고 그 외는 에러 메시지를 준다.

    ★ HTTP 200 이어도 STATUS 가 0 이 아닐 수 있다. 상태코드만 보고 성공으로
      처리하면 '행 0개'와 '파라미터 오류'가 같은 얼굴이 된다.
    """
    r = ((payload or {}).get(root) or {}).get("RESULT") or {}
    st = r.get("STATUS")
    try:
        st = int(st)
    except (TypeError, ValueError):
        st = None
    return st, str(r.get("ERROR_MSG") or "")


def as_list(v) -> list:
    """e-Stat JSON 은 원소가 하나면 배열이 아니라 객체로 온다."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]
