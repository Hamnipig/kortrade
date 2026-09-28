#!/usr/bin/env python3
"""두 SQLite DB 를 **행 단위로** 합친다. (API 호출 없음)

왜 필요한가 (2026-09-28 실측 사고)
    update.yml 과 flash.yml 이 같은 `data/kortrade.sqlite` 를 같은 브랜치에 커밋한다.
    커밋 직전에 상대가 먼저 push 하면 `git pull --rebase` 가 걸리는데,
    **sqlite 는 바이너리라 merge 가 원리적으로 불가능하다.**

        warning: Cannot merge binary files: data/kortrade.sqlite
        CONFLICT (content): Merge conflict in data/kortrade.sqlite
        fatal: You are not currently on a branch.   ← rebase 중단 → detached HEAD
        Error: Process completed with exit code 128

    한쪽을 버리는 방식(--ours/--theirs)도 답이 아니다. 버리는 쪽이 방금 쓴 수백~수천
    API 콜이기 때문이다(fetch_log 가 사라지면 다음 실행이 다시 받는다).

해법 — **git 이 아니라 SQLite 레벨에서 합친다.**
    이 프로젝트의 모든 표는 자연키에 UNIQUE 제약이 걸려 있고 UPSERT 로 쌓인다.
    그래서 "상대 DB 의 행을 내 DB 에 INSERT OR IGNORE" 하면 정확히 합쳐진다.

      · 양쪽에 다 있는 행 → **내 값이 남는다** (이번 실행이 방금 수집한 최신값)
      · 상대에만 있는 행 → 추가된다 (상대가 수집한 것을 잃지 않는다)

    revisions 만 UNIQUE 가 없는 로그성 표라 EXCEPT 로 중복을 걸러 붙인다.

★ 컬럼이 다를 수 있다. 상대 DB 가 스키마 변경 이전이면 새 표(demand_series)가
  아예 없거나 컬럼이 모자란다. 그래서 **양쪽 컬럼의 교집합만** 옮긴다.
  없는 표는 건너뛴다. 스키마를 맞추려 들지 않는다 — 합치기만 한다.

사용법:
    python scripts/merge_db.py --into data/kortrade.sqlite --from /tmp/remote.sqlite
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.store import Store

# 로그성(중복 허용) 표 — UNIQUE 가 없어 INSERT OR IGNORE 가 안 먹는다
APPEND_ONLY = {"revisions"}


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def merge(into: Path, src: Path) -> dict[str, int]:
    if not src.exists():
        return {}
    # Store() 로 먼저 열어 대상 DB 에 최신 스키마를 보장한다.
    # (상대가 신버전이고 내가 구버전이면 표가 없어서 그냥 못 받는다)
    with Store(into):
        pass

    conn = sqlite3.connect(into)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src),))
        mine = tables(conn)
        theirs = {r[0] for r in conn.execute(
            "SELECT name FROM src.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        stats: dict[str, int] = {}
        for t in sorted(mine & theirs):
            a, b = columns(conn, t, "main"), columns(conn, t, "src")
            cols = [c for c in a if c in b]          # 교집합, 대상 DB 순서 유지
            if not cols:
                continue
            cl = ", ".join(f'"{c}"' for c in cols)
            before = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
            if t in APPEND_ONLY:
                # UNIQUE 가 없으므로 값이 완전히 같은 행만 걸러낸다
                conn.execute(f"INSERT INTO main.{t} ({cl}) "
                             f"SELECT {cl} FROM src.{t} "
                             f"EXCEPT SELECT {cl} FROM main.{t}")
            else:
                # ★ OR IGNORE = 충돌 시 **내 값이 이긴다**. 이번 실행이 방금 수집한 값이다.
                conn.execute(f"INSERT OR IGNORE INTO main.{t} ({cl}) SELECT {cl} FROM src.{t}")
            after = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
            stats[t] = after - before
        conn.commit()
        conn.execute("DETACH DATABASE src")
        return stats
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--into", required=True, help="이 DB 에 합친다 (내 것, 값 충돌 시 승리)")
    ap.add_argument("--from", dest="src", required=True, help="여기서 가져온다 (원격)")
    args = ap.parse_args()

    into, src = Path(args.into), Path(args.src)
    if not src.exists():
        print(f"{src} 가 없습니다 — 병합할 것이 없습니다.")
        return 0
    if not into.exists():
        # 내 DB 가 없으면 상대 것을 그대로 쓴다
        into.parent.mkdir(parents=True, exist_ok=True)
        into.write_bytes(src.read_bytes())
        print(f"{into} 가 없어 원격본을 그대로 씁니다.")
        return 0

    stats = merge(into, src)
    added = sum(stats.values())
    print(f"DB 병합 — 원격에서 {added:,}행 추가")
    for t, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        if n:
            print(f"  {t}: +{n:,}")
    if not added:
        print("  (추가된 행 없음 — 내 DB 가 원격을 이미 포함합니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
