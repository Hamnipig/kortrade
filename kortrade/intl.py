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
    # ★ 生産·出荷·在庫 가 **한 축에 같이 들어 있다.** '金額' 하나로 맞추면
    #   생산금액 자리에 재고금액이 섞여 YoY 가 통째로 다른 이야기가 된다.
    exclude: list[str] = field(default_factory=list)


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
    probe_top: int = 8
    # 「製品月表」 처럼 **표 하나가 한 달**인 계열은 vintage 를 이어붙여야
    # 시계열이 된다. 한 표가 이만큼의 월을 이미 갖고 있으면 시계열표로 보고
    # 이어붙이지 않는다.
    max_tables: int = 120
    min_months_single: int = 24
    revision_vintages: int = 15
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
        if not (1 <= self.max_tables <= 400):
            errs.append(f"max_tables {self.max_tables} — 1~400 이어야 한다")
        if not (1 <= self.revision_vintages <= 120):
            errs.append(f"revision_vintages {self.revision_vintages} — 1~120 이어야 한다")
        if not (2 <= self.min_months_single <= 240):
            errs.append(f"min_months_single {self.min_months_single} — 2~240 이어야 한다")
        if not (1 <= self.probe_top <= 20):
            errs.append(f"probe_top {self.probe_top} — 1~20 이어야 한다 "
                        "(후보마다 getMetaInfo 호출이 한 번씩 나간다)")
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
                          match=_list(d.get("match")), exclude=_list(d.get("exclude")))
                  for d in (es.get("measures") or [])],
        items=[Item(key=str(d.get("key", "")), label=str(d.get("label", "")),
                    match=_list(d.get("match")), exclude=_list(d.get("exclude")),
                    why=" ".join(str(d.get("why", "")).split()))
               for d in (es.get("items") or [])],
        start=str(es.get("start", "2019-01")),
        probe_top=int(es.get("probe_top", 8)),
        max_tables=int(es.get("max_tables", 120)),
        min_months_single=int(es.get("min_months_single", 24)),
        revision_vintages=int(es.get("revision_vintages", 15)),
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

# 和暦. e-Stat 의 오래된 통계표는 시간 항목 이름이 '平成19年11月' 로 온다
# (2026-09-30 실측: 「主要製品統計表（時系列）１３４．電子回路基板」의 time 클래스
#  이름이 '年月(H19～H25)', 항목이 '平成19年'·'平成20年11月' 이었다).
# 西暦만 읽으면 그 표는 통째로 '기간 없음'이 되어, 왜 비었는지도 알 수 없다.
_ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925, "R": 2018, "H": 1988, "S": 1925}
_ERA_YM = re.compile(r"(令和|平成|昭和|[RHS])\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月")


def parse_period(name: str | None, code: str | None = None) -> str | None:
    """e-Stat 시간 항목 → 'YYYY-MM'. 월차가 아니면 None.

    이름('2026年8月' 또는 '平成20年11月')을 먼저 본다. 코드 체계는 개정되지만
    이름은 남는다. 코드로 떨어질 때는 **시작월 == 종료월**인 것만 받는다 —
    '2026000406'(4~6월기)을 월차로 읽으면 분기값이 월 자리에 들어가 전부 3배가 된다.

    '平成19年度'(연度)처럼 月이 없는 항목은 매칭되지 않는다. 연차·연도값이 월값
    자리에 섞이는 것이 이 함수가 막아야 할 가장 큰 사고다.
    """
    if name:
        t = str(name)
        m = _KANJI_YM.search(t)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}"
        m = _ERA_YM.search(t)
        if m:
            era, yr, mo = m.group(1), m.group(2), int(m.group(3))
            n = 1 if yr == "元" else int(yr)
            y = _ERA_BASE[era] + n
            if 1 <= mo <= 12 and 1900 <= y <= 2100:
                return f"{y:04d}-{mo:02d}"
    if code:
        m = _CODE_YM.match(str(code).strip())
        if m:
            y, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a == b and 1 <= a <= 12:
                return f"{y:04d}-{a:02d}"
    return None


def survey_year(survey_date) -> int | None:
    """TABLE_INF.SURVEY_DATE('201001' · '201001-201012' · 0) 에서 연도만.

    통계표 후보를 **최신성**으로 거르기 위한 것이다. 2026-09-30 실측에서
    searchWord='電子回路基板' 이 돌려준 47건이 전부 surveyDate 2010xx 의
    옛 표였고, 점수에 최신성이 없어 그중 하나가 1위로 올라갔다.
    """
    t = str(survey_date or "").strip()
    m = re.match(r"^(\d{4})", t)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1900 <= y <= 2100 else None


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


def pick_measure(name: str, measures: list[Measure]) -> Measure | None:
    """表章項目 하나를 고른다. pick_item 과 같은 규칙 — 구체적인 쪽이 이긴다.

    '生産　金額(百万円)' 은 amt 의 '生産　金額' 과 잔여 '金額' 양쪽에 걸린다.
    긴 매칭이 이기게 하고, exclude 로 出荷·在庫를 떼어낸다. 순서대로 first-match
    를 쓰면 설정에 적은 차례가 결과를 만든다 — 그건 근거가 아니다.
    """
    best, best_len = None, -1
    for m in measures:
        if any(a for a in m.exclude if a and a in name):
            continue
        hits = [w for w in m.match if w and w in name]
        if not hits:
            continue
        n = max(len(w) for w in hits)
        if n > best_len:
            best, best_len = m, n
    return best


_UNIT_IN_NAME = re.compile(r"[（(]([^）)]+)[）)]\s*$")


def unit_from_name(name: str | None) -> str:
    """'生産　金額(百万円)' → '百万円'.

    실측상 단위가 CLASS 의 @unit 이 아니라 **항목 이름 괄호 안에** 들어 있다.
    百万円→億円 환산을 우리가 하지 않는 이유는 그 환산이 숫자를 만들기 때문이다.
    원문 단위를 그대로 들고 화면에도 원문으로 싣는다.
    """
    if not name:
        return ""
    m = _UNIT_IN_NAME.search(str(name).strip())
    return m.group(1) if m else ""


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
