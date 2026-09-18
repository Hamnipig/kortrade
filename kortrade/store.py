"""SQLite 저장소.

설계 원칙
  1. 모든 테이블은 자연키에 UNIQUE 제약 → 재수집 시 UPSERT 로 소급 정정을 자동 반영한다.
  2. 값이 실제로 바뀐 행은 revisions 테이블에 남긴다. 관세청은 정정/취하를 소급 반영하므로
     "지난달에 본 숫자가 이번달에 바뀌었다"는 사실 자체가 정보다.
  3. fetch_log 로 (엔드포인트, 파라미터) 단위 수집 이력을 남겨 중복 호출을 피한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS sector_trade (
    period       TEXT NOT NULL,          -- 'YYYY-MM'
    hs_code      TEXT NOT NULL,          -- 응답 원본. 6단위로 요청해도 10단위로 돌아온다
    hs6          TEXT,                   -- 6단위 롤업 키. 기업 레이어와 조인할 때 쓴다
    hs_name      TEXT,
    country_code TEXT NOT NULL,
    country_name TEXT,
    exp_usd      INTEGER,
    exp_wgt      INTEGER,
    imp_usd      INTEGER,
    imp_wgt      INTEGER,
    bal_usd      INTEGER,
    fetched_at   TEXT NOT NULL,
    UNIQUE (period, hs_code, country_code)
);
CREATE INDEX IF NOT EXISTS ix_sector_hs      ON sector_trade (hs_code, period);
CREATE INDEX IF NOT EXISTS ix_sector_hs6     ON sector_trade (hs6, period);
CREATE INDEX IF NOT EXISTS ix_sector_country ON sector_trade (country_code, period);

-- 전산업 유니버스. 97개 장을 전수 수집해 **HS 4단위로 집계**해 담는다.
-- 10단위 전수(약 12,000코드 x 80개월 = 96만행)는 DB 가 GitHub 100MB 파일 제한을
-- 넘기므로 담지 않는다. 4단위면 약 1,200 x 80 = 10만행으로 10~20MB 수준이다.
-- 10단위가 필요한 품목은 config/watchlist.yaml 에 올려 sector_trade 로 따로 받는다.
CREATE TABLE IF NOT EXISTS universe_trade (
    period     TEXT NOT NULL,           -- 'YYYY-MM'
    hs4        TEXT NOT NULL,           -- 4단위 항
    hs2        TEXT NOT NULL,           -- 2단위 장 (롤업 키)
    top_name   TEXT,                    -- 그 항에서 금액이 가장 큰 10단위의 공식 품명
    exp_usd    INTEGER,
    exp_wgt    INTEGER,
    imp_usd    INTEGER,
    fetched_at TEXT NOT NULL,
    UNIQUE (period, hs4)
);
CREATE INDEX IF NOT EXISTS ix_uni_hs4 ON universe_trade (hs4, period);
CREATE INDEX IF NOT EXISTS ix_uni_hs2 ON universe_trade (hs2, period);

CREATE TABLE IF NOT EXISTS region_trade (
    period       TEXT NOT NULL,
    hs_code      TEXT NOT NULL,          -- HS 6단위
    hs_name      TEXT,
    sido_cd      TEXT NOT NULL,
    sido_name    TEXT,
    sigungu_name TEXT NOT NULL,
    exp_cnt      INTEGER,
    exp_usd      INTEGER,
    imp_cnt      INTEGER,
    imp_usd      INTEGER,
    bal_usd      INTEGER,
    fetched_at   TEXT NOT NULL,
    UNIQUE (period, hs_code, sido_cd, sigungu_name)
);
CREATE INDEX IF NOT EXISTS ix_region_place ON region_trade (sido_cd, sigungu_name, hs_code, period);
CREATE INDEX IF NOT EXISTS ix_region_hs    ON region_trade (hs_code, period);

CREATE TABLE IF NOT EXISTS sido_codes (
    sido_cd    TEXT NOT NULL,
    sido_name  TEXT NOT NULL,
    valid_from TEXT NOT NULL,            -- 'YYYY-MM'. 행정체계 개편 대응 (예: 2026-07)
    checked_at TEXT NOT NULL,
    UNIQUE (sido_cd, valid_from)
);

CREATE TABLE IF NOT EXISTS revisions (
    table_name TEXT NOT NULL,
    key_json   TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    noticed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rev_time ON revisions (noticed_at);

CREATE TABLE IF NOT EXISTS fetch_log (
    endpoint   TEXT NOT NULL,
    params_json TEXT NOT NULL,
    rows       INTEGER NOT NULL,
    status     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (endpoint, params_json)
);

CREATE TABLE IF NOT EXISTS company_kpi (
    company   TEXT NOT NULL,
    period    TEXT NOT NULL,             -- 분기: 'YYYY-Qn'
    metric    TEXT NOT NULL,             -- '매출액' 등
    value     REAL,
    unit      TEXT,
    source    TEXT,
    UNIQUE (company, period, metric)
);
"""

SECTOR_COLS = ["period", "hs_code", "hs6", "hs_name", "country_code", "country_name",
               "exp_usd", "exp_wgt", "imp_usd", "imp_wgt", "bal_usd"]
SECTOR_KEY = ["period", "hs_code", "country_code"]

REGION_COLS = ["period", "hs_code", "hs_name", "sido_cd", "sido_name", "sigungu_name",
               "exp_cnt", "exp_usd", "imp_cnt", "imp_usd", "bal_usd"]
REGION_KEY = ["period", "hs_code", "sido_cd", "sigungu_name"]

UNIVERSE_COLS = ["period", "hs4", "hs2", "top_name", "exp_usd", "exp_wgt", "imp_usd"]
UNIVERSE_KEY = ["period", "hs4"]

_NUMERIC = {"exp_usd", "exp_wgt", "imp_usd", "imp_wgt", "bal_usd", "exp_cnt", "imp_cnt"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(round(float(str(v).replace(",", "").strip())))
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self, path: str | Path = "data/kortrade.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.commit()
        self.close()

    # ------------------------------------------------------------ upsert

    def _upsert(self, table: str, cols: Sequence[str], key: Sequence[str],
                rows: Iterable[dict]) -> dict[str, int]:
        rows = list(rows)
        if not rows:
            return {"inserted": 0, "updated": 0, "unchanged": 0}

        stats = {"inserted": 0, "updated": 0, "unchanged": 0}
        now = _now()
        cur = self.conn.cursor()
        where = " AND ".join(f"{k}=?" for k in key)
        value_cols = [c for c in cols if c not in key]

        for raw in rows:
            rec = {c: (_to_int(raw.get(c)) if c in _NUMERIC else raw.get(c)) for c in cols}
            keyvals = [rec[k] for k in key]
            cur.execute(f"SELECT {', '.join(value_cols)} FROM {table} WHERE {where}", keyvals)
            existing = cur.fetchone()

            if existing is None:
                cur.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}, fetched_at) "
                    f"VALUES ({', '.join('?' * len(cols))}, ?)",
                    [rec[c] for c in cols] + [now],
                )
                stats["inserted"] += 1
                continue

            changed = {c: (existing[c], rec[c]) for c in value_cols
                       if existing[c] != rec[c] and rec[c] is not None}
            if not changed:
                stats["unchanged"] += 1
                continue

            cur.execute(
                f"UPDATE {table} SET {', '.join(f'{c}=?' for c in value_cols)}, fetched_at=? "
                f"WHERE {where}",
                [rec[c] for c in value_cols] + [now] + keyvals,
            )
            kjson = json.dumps(dict(zip(key, keyvals)), ensure_ascii=False)
            for field, (old, new) in changed.items():
                # 이름 필드 변경(품목명 표기 변경 등)은 정정으로 보지 않는다
                if field.endswith("_name"):
                    continue
                cur.execute(
                    "INSERT INTO revisions (table_name, key_json, field, old_value, new_value, noticed_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (table, kjson, field, str(old), str(new), now),
                )
            stats["updated"] += 1

        self.conn.commit()
        return stats

    def upsert_sector(self, rows: Iterable[dict]) -> dict[str, int]:
        return self._upsert("sector_trade", SECTOR_COLS, SECTOR_KEY, rows)

    def upsert_region(self, rows: Iterable[dict]) -> dict[str, int]:
        return self._upsert("region_trade", REGION_COLS, REGION_KEY, rows)

    def upsert_universe(self, rows: Iterable[dict]) -> dict[str, int]:
        return self._upsert("universe_trade", UNIVERSE_COLS, UNIVERSE_KEY, rows)

    # ------------------------------------------------------------ 시도코드

    def save_sido_codes(self, mapping: dict[str, str], valid_from: str) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO sido_codes (sido_cd, sido_name, valid_from, checked_at) VALUES (?,?,?,?)"
            " ON CONFLICT(sido_cd, valid_from) DO UPDATE SET sido_name=excluded.sido_name,"
            " checked_at=excluded.checked_at",
            [(cd, nm, valid_from, now) for cd, nm in mapping.items()],
        )
        self.conn.commit()

    def sido_codes(self, valid_from: str | None = None) -> dict[str, str]:
        if valid_from:
            rows = self.conn.execute(
                "SELECT sido_cd, sido_name FROM sido_codes WHERE valid_from=?", (valid_from,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT sido_cd, sido_name FROM sido_codes WHERE valid_from ="
                " (SELECT MAX(valid_from) FROM sido_codes)"
            ).fetchall()
        return {r["sido_cd"]: r["sido_name"] for r in rows}

    def all_sido_names(self) -> list[str]:
        """개편 전/후 표를 **합집합**으로 돌려준다.

        2026-07 개편으로 '광주광역시'·'전라남도'는 사라지고 '전남광주통합특별시'가 생겼다.
        어느 한쪽 표만 쓰면 반대쪽 기간을 통째로 놓친다. 수집은 둘 다 돌되,
        각 시도가 존재하지 않던 기간은 collect_region 이 건너뛴다.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT sido_name FROM sido_codes ORDER BY sido_name"
        ).fetchall()
        return [r["sido_name"] for r in rows]

    def resolve_sido(self, name: str) -> str | None:
        """'강원' / '강원도' / '강원특별자치도' 어느 표기로도 코드를 찾는다."""
        from .codes import canon_sido
        rows = self.conn.execute(
            "SELECT sido_cd, sido_name FROM sido_codes ORDER BY valid_from DESC"
        ).fetchall()
        needle = canon_sido(name)
        for r in rows:
            if canon_sido(r["sido_name"]) == needle:
                return r["sido_cd"]
        return None

    # ------------------------------------------------------------ fetch log

    def mark_fetched(self, endpoint: str, params: dict, rows: int, status: str = "ok") -> None:
        self.conn.execute(
            "INSERT INTO fetch_log (endpoint, params_json, rows, status, fetched_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(endpoint, params_json) DO UPDATE SET"
            " rows=excluded.rows, status=excluded.status, fetched_at=excluded.fetched_at",
            (endpoint, json.dumps(params, sort_keys=True, ensure_ascii=False), rows, status, _now()),
        )
        self.conn.commit()

    def already_fetched(self, endpoint: str, params: dict) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM fetch_log WHERE endpoint=? AND params_json=? AND status='ok'",
            (endpoint, json.dumps(params, sort_keys=True, ensure_ascii=False)),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ KPI

    def upsert_kpi(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        self.conn.executemany(
            "INSERT INTO company_kpi (company, period, metric, value, unit, source)"
            " VALUES (:company,:period,:metric,:value,:unit,:source)"
            " ON CONFLICT(company, period, metric) DO UPDATE SET"
            " value=excluded.value, unit=excluded.unit, source=excluded.source",
            rows,
        )
        self.conn.commit()
        return len(rows)

    # ------------------------------------------------------------ 조회

    def frame(self, sql: str, params: Sequence = ()):
        import pandas as pd
        return pd.read_sql_query(sql, self.conn, params=params)

    def coverage(self) -> dict:
        def one(sql):
            return self.conn.execute(sql).fetchone()
        s = one("SELECT COUNT(*) n, MIN(period) a, MAX(period) b FROM sector_trade")
        r = one("SELECT COUNT(*) n, MIN(period) a, MAX(period) b FROM region_trade")
        rev = one("SELECT COUNT(*) n FROM revisions")
        return {
            "sector_trade": {"rows": s["n"], "from": s["a"], "to": s["b"]},
            "region_trade": {"rows": r["n"], "from": r["a"], "to": r["b"]},
            "revisions": rev["n"],
        }
