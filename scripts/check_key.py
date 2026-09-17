#!/usr/bin/env python3
"""인증키 진단 — 5개 엔드포인트를 각각 1회씩 호출해 어디까지 되는지 확인한다.

공공데이터포털은 **API별로 활용신청**이 필요하다. 키가 맞아도 신청하지 않은 API는
`SERVICE_KEY_IS_NOT_REGISTERED_ERROR`(코드 30)를 돌려준다. 전부 30이면 키 자체 또는
계정 레벨 문제이고, 일부만 30이면 그 API만 활용신청하면 된다.

사용법:
    export DATA_GO_KR_SERVICE_KEY='...'
    python scripts/check_key.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kortrade.client import CONFIG_DIR  # noqa: E402
import yaml  # noqa: E402

PROBES = {
    "item_country":  {"strtYymm": "202506", "endYymm": "202506", "cntyCd": "US", "hsSgn": "330499"},
    "item":          {"strtYymm": "202506", "endYymm": "202506", "hsSgn": "330499"},
    "sido":          {"strtYymm": "202506", "endYymm": "202506"},
    "sido_item":     {"strtYymm": "202506", "endYymm": "202506", "sidoCd": "42", "hsSgn": "330499"},
    "sigungu_item":  {"strtYymm": "202506", "endYymm": "202506", "HsSgn": "330499", "sidoCd": "42"},
}

# 연결 실패는 키 문제와 전혀 다르다. 짧게 재시도해서 일시적 끊김과 구분한다.
NET_RETRIES = 3
NET_WAIT = 15
HOST_HINT = "apis.data.go.kr (27.101.236.63)"

APPLY_URL = {
    "item_country": "https://www.data.go.kr/data/15100475/openapi.do",
    "item":         "https://www.data.go.kr/data/15101609/openapi.do",
    "sido":         "https://www.data.go.kr/data/15101643/openapi.do",
    "sido_item":    "https://www.data.go.kr/data/15101641/openapi.do",
    "sigungu_item": "https://www.data.go.kr/data/15134343/openapi.do",
}


def main() -> int:
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        print("DATA_GO_KR_SERVICE_KEY 가 설정되지 않았습니다.")
        return 1

    print(f"키 길이 {len(key)}자 / 앞 6자 {key[:6]}… / 뒤 4자 …{key[-4:]}")
    if len(key) < 40:
        print("  ⚠ 일반적인 공공데이터포털 인증키보다 짧습니다. 값을 다시 확인하세요.")
    print()

    cfg = yaml.safe_load((CONFIG_DIR / "api.yaml").read_text(encoding="utf-8"))
    base = cfg["host"] + cfg["prefix"]

    ok, bad, net = [], [], []
    for name, params in PROBES.items():
        path = cfg["endpoints"][name]["path"]
        qs = urlencode(params, quote_via=quote)
        url = f"{base}{path}?serviceKey={key}&{qs}"
        body = None
        # 연결 자체가 안 되는 건 키 문제가 아니다. 짧게 3번까지 다시 시도한다.
        for attempt in range(1, NET_RETRIES + 1):
            try:
                body = requests.get(url, timeout=(15, 60)).text
                break
            except requests.RequestException as exc:
                last = exc
                if attempt < NET_RETRIES:
                    print(f"  … {name:<14} 연결 실패({attempt}/{NET_RETRIES}) — {NET_WAIT}초 후 재시도")
                    time.sleep(NET_WAIT)
        if body is None:
            print(f"  ✗ {name:<14} 연결 불가: {type(last).__name__}")
            net.append(name)
            # 5개 모두 같은 호스트다. 둘 연속 연결 불가면 나머지를 찔러볼 이유가 없다.
            if len(net) >= 2 and not ok and not bad:
                print("  … 같은 호스트이므로 나머지 점검은 생략합니다")
                break
            continue

        if "SERVICE_KEY_IS_NOT_REGISTERED" in body:
            print(f"  ✗ {name:<14} 코드 30 등록되지 않은 서비스키 → 활용신청 필요: {APPLY_URL[name]}")
            bad.append(name)
        elif "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in body:
            print(f"  ✗ {name:<14} 일일 트래픽 초과")
            bad.append(name)
        elif "<errMsg>" in body:
            msg = body.split("<errMsg>")[1].split("</errMsg>")[0]
            print(f"  ✗ {name:<14} {msg}")
            bad.append(name)
        else:
            cnt = body.split("<totalCount>")[1].split("</totalCount>")[0] if "<totalCount>" in body else "?"
            n_item = body.count("<item>")
            print(f"  ✓ {name:<14} 정상 (totalCount={cnt}, item={n_item})")
            ok.append(name)

    print(f"\n정상 {len(ok)} / 키·권한 실패 {len(bad)} / 연결 불가 {len(net)}")

    # ★ 연결 불가와 키 문제를 절대 섞어서 안내하지 않는다.
    #   실측 사례: 러너(해외 IP)에서 27.101.236.63 으로 SYN 이 드롭돼 전부 타임아웃인데
    #   "활용신청을 확인하세요"라고 안내해 엉뚱한 곳을 뒤지게 만들었다.
    if net and not ok and not bad:
        print(f"""
키 문제가 아닙니다. {HOST_HINT} 서버에 **연결 자체가 되지 않았습니다**
(TLS/인증 단계까지 가지도 못한 connect timeout).

  · 같은 키로 한국에서 브라우저로 열면 정상 응답합니다 → 키·활용신청은 정상.
  · GitHub Actions 러너는 해외(Azure) IP라 관세청 게이트웨이가 간헐적으로
    응답하지 않을 수 있습니다. 며칠 전 같은 워크플로는 정상 수집했습니다.

조치:
  1. 30분~2시간 뒤 'Run workflow' 재실행 — 대부분 이걸로 지나갑니다.
  2. 계속 실패하면 국내 IP에서 수집해야 합니다. README 의
     '자체 호스팅 러너(self-hosted runner)' 항목을 보세요.""")
        return 2   # 2 = 네트워크 도달 실패 (키/권한 문제인 1과 구분)

    if not ok:
        print("""
전부 실패했습니다. 순서대로 확인하세요:
  1. 활용신청 여부 — 마이페이지 > 오픈API > 개발계정 에서 위 5개 API가 '승인' 상태인지.
     API마다 개별 신청이 필요합니다(개발단계는 자동승인, 신청 즉시 승인).
  2. 발급 직후 전파 지연 — 신규 키는 게이트웨이 반영까지 1~2시간 걸립니다.
  3. 키 값 — 마이페이지의 '일반 인증키(Encoding)' 을 그대로 복사했는지.
     Decoding 키를 쓸 경우 특수문자(+, /, =)가 있으면 URL 인코딩이 필요합니다.""")
    elif bad:
        print("\n실패한 API만 위 링크에서 활용신청하면 됩니다.")
    elif net:
        print(f"\n일부만 연결 실패({net}) — 일시적 네트워크 문제로 보입니다. 수집은 재시도로 넘어갑니다.")
        return 0
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
