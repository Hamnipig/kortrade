"""행정구역 개편 대응.

이 파이프라인은 시군구를 **이름**으로 키잉한다. 관세청 API 가 시군구 코드를 주지 않고
`sggNm`(한글명)만 주기 때문이다. 그래서 행정구역이 개편되면 같은 실체가 다른 이름이 되고
시계열이 두 동강 난다. 보정하지 않으면 개편을 급감/급증으로 오독한다.

실측 사례 (2026-07 인천 개편):
    인천광역시 중구   2025-01 ~ 2026-06  (2026-06 $119M)
    인천광역시 제물포구 2026-07 ~          (2026-07 $140M)
  → 이름만 보면 중구는 -47% YoY 급감. 실제로는 개칭.

두 가지를 제공한다.
  canonical_sigungu()      설정에 등재된 구명을 대표명으로 접는다 (조회 시점 변환, 원본 불변)
  detect_boundary_changes() 미등재 개편을 데이터에서 자동 탐지해 후보로 제시한다
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=1)
def _cfg() -> dict:
    path = CONFIG_DIR / "regions.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def _alias_index() -> dict[tuple[str, str], str]:
    """(시도접두2자, 구명) -> 대표명. 시도를 같이 보는 이유: '중구'는 여러 시도에 있다."""
    idx: dict[tuple[str, str], str] = {}
    for sido, entries in (_cfg().get("sigungu_aliases") or {}).items():
        head = sido[:2]
        for canon, spec in entries.items():
            target = spec.get("canonical", canon)
            idx[(head, canon)] = target
            for old in spec.get("former", []):
                idx[(head, old)] = target
    return idx


def canonical_sigungu(sido_name: str, sigungu_name: str) -> str:
    """개편 전후 이름을 하나의 대표명으로 접는다. 미등재면 원래 이름 그대로."""
    return _alias_index().get(((sido_name or "")[:2], sigungu_name), sigungu_name)


def canonical_aliases(sido_name: str, sigungu_name: str) -> list[str]:
    """어떤 이름이 주어지든, 같은 실체로 묶인 모든 표기를 돌려준다.

    조회 시 WHERE sigungu_name IN (...) 로 쓰면 개편 전후가 한 계열로 합쳐진다.
    """
    head = (sido_name or "")[:2]
    target = canonical_sigungu(sido_name, sigungu_name)
    names = {sigungu_name, target}
    for (h, name), canon in _alias_index().items():
        if h == head and canon == target:
            names.add(name)
    return sorted(names)


def sido_reorgs() -> list[dict]:
    return list(_cfg().get("sido_reorg") or [])


def breaks() -> list[dict]:
    """시계열 단절 구간. 리포트에서 경고로 띄운다."""
    return list(_cfg().get("breaks") or [])


def break_warning(periods: list[str], sido_name: str | None = None) -> str | None:
    """조회 구간이 단절 시점을 걸치면 경고 문자열을 만든다."""
    if not periods:
        return None
    lo, hi = min(periods), max(periods)
    hits = []
    for b in breaks():
        p = b["period"]
        if lo < p <= hi:
            scope = b.get("scope") or []
            if sido_name and scope and not any(s[:2] == sido_name[:2] for s in scope):
                continue
            hits.append(f"{p} {b.get('reason', '행정구역 개편')}")
    if not hits:
        return None
    return "⚠ 시계열 단절 구간 포함: " + " / ".join(hits)


# ------------------------------------------------------------------ 자동 탐지

def detect_boundary_changes(store, min_total_usd: float = 20_000_000,
                            table: str = "region_trade") -> list[dict]:
    """설정에 없는 개편을 데이터에서 찾아낸다.

    같은 시도 안에서 어떤 달에 **사라진 시군구**와 **그 달에 처음 나타난 시군구**가
    동시에 있으면 개편 후보다. 금액 수준이 비슷하면 확신도가 올라간다.
    자동으로 매핑하지는 않는다 — 후보만 제시하고 확정은 사람이 한다.
    """
    df = store.frame(
        f"SELECT sido_name, sigungu_name, MIN(period) p0, MAX(period) p1,"
        f" SUM(exp_usd) tot, COUNT(*) n FROM {table}"
        f" WHERE exp_usd > 0 GROUP BY sido_name, sigungu_name"
    )
    if df.empty:
        return []

    overall_max = store.frame(f"SELECT MAX(period) m FROM {table}")["m"].iloc[0]
    overall_min = store.frame(f"SELECT MIN(period) m FROM {table}")["m"].iloc[0]
    df = df[df["tot"].astype(float) >= min_total_usd]

    out: list[dict] = []
    for sido, g in df.groupby("sido_name"):
        gone = g[g["p1"] < overall_max]          # 중간에 끊긴 것
        born = g[g["p0"] > overall_min]          # 중간에 생긴 것
        for _, d in gone.iterrows():
            nxt = _next_period(d["p1"])
            cand = born[born["p0"] == nxt]
            for _, b in cand.iterrows():
                a_amt, b_amt = float(d["tot"]) / max(d["n"], 1), float(b["tot"]) / max(b["n"], 1)
                ratio = b_amt / a_amt if a_amt else float("inf")
                out.append({
                    "시도": sido,
                    "사라진_시군구": d["sigungu_name"], "마지막_월": d["p1"],
                    "등장_시군구": b["sigungu_name"], "첫_월": b["p0"],
                    "월평균_전": round(a_amt), "월평균_후": round(b_amt),
                    "규모비": round(ratio, 2),
                    "확신도": "높음" if 0.5 <= ratio <= 2.0 else "낮음 — 분할/통합 가능성",
                })
    return sorted(out, key=lambda r: -r["월평균_후"])


def _next_period(p: str) -> str:
    y, m = int(p[:4]), int(p[5:7])
    t = y * 12 + m           # (m-1)+1
    return f"{t // 12:04d}-{t % 12 + 1:02d}"
