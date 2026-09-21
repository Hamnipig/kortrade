#!/usr/bin/env python3
"""속보 API 응답 **원문**을 그대로 찍는다. 진단 전용.

왜 필요한가 ─ 이 API 는 공개 문서가 부실하고, 필드명이 itemUsdAmt00~10 처럼
번호뿐이라 응답을 직접 보지 않으면 구조를 알 수 없다. 실제로 priodMon 에
달(月)이 아닌 값이 들어와 수집이 통째로 어긋났고, 추측으로 두 번 고치다
두 번 다 틀렸다. 한 번 찍어 보면 끝날 일이었다.

호출 2회(품목·국가 각 1개월)면 끝난다. **인증키는 절대 출력하지 않는다.**

사용법:
    export DATA_GO_KR_SERVICE_KEY='...'
    python scripts/inspect_flash.py
    python scripts/inspect_flash.py --yymm 202608 --chars 4000
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.client import CONFIG_DIR  # noqa: E402
from kortrade.collect import current_yymm, shift_yymm  # noqa: E402

TIMEOUT = (10, 45)


def dump(name: str, spec: dict, base: str, key: str, yymm: str, chars: int) -> None:
    params = {"strtYymm": yymm, "endYymm": yymm}
    url = f"{base}{spec['path']}?serviceKey={key}&{urlencode(params, quote_via=quote)}"
    print(f"\n{'=' * 72}\n[{name}] {spec['path']}  ({yymm} 1개월)\n{'=' * 72}")
    try:
        resp = requests.get(url, timeout=TIMEOUT)          # ★ url 은 출력하지 않는다
    except requests.RequestException as exc:
        print(f"  연결 실패: {type(exc).__name__}: {exc}")
        return
    body = resp.text
    print(f"  HTTP {resp.status_code} · {len(body):,}자")

    print(f"\n--- 원문 앞 {chars}자 ---")
    print(body[:chars])
    if len(body) > chars:
        print(f"... (이하 {len(body) - chars:,}자 생략)")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        print(f"\n  XML 파싱 실패: {exc}")
        return

    items = list(root.iter("item"))
    print(f"\n--- 구조 ---")
    print(f"  루트 태그      : {root.tag}")
    print(f"  <item> 개수    : {len(items)}")
    tc = root.findtext(".//totalCount")
    if tc:
        print(f"  totalCount     : {tc}")
    if not items:
        print("  ※ <item> 이 없습니다. 위 원문에서 오류 메시지를 확인하세요.")
        return

    tags: list[str] = []
    for it in items:
        for ch in it:
            if ch.tag not in tags:
                tags.append(ch.tag)
    print(f"  item 자식 태그 ({len(tags)}개, 등장 순서):")
    for t in tags:
        print(f"    - {t}")

    # 설정에 적어 둔 매핑과 실제 태그를 맞춰 본다. 여기서 어긋나면 그게 원인이다.
    mapped = spec.get("item_fields", {})
    missing = [t for t in mapped if t not in tags]
    extra = [t for t in tags if t not in mapped]
    print(f"\n  config/api.yaml 매핑 대조:")
    print(f"    설정에 있으나 응답에 없음 : {missing or '없음'}")
    print(f"    응답에 있으나 설정에 없음 : {extra or '없음'}")

    print(f"\n--- 앞 {min(4, len(items))}개 item 전체 값 ---")
    for i, it in enumerate(items[:4]):
        rec = {ch.tag: (ch.text or "").strip() for ch in it}
        print(f"  [{i}] {rec}")

    # 기간 관련 태그만 따로. 어느 태그가 '달'이고 어느 태그가 '순'인지 한눈에 본다.
    per = [t for t in tags if "riod" in t or "Ym" in t or "ym" in t
           or "Dt" in t or "Mon" in t or "Year" in t]
    if per:
        print(f"\n--- 기간 태그 값 분포 (전체 {len(items)}행) ---")
        for t in per:
            vals = []
            for it in items:
                v = (it.findtext(t) or "").strip()
                if v not in vals:
                    vals.append(v)
            print(f"  {t:<14} {vals[:12]}{' …' if len(vals) > 12 else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yymm", default=None, help="기본값: 지난달")
    ap.add_argument("--chars", type=int, default=2500, help="원문 출력 길이")
    args = ap.parse_args()

    import os
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        print("DATA_GO_KR_SERVICE_KEY 가 설정되지 않았습니다.")
        return 1

    cfg = yaml.safe_load((CONFIG_DIR / "api.yaml").read_text(encoding="utf-8"))
    base = cfg["host"] + cfg["prefix"]
    yymm = args.yymm or shift_yymm(current_yymm(), -1)

    print("속보 API 응답 원문 진단")
    print(f"  기준월 {yymm} · 호출 2회 · 인증키는 출력하지 않습니다")
    for name in ("flash_item", "flash_country"):
        if name not in cfg["endpoints"]:
            print(f"\n[{name}] config/api.yaml 에 정의가 없습니다.")
            continue
        dump(name, cfg["endpoints"][name], base, key, yymm, args.chars)

    print(f"\n{'=' * 72}")
    print("위 '기간 태그 값 분포' 와 'item 전체 값' 을 그대로 보내주시면")
    print("수집기의 필드 매핑을 실제 응답에 맞춰 고칩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
