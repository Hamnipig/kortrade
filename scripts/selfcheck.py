#!/usr/bin/env python3
"""배포 정합성 점검 — 모듈이 다 있고 설정이 다 읽히는지. (API 호출 0회, 1초)

왜 필요한가 ─ 이 레포는 zip 으로 파일을 올려 갱신한다. 그러다 파일 하나를
빼먹으면 수집 단계 한복판에서 ImportError 로 죽는다. 실제로 그랬다:

    from kortrade.flash import ols
    ImportError: cannot import name 'ols' from 'kortrade.flash'

battery.py 는 올라갔는데 flash.py 가 안 올라간 상태였다. 인증키 확인을 통과하고
수집을 시작한 뒤에야 터져서, 그 사이 시간과 호출을 버렸다.

이 스크립트를 **수집 전 첫 단계**로 돌리면 그런 사고가 1초 만에, 명확한
메시지와 함께 잡힌다.

사용법:
    python scripts/selfcheck.py
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 레이어 모듈. 하나라도 빠지면 그 레이어가 통째로 죽는다.
MODULES = [
    "kortrade.stats", "kortrade.client", "kortrade.store", "kortrade.collect",
    "kortrade.codes", "kortrade.regions", "kortrade.sectors", "kortrade.analyze",
    "kortrade.watchlist", "kortrade.chains", "kortrade.flash", "kortrade.battery",
    "kortrade.kpi", "kortrade.discover", "kortrade.localization",
    "kortrade.pq", "kortrade.breadth",
]

# 설정은 '읽히는 것'만으로 부족하다 — validate() 까지 통과해야 수집이 의미를 갖는다.
CONFIGS = [
    ("워치리스트", "kortrade.watchlist"),
    ("밸류체인", "kortrade.chains"),
    ("속보", "kortrade.flash"),
    ("2차전지", "kortrade.battery"),
]

# 수집·빌드 스크립트. import 만 해 본다(main 은 실행하지 않는다).
SCRIPTS = [
    "run_update", "run_watchlist", "run_universe", "run_flash",
    "build_site", "build_watchlist", "build_universe", "build_chains",
    "build_flash", "build_battery", "verify_flash", "inspect_flash",
    "bootstrap_codes", "check_key", "dump_places",
]


def main() -> int:
    bad = 0

    print("[1/3] 모듈 import")
    mods = {}
    for name in MODULES:
        try:
            mods[name] = importlib.import_module(name)
            print(f"  ok   {name}")
        except Exception as exc:                       # noqa: BLE001
            bad += 1
            print(f"  ✗    {name}: {type(exc).__name__}: {exc}")
            if isinstance(exc, ImportError):
                print("       → 파일이 누락됐거나 구버전일 수 있습니다. "
                      "이 모듈과 이 모듈이 import 하는 파일을 함께 올리세요.")

    print("\n[2/3] 설정 로드 + 검증")
    for label, name in CONFIGS:
        mod = mods.get(name)
        if mod is None:
            print(f"  –    {label}: 모듈을 못 읽어 건너뜀")
            continue
        try:
            cfg = mod.load()
            errs = cfg.validate()
            if errs:
                bad += 1
                print(f"  ✗    {label}: 설정 오류 {len(errs)}건")
                for e in errs[:8]:
                    print(f"         - {e}")
            else:
                print(f"  ok   {label}")
        except Exception as exc:                       # noqa: BLE001
            bad += 1
            print(f"  ✗    {label}: {type(exc).__name__}: {exc}")

    print("\n[3/3] 스크립트 import")
    sys.path.insert(0, str(ROOT / "scripts"))
    for s in SCRIPTS:
        path = ROOT / "scripts" / f"{s}.py"
        if not path.exists():
            bad += 1
            print(f"  ✗    {s}.py 파일이 없습니다")
            continue
        try:
            importlib.import_module(s)
            print(f"  ok   {s}.py")
        except Exception as exc:                       # noqa: BLE001
            bad += 1
            print(f"  ✗    {s}.py: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)

    print()
    if bad:
        print(f"::error::배포 정합성 점검 실패 {bad}건 — 수집을 시작하기 전에 멈춥니다. "
              f"누락되거나 구버전인 파일을 올린 뒤 다시 실행하세요.")
        return 1
    print(f"정합성 OK — 모듈 {len(MODULES)}개 · 설정 {len(CONFIGS)}개 · "
          f"스크립트 {len(SCRIPTS)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
